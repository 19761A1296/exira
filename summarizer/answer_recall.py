# summarizer/answer_recall.py
"""Summarises answers the user has already been given this session.

Reached when the analyzer returns relation ANSWER_RECALL. Nothing downstream
runs - no flow builder, no connector, no engine, no web - because every word of
the answer is text the user has already seen.

    summarise_answers(question, answers, scope) -> str

    answers : [{"question": str, "answer": str}, ...] oldest first
    scope   : "last" -> the newest answer only
              "all"  -> every stored answer

Fails open. If the model call breaks, the raw answer is returned rather than an
error, because the text is already correct - it just was not condensed.

.env
----
    OPENROUTER_API_KEY=sk-or-...
    OPENROUTER_MODEL_RECALL=anthropic/claude-haiku-4.5
"""

import os

import requests
from dotenv import load_dotenv

from summarizer.connector import ANSWER_CHARS

load_dotenv()

URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.getenv("OPENROUTER_MODEL_RECALL", "google/gemini-3.5-flash-lite")
MAX_TOKENS = int(os.getenv("RECALL_MAX_TOKENS", "5000"))
RE_ANSWER_CHARS = int(os.getenv("RECALL_ANSWER_CHARS", "10000"))
MAX_ANSWERS = int(os.getenv("RECALL_MAX_ANSWERS", "20"))
PRINT_RECALL = (os.getenv("PRINT_RECALL", "1") or "").strip() not in {"0", "false", "False", ""}

NOTHING_YET = "Nothing to summarise yet — ask me something first."


PROMPT_LAST = """Condense one answer the user has already been given.

RULES
- Use only what the answer contains. Add nothing, look nothing up, draw no new
  conclusions.
- Keep every company name, HS code, port, country, date and figure exactly as
  written. Never round a number or shorten a name.
- Lead with the finding. Drop the throat-clearing, the caveats and the repetition.
- Match the length they asked for. "Shorten this" means a few sentences; "in one
  line" means one line. When they gave no steer, aim for a short paragraph.
- Plain prose. No headings, no bullet points, no markdown.
- Do not mention that this is a summary, do not say "previously" or "as I said".
  Just give the condensed answer.
- If the answer was itself short, return it almost unchanged rather than padding.

WHAT THEY ASKED FOR
{request}

THE QUESTION THAT WAS ANSWERED
{question}

THE ANSWER
{answer}

Write the condensed version now."""


PROMPT_ALL = """Recap everything the user has been told in this session.

You are given each question they asked and the answer they received, oldest
first. Draw them into one account of what has been covered.

RULES
- Use only what the answers contain. Add nothing, look nothing up, draw no new
  conclusions of your own.
- Keep every company name, HS code, port, country, date and figure exactly as
  written.
- Follow the order the session took, so it reads as a progression.
- Give each topic a sentence or two. Drop repetition where two answers covered
  the same ground, and say so once rather than twice.
- Where one answer built on another, join them: "having identified X, the market
  options were Y".
- Note anything that came back empty, in one short clause, so the user knows it
  was asked and not answered.
- Plain prose, a short paragraph per topic. No headings, no bullet points, no
  markdown, no numbering.
- Do not mention sub-questions, routes, sources or any process.
- Match the length they asked for. When they gave no steer, keep the whole recap
  under roughly 250 words.

WHAT THEY ASKED FOR
{request}

THE SESSION
{session}

Write the recap now."""


# ───────────────────────── llm ─────────────────────────

def _chat(prompt: str) -> str:
    r = requests.post(
        URL,
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
        json={
            "model": MODEL,
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=90,
    )
    r.raise_for_status()
    payload = r.json()

    if payload.get("error"):
        raise RuntimeError(payload["error"].get("message", "OpenRouter error"))

    choice = payload["choices"][0]
    content = (choice["message"].get("content") or "").strip()
    if not content:
        raise RuntimeError(f"empty content (finish_reason={choice.get('finish_reason')})")

    return content


def _session_block(answers: list) -> str:
    """Every stored answer, labelled with the question that produced it."""
    parts = []
    for i, item in enumerate(answers, 1):
        question = str(item.get("question") or "").strip() or "(not recorded)"
        answer = str(item.get("answer") or "").strip()[:RE_ANSWER_CHARS]
        if not answer:
            continue
        parts.append(f"{i}. THEY ASKED: {question}\n   THEY WERE TOLD: {answer}")
    return "\n\n".join(parts)


# ───────────────────────── entry point ─────────────────────────

def summarise_answers(request: str, answers: list, scope: str = "last") -> str:
    """Condense one earlier answer, or recap the whole session. Never raises."""
    answers = [a for a in (answers or [])
               if isinstance(a, dict) and str(a.get("answer") or "").strip()]

    if not answers:
        return NOTHING_YET

    answers = answers[-MAX_ANSWERS:]
    scope = (scope or "last").lower().strip()
    request = (request or "").strip() or "summarise it"

    if scope == "all" and len(answers) == 1:
        scope = "last"              # one answer is not a session

    try:
        if scope == "all":
            prompt = PROMPT_ALL.format(request=request,
                                       session=_session_block(answers))
        else:
            last = answers[-1]
            prompt = PROMPT_LAST.format(
                request=request,
                question=str(last.get("question") or "(not recorded)").strip(),
                answer=str(last.get("answer") or "").strip()[:RE_ANSWER_CHARS],
            )

        out = _chat(prompt)
        if PRINT_RECALL:
            print(f"--- RECALL {scope} | {len(answers)} stored | {len(out)} chars")
        return out

    except Exception as exc:
        print("summarise_answers failed:", type(exc).__name__, exc)
        # the text is already correct, it just was not condensed
        if scope == "all":
            return "\n\n".join(
                f"{a['question']}\n{a['answer']}" for a in answers
                if a.get("question")
            )
        return answers[-1]["answer"]


def capture(answers: list, question: str, answer: str,
            limit: int = MAX_ANSWERS) -> list:
    """Append one answered turn, keeping the newest `limit`. Returns the list."""
    question = (question or "").strip()
    answer = (answer or "").strip()
    if not answer:
        return answers

    answers.append({"question": question, "answer": answer})
    if len(answers) > limit:
        del answers[:len(answers) - limit]
    return answers


def preview(answer: str, chars: int = 300) -> str:
    """First `chars` of an answer, for a compact listing."""
    text = " ".join((answer or "").split())
    return text if len(text) <= chars else text[:chars].rstrip() + "..."


# ───────────────────────── run directly ─────────────────────────
#
#   python -m summarizer.answer_recall

if __name__ == "__main__":
    SESSION = [
        {"question": "Who are the top buyers of HS 610910 in the last 24 months?",
         "answer": "Over the last 24 months the largest buyers of HS 610910 were "
                   "TARGET SOURCING in the United States at $4.1M, ZARA TRADING "
                   "in Spain at $2.8M and H&M ASIA in Hong Kong at $1.9M. "
                   "Together they account for most of the recorded volume, and "
                   "all three ship through Chennai Sea."},
        {"question": "Which countries show increasing demand for cotton knitwear?",
         "answer": "Germany, the Netherlands and Canada all show rising import "
                   "volumes for HS 6109 over the last eight quarters, with "
                   "Germany growing fastest. Vietnam and Bangladesh are the main "
                   "competing origins into those markets."},
        {"question": "What tariffs apply in Germany?",
         "answer": "Cotton knitted t-shirts entering Germany under HS 610910 "
                   "carry a 12% MFN duty. India does not currently hold a "
                   "preferential arrangement that reduces this."},
    ]

    print(f"answer recall — model {MODEL}\n")

    print("=== empty ===")
    print(summarise_answers("summarise that", []))

    print("\n=== preview (300 chars) ===")
    for item in SESSION:
        print(f"  {item['question'][:50]}")
        print(f"    {preview(item['answer'])}\n")

    print("=== capture, cap 2 ===")
    held = []
    for item in SESSION:
        capture(held, item["question"], item["answer"], limit=2)
    print(f"  kept {len(held)}: {[h['question'][:30] for h in held]}")

    print("\n=== scope last ===")
    print(summarise_answers("shorten that", SESSION, "last"))

    print("\n=== scope all ===")
    print(summarise_answers("recap the session", SESSION, "all"))

    print("\n=== one answer, scope all falls back to last ===")
    print(summarise_answers("recap everything", SESSION[:1], "all"))