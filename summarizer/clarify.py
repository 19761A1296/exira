# summarizer/clarify.py
"""Handles the engine's query-ambiguity clarifications.

The engine asks two kinds of question. The HS domain pick is already handled by
scope_pending. The other kind is query ambiguity - "what do you mean by best?" -
and that is what this module is for.

Two jobs, one small LLM call each, both only on a clarification turn:

    options_from(question)         -> ["High demand", "Low competition", ...]
    answers_question(question, a)  -> {"ok": bool, "reason": str}

The options are ANSWERS to the question, not replacement questions. That matters:
an answer leaves the original sub-question intact, so anything depending on it
still makes sense. A replacement question changes what the node asks, and the
dependant is then built on the wrong thing.

Both calls fail open. If options cannot be derived the caller falls back to the
engine's own followups; if validation fails the answer is accepted. Blocking a
user because a model call broke is worse than letting an odd answer through.

.env
----
    OPENROUTER_API_KEY=sk-or-...
    OPENROUTER_MODEL_CLARIFY=anthropic/claude-haiku-4.5
"""

import json
import os

import requests
from dotenv import load_dotenv

load_dotenv()

URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.getenv("OPENROUTER_MODEL_CLARIFY")
MAX_TOKENS = int(os.getenv("CLARIFY_MAX_TOKENS", "1000"))
MAX_OPTIONS = int(os.getenv("CLARIFY_MAX_OPTIONS", "3"))
PRINT_CLARIFY = (os.getenv("PRINT_CLARIFY", "1") or "").strip() not in {"0", "false", "False", ""}


PROMPT_OPTIONS = """The assistant asked the user a clarifying question. Turn it into
a short multiple choice list.

Each option is an ANSWER to the question, never another question. The user picks
one and it is sent back as their reply, so it must read as something a person
would say.

RULES
- Take the candidates the question itself offers. Most clarifying questions name
  them: "high demand, low competition, or premium prices" gives you three.
- When the question names fewer than {n}, add the obvious remaining ones for that
  kind of question. When it names more, keep the first {n}.
- Two to {n} options. Never more.
- Each option is a short phrase, two to five words, no trailing punctuation.
- Options must be genuinely different from each other.
- Never write an option that is a question.
- Never invent a product, country, HS code or figure that the question does not
  mention.
- If the question cannot be answered by choosing from a short list - it asks for
  a number, a date or a name only the user knows - return an empty list.

EXAMPLES

Question: "What criteria define 'best markets' for you? For example, are you
interested in high demand, low competition, or premium prices?"
{{"options": ["High demand", "Low competition", "Premium prices"]}}

Question: "Are you asking about buyers or suppliers?"
{{"options": ["Buyers", "Suppliers"]}}

Question: "Which product should I base this analysis on?"
{{"options": []}}
(only the user knows, and the list would be invented)

Question: "Do you want this by shipment value or by quantity?"
{{"options": ["By value", "By quantity"]}}

QUESTION
{question}

Return ONLY this JSON:
{{"options": ["...", "..."]}}
"""


PROMPT_VALIDATE = """The assistant asked the user a clarifying question. The user
typed a free-text reply. Decide whether the reply answers that question.

PASSES
- It names one of the things the question asked about, even loosely.
  Q: "high demand, low competition, or premium prices?"  A: "premium" -> passes
- It gives an equivalent answer in the user's own words.
  Q: "what does best mean?"  A: "wherever I can sell the most" -> passes
- It adds a condition on top of a valid answer.
  A: "low competition, but only in Europe" -> passes
- It is terse. Short is not wrong.

FAILS
- It answers a different question, or no question at all.
  Q: "buyers or suppliers?"  A: "what is my persona" -> fails
- It is off topic entirely.
  A: "book me a hotel in Quito" -> fails
- It is only a pleasantry with no content.
  A: "thanks" -> fails

RULES
- Judge only whether it answers THIS question. Do not judge whether it is a good
  idea, whether the data exists, or whether it mentions a product or HS code.
  A criterion like "high demand" names no product and still answers perfectly.
- When you are unsure, pass it. A wrongly rejected answer costs the user their
  turn; a wrongly accepted one just produces a slightly odd query.
- reason is one short clause, addressed to the user, explaining a failure.
  Leave it empty when it passes.

QUESTION
{question}

USER REPLY
{answer}

Return ONLY this JSON:
{{"ok": true|false, "reason": "short clause, or empty string"}}
"""


# ───────────────────────── llm ─────────────────────────

def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return text.strip()


def _chat_json(prompt: str) -> dict:
    r = requests.post(
        URL,
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
        json={
            "model": MODEL,
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30,
    )
    r.raise_for_status()
    payload = r.json()

    if payload.get("error"):
        raise RuntimeError(payload["error"].get("message", "OpenRouter error"))

    choice = payload["choices"][0]
    content = _strip_fences(choice["message"].get("content") or "")
    if not content:
        raise RuntimeError(f"empty content (finish_reason={choice.get('finish_reason')})")

    return json.loads(content)


def _log(label: str, value) -> None:
    if PRINT_CLARIFY:
        print(f"--- CLARIFY {label}: {value}")


# ───────────────────────── entry points ─────────────────────────

def options_from(question: str, limit: int = MAX_OPTIONS) -> list:
    """Short answers the user can pick from. [] when the question needs free text."""
    question = (question or "").strip()
    if not question:
        return []

    try:
        parsed = _chat_json(PROMPT_OPTIONS.format(question=question, n=limit))
        raw = parsed.get("options")
        if not isinstance(raw, list):
            return []

        seen, options = set(), []
        for item in raw:
            text = str(item or "").strip().rstrip(".?!")
            if not text or text.lower() in seen:
                continue
            if text.endswith("?"):          # a question slipped through
                continue
            seen.add(text.lower())
            options.append(text)
            if len(options) >= limit:
                break

        _log("options", options)
        return options

    except Exception as exc:
        print("clarify.options_from failed:", type(exc).__name__, exc)
        return []


def answers_question(question: str, answer: str) -> dict:
    """Does the typed reply answer the question? Fails open on any error."""
    question = (question or "").strip()
    answer = (answer or "").strip()

    if not answer:
        return {"ok": False, "reason": "nothing was typed"}
    if not question:
        return {"ok": True, "reason": ""}

    try:
        parsed = _chat_json(PROMPT_VALIDATE.format(question=question, answer=answer))
        result = {
            "ok": bool(parsed.get("ok")),
            "reason": str(parsed.get("reason") or "").strip(),
        }
        if not result["ok"] and not result["reason"]:
            result["reason"] = "it does not answer the question that was asked"
        _log("validate", f"{result['ok']} | {answer[:50]}")
        return result

    except Exception as exc:
        print("clarify.answers_question failed:", type(exc).__name__, exc)
        return {"ok": True, "reason": ""}          # fail open


def rejection_message(reason: str, fallback: str) -> str:
    """What the user sees when their typed answer is set aside."""
    reason = (reason or "it does not answer the question that was asked").strip()
    return (f"Not considering the answer you typed, because {reason}. "
            f"Going with: {fallback}.")


# ───────────────────────── run directly ─────────────────────────
#
#   python -m summarizer.clarify            the sample cases
#   python -m summarizer.clarify loop       type your own

if __name__ == "__main__":

    print(f"clarify — model {MODEL}\n")

    while True:
        try:
            q = input("\nEnter your clarifying question: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not q or q.lower() in {"exit", "quit"}:
            break

        # Extract possible options from the question
        opts = options_from(q)

        print("\nOptions:", opts or "(none — free text only)")

        # Ask for your test answer
        try:
            a = input("Enter your answer: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not a:
            continue

        # Validate your answer against your question
        v = answers_question(q, a)

        if v["ok"]:
            print("\nResult: ACCEPTED")
        else:
            print("\nResult: REJECTED")
            print("Reason:", v["reason"])