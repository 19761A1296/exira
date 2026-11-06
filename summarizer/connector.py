# summarizer/connector.py
"""Walks a tagged flow, runs each sub-question, and combines the answers.

    analyzer ──► question_flow_builder ──► classifier.tag_flow ──► connector

    run = FlowRun(flow, memory, handlers)
    out = run.start()

    while out["status"] == "needs_hs_pick":
        # the caller shows the engine's domain options and sends the user's pick
        # to the engine itself, then:
        out = run.resume()

    while out["status"] == "needs_answer":
        # the caller shows the engine's clarifying question as a short list,
        # takes the user's reply, then:
        out = run.supply_answer(text_the_engine_returned)

    out["status"] == "done"     -> out["answer"] is the combined reply
    out["status"] == "stopped"  -> out["answer"] explains why it stopped

FlowRun is resumable on purpose. The engine can interrupt a node twice over -
once to ask which HS industry a product belongs to, once to ask what an
ambiguous question actually means - and in Streamlit those pauses have to
survive a rerun, so the object is kept in session state rather than the call
stack.

The two pauses differ in who resolves them:

    needs_hs_pick   the CALLER sends the pick to the engine and calls resume(),
                    which retries the same node from the top.
    needs_answer    the CALLER sends the clarification answer to the engine,
                    gets the real reply back, and hands that text to
                    supply_answer(), which records it and moves on.

HANDLERS — injected by the caller, so this module imports no engine and no web.

    personal(question) -> str

    trade(question, node) -> {"text": str,          what to show and pass on
                              "ok": bool,           True only if data came back
                              "pending": bool,      True if the engine wants an HS pick
                              "needs_answer": bool} True if the engine asked a question

    For backward compatibility, a legacy trade(question) handler is also accepted.

    web(question)      -> str

WHAT EACH ROUTE DOES

    PERSONAL  personal()
    TRADE     trade(); if it came back empty, web() is tried as a fallback
    WEB       web() alone in a multi-node flow, because the grounding comes from
              the TRADE nodes it depends on; in a single-node flow trade() runs
              first and web() is appended, which is the existing behaviour

WHEN A NODE COMES BACK EMPTY

Both its source and its fallback produced nothing. If anything depends on that
node, the flow stops there and says so — a dependent question cannot be answered
from a blank. If nothing depends on it, the gap is recorded and the rest runs.

.env
----
    OPENROUTER_API_KEY=sk-or-...
    MODEL_CONNECTOR
"""

import inspect
import json
import os
import re
import unicodedata

import requests
from dotenv import load_dotenv

from summarizer.question_flow_builder import dependants_of

load_dotenv()

URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.getenv("MODEL_CONNECTOR")
REWRITE_MAX_TOKENS = int(os.getenv("CONNECTOR_REWRITE_MAX_TOKENS", "1000"))
COMBINE_MAX_TOKENS = int(os.getenv("CONNECTOR_COMBINE_MAX_TOKENS", "3000"))
ANSWER_CHARS = int(os.getenv("CONNECTOR_ANSWER_CHARS", "3000"))
PRINT_CONNECTOR = (os.getenv("PRINT_CONNECTOR", "1") or "").strip() not in {"0", "false", "False", ""}


PROMPT_REWRITE = """You are rewriting one sub-question so it can be asked on its own.

The sub-question refers to the answer of an earlier sub-question. That answer is
now known. Put the real value into the question and hand back one self-contained
line.

RULES
- Replace every reference to an earlier question with the actual value from its
  answer: "the product identified in q1" becomes the real product, "those buyers"
  becomes the real buyer names.
- Take only what the question needs. If the earlier answer lists ten buyers and
  the question needs the top three, name the top three.
- Copy names, HS codes, ports, countries and figures exactly as the earlier
  answer wrote them. Never round, rename or abbreviate them.
- Change nothing else. Keep the deliverable, the time window, the geography and
  the register exactly as they are.
- If the earlier answer does not contain what the question needs, say so by
  leaving the reference as plain words rather than inventing a value.
- Never add analysis, never answer the question, never add a preamble.
- Third person about "the user", the same as the input.

EARLIER ANSWERS
{answers}

SUB-QUESTION TO REWRITE
{question}

Return ONLY this JSON:
{{"question": "the rewritten self-contained question"}}
"""


PROMPT_COMBINE = """You are answering a user on a trade-intelligence platform.
 
Their message may contain one question or several related questions. Each part
has already been answered separately by the appropriate system. Your job is to
combine those answers into ONE polished, highly readable final response.
 
The final response should feel like a strong professional assistant response,
not like several raw outputs pasted together.
 
RULES
 
ACCURACY
- Use only what the answers below contain.
- Add no figures, names, HS codes, companies, ports, countries, percentages,
  dates, claims or recommendations of your own.
- Keep names, companies, HS codes, ports, countries and numerical values exactly
  as the underlying answers wrote them.
- Do not change the meaning of any individual answer.
- Do not resolve contradictions by inventing new facts.
 
ORDER
- Follow the user's original order.
- Answer what they asked first, first.
- When a later answer depends on an earlier answer, explicitly name the entity
  it depends on so the relationship is clear to the user.
 
STRUCTURE AND READABILITY
- If the user's request contains multiple meaningful topics or deliverables,
  divide the response into clearly separated sections.
- Give each major topic a short, natural Markdown heading using ##.
- Headings should describe the user's actual question in human-readable language.
- Do NOT use generic headings such as "Question 1", "Part 2", "TRADE", "WEB",
  "PERSONAL", "q1", "q2", or internal system terminology.
- Do not create a heading for every tiny sentence. Group closely related facts
  under the same heading.
- A short introductory sentence is allowed before the first section when useful.
- Use short paragraphs beneath each heading.
- Use bullet points only when they genuinely improve readability, such as:
    * several markets
    * several adjacent product categories
    * several routes
    * several risks or actions
- Avoid giant walls of text.
- Avoid excessive headings for simple one-part questions.
 
HEADINGS SHOULD BE CONTEXTUAL
 
For example, if the user asks:
"Which product or HS code do I deal in the most, show its trend from the USA in
2025, explain how tariffs affect it, and suggest expansion opportunities"
 
A good structure would be similar to:
 
## Your Most-Dealt Product
 
...
 
## 2025 USA Trade Trend
 
...
 
## How Tariffs Affect This Product
 
...
 
## Expansion Opportunities
 
...
 
The exact headings should adapt naturally to the user's actual question and the
answers available.
 
DEPENDENT ANSWERS
- Where one answer builds on another, make the dependency obvious.
  Example:
  "Based on your most-dealt product, HS 84795000..."
- Do not make the reader work out what "this", "it", or "that product" refers to
  when the actual product or HS code is already known.
 
STYLE
- Professional, clear and conversational.
- Prioritize readability over dense prose.
- Be concise where the underlying answer is simple and detailed where the
  underlying answer requires explanation.
- Use Markdown headings, bullets and bold emphasis sparingly when they improve
  scanning.
- Do not repeat the same fact in multiple sections.
- Do not mention routing, agents, sub-questions, q1/q2, prompts, connectors,
  databases or internal processing. Do not expose raw internal source labels.
- You MAY refer naturally to the information you worked with, for example
  "From the trade records I have...", "I found...", or "From the latest market
  information I found..." when that makes the answer clearer and more human.

HUMAN VOICE (CRITICAL)
- Write as if you are speaking directly to the user as a capable trade analyst,
  not as a machine reporting a system output.
- Use first-person phrasing naturally when appropriate: "I found...", "What
  stands out to me is...", "From the trade records I have...", "I'd focus on...".
- Address the user directly with "you" and "your" where the underlying answers
  support that relationship.
- Do NOT overuse "I" in every sentence. The tone should feel natural, calm and
  professional rather than scripted.
- Avoid robotic phrases such as "the system identified", "the database shows",
  "the provided data indicates", "the user has", or "according to the generated
  result" when a natural human phrasing can say the same thing.
- Do not pretend to have personal experiences, feelings, private knowledge or
  information beyond THE ANSWERS. First-person language refers only to the
  analysis and information supplied in this prompt.

PRESENTATION QUALITY (CRITICAL)
- Lead with the answer or the most decision-relevant finding. Do not make the
  user read a long setup before learning what matters.
- For complex answers, use a clear progression such as: key finding -> evidence
  -> implications/risks -> actions -> useful next checks. Adapt this structure
  to the actual question; do not force sections that are not needed.
- When several comparable entities and their values are already present in THE
  ANSWERS, a compact Markdown table is preferred when it improves scanning.
  Reformatting supplied facts into a table is allowed; recalculating or inventing
  values is not.
- Use bullets for distinct risks, findings or actions. Keep each bullet focused
  on one idea.
- Recommendations should be concrete and action-oriented when recommendations
  are present in THE ANSWERS. Do not create recommendations that upstream
  answers did not provide.
- Put caveats and limitations near the finding they qualify, or in a short
  final limitations/next-checks section when there are several of them.
- Do not dump long raw prose from one upstream answer when it can be organized
  more clearly without changing its meaning.

MARKDOWN AND TYPOGRAPHY HYGIENE (CRITICAL)
- Use standard Markdown only.
- Use only normal Unicode prose. NEVER use mathematical/fancy Unicode alphabets
  for ordinary words or sentences.
- Use ## for major section headings when sections are needed. Do not mix several
  decorative heading styles.
- Use **bold** only for short labels, entity names or key values that genuinely
  deserve emphasis. Never bold or italicize whole paragraphs.
- Do not use italic emphasis for ordinary prose. Do not use decorative ***text***.
- Use hyphen bullets (-) rather than asterisks as bullet markers.
- Never expose unmatched or stray Markdown markers such as *, **, ***, _, ` or #.
- Ensure normal spaces exist between words and after sentence-ending punctuation.
  If an upstream answer contains an obvious formatting defect such as words
  accidentally concatenated together, repair ONLY the spacing/punctuation needed
  for readability; do not alter entity names, HS codes, figures, dates or meaning.
- Put a blank line before and after headings, tables and lists so the frontend
  renders them cleanly.
- Do not place literal Markdown markers around values in a way that leaves the
  markers visible to the user.

FINAL SELF-CHECK (MANDATORY BEFORE RETURNING)
Before returning the response, silently verify all of the following:
1. Every factual claim is supported by THE ANSWERS.
2. No number, name, date, HS code or classification was changed.
3. The response reads like one person speaking to the user, not pasted modules.
4. No broken spacing or concatenated prose remains where it is obviously a
   formatting defect.
5. Markdown markers are balanced and standard.
6. Typography is consistent: normal text plus restrained standard Markdown only.
7. The most important answer is easy to find within the first few lines.
 
FAILURES
- If one part could not be answered, mention that briefly in the appropriate
  section and continue with the remaining answered parts.
- Do not apologise at length.
 
THE USER'S QUESTION
{question}
 
THE ANSWERS
{answers}
 
Write the combined final answer now."""


# ───────────────────────── llm helper ─────────────────────────

def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return text.strip()


def _clean_final_answer(text: str) -> str:
    """Normalize presentation artifacts without changing answer content.

    This is intentionally conservative. It does not rewrite facts, names, numbers,
    dates, HS codes or analytical conclusions; the LLM combine pass owns prose and
    structure. This helper only normalizes typography/whitespace and removes
    obviously unbalanced emphasis markers that can break the Streamlit rendering.
    """
    text = (text or "").strip()
    if not text:
        return ""

    # Compatibility-normalize decorative mathematical Unicode letters into normal
    # text while preserving ordinary Unicode names and punctuation.
    text = unicodedata.normalize("NFKC", text)

    # Remove invisible formatting characters that can make words render oddly.
    text = (
        text.replace("\u00a0", " ")
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\ufeff", "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )

    # Strip trailing whitespace and keep paragraph spacing predictable.
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # The final-answer style deliberately avoids italics. If a model still leaves
    # an unmatched bold/italic marker, remove that marker family rather than let
    # raw asterisks leak into the UI. Balanced standard bold remains untouched.
    if text.count("**") % 2:
        text = text.replace("**", "")
    single_star_view = text.replace("**", "")
    if single_star_view.count("*") % 2:
        text = text.replace("*", "")

    return text.strip()


def _chat(prompt: str, max_tokens: int, as_json: bool = False) -> str:
    body = {
        "model": MODEL,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if as_json:
        body["response_format"] = {"type": "json_object"}

    r = requests.post(
        URL,
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
        json=body,
        timeout=90,
    )
    r.raise_for_status()
    payload = r.json()

    if payload.get("error"):
        raise RuntimeError(payload["error"].get("message", "OpenRouter error"))

    return _strip_fences(payload["choices"][0]["message"].get("content") or "")


def _log(label: str, value="") -> None:
    if PRINT_CONNECTOR:
        print(f"--- CONNECTOR {label}" + (f" | {str(value)[:]}" if value else ""))


# ───────────────────────── handler compatibility ─────────────────────────

def _call_trade_handler(fn, question: str, node: dict):
    """
    Call the TRADE handler with the full node when the handler supports it.

    New contract:
        trade(question, node)

    Legacy contract:
        trade(question)

    This keeps the current UI/CLI working while the new Trade Mini Connector
    is wired in. Once both callers use the two-argument contract, this helper
    can remain harmlessly in place.
    """
    if fn is None:
        return {
            "text": "",
            "ok": False,
            "pending": False,
            "needs_answer": False,
        }

    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())

        accepts_varargs = any(
            p.kind == inspect.Parameter.VAR_POSITIONAL
            for p in params
        )

        positional = [
            p
            for p in params
            if p.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]

        if accepts_varargs or len(positional) >= 2:
            return fn(question, node)

    except (TypeError, ValueError):
        # If the callable cannot be introspected, preserve the legacy path.
        pass

    return fn(question)


# ───────────────────────── the run ─────────────────────────

class FlowRun:
    """One pass over a tagged flow. Resumable at the node that paused."""

    def __init__(self, flow: dict, memory: dict, handlers: dict,
                 original_question: str = ""):
        self.flow = flow or {}
        self.memory = memory or {}
        self.handlers = handlers or {}
        self.original_question = (original_question or "").strip()

        self.questions = {q["id"]: q for q in self.flow.get("questions", [])}
        self.queue = [qid for level in self.flow.get("order", []) for qid in level]
        self.multi = len(self.questions) > 1

        self.answers = {}          # qid -> text shown to the user
        self.asked = {}            # qid -> the question actually sent
        self.failed = []           # qids that produced nothing
        self.clarified = set()     # qids that already paused for a clarification
        self.position = 0          # index into self.queue
        self.stopped_reason = ""

    # ---- public -------------------------------------------------

    def start(self) -> dict:
        return self._walk()

    def resume(self) -> dict:
        """Call after an HS domain pick. Retries the same node from the top."""
        return self._walk()

    def supply_answer(self, text: str) -> dict:
        """Record the paused node's answer and carry on.

        Call this after a clarification: the caller sent the user's reply to the
        engine, got the real answer back, and passes that text in here. An empty
        text means the node produced nothing, which follows the usual
        empty-node rules.
        """
        if self.position >= len(self.queue):
            return self._done()

        qid = self.queue[self.position]
        text = (text or "").strip()

        if text:
            self.answers[qid] = text
            _log(f"{qid} answered after a clarification")
        else:
            self.failed.append(qid)
            blocked = dependants_of(self.flow, qid)
            if blocked:
                self.stopped_reason = (
                    "Nothing came back for that part, and the rest of your "
                    "question depends on it, so I stopped there."
                )
                _log(f"{qid} empty after a clarification and {blocked} depend on it")
                return self._stopped()
            _log(f"{qid} empty after a clarification, nothing depends on it")

        self.position += 1
        return self._walk()

    # ---- the walk -----------------------------------------------

    def _walk(self) -> dict:
        while self.position < len(self.queue):
            qid = self.queue[self.position]
            node = self.questions[qid]

            question = self._prepare(node)
            self.asked[qid] = question

            outcome = self._run_node(node, question)

            if outcome["pending"]:
                _log(f"{qid} paused for an HS domain pick")
                return {"status": "needs_hs_pick", "qid": qid,
                        "question": question, "answer": "",
                        "answers": dict(self.answers)}

            if outcome.get("needs_answer"):
                # one clarification per node; a second one is treated as the answer
                self.clarified.add(qid)
                _log(f"{qid} paused for a clarification")
                return {"status": "needs_answer", "qid": qid,
                        "question": question, "answer": "",
                        "answers": dict(self.answers)}

            if outcome["text"]:
                self.answers[qid] = outcome["text"]
            else:
                self.failed.append(qid)
                blocked = dependants_of(self.flow, qid)
                if blocked:
                    self.stopped_reason = (
                        f"Nothing came back for the first part, and the rest of "
                        f"your question depends on it, so I stopped there."
                    )
                    _log(f"{qid} empty and {blocked} depend on it — stopping")
                    return self._stopped()
                _log(f"{qid} empty, nothing depends on it — carrying on")

            self.position += 1

        return self._done()

    # ---- one node -----------------------------------------------

    def _prepare(self, node: dict) -> str:
        """Substitute earlier answers into a dependent question."""
        deps = [d for d in node.get("depends_on", []) if d in self.answers]
        if not deps:
            return node["text"]

        answers = "\n\n".join(
            f"{d} asked: {self.asked.get(d, '')}\n{d} answered: "
            f"{self.answers[d][:ANSWER_CHARS]}"
            for d in deps
        )
        prompt = PROMPT_REWRITE.format(answers=answers, question=node["text"])

        try:
            parsed = json.loads(_chat(prompt, REWRITE_MAX_TOKENS, as_json=True))
            rewritten = str(parsed.get("question") or "").strip()
            if rewritten:
                _log(f"{node['id']} rewritten", rewritten)
                return rewritten
        except Exception as exc:
            print(f"connector: rewrite of {node['id']} failed:",
                  type(exc).__name__, exc)

        return node["text"]

    def _run_node(self, node: dict, question: str) -> dict:
        """Dispatch one node. Returns {'text', 'pending', 'needs_answer'}."""
        route = node.get("route", "TRADE")
        _log(f"{node['id']} {route}", question)

        try:
            if route == "PERSONAL":
                fn = self.handlers.get("personal")
                return {"text": (fn(question) if fn else "").strip(),
                        "pending": False, "needs_answer": False}

            if route == "WEB" and self.multi:
                # grounding comes from the TRADE nodes this one depends on
                fn = self.handlers.get("web")
                return {"text": (fn(question) if fn else "").strip(),
                        "pending": False, "needs_answer": False}

            # TRADE, and a single-node WEB flow: the engine runs first.
            #
            # The full node is now made available to the trade handler so the
            # new Trade Mini Connector can read node["trade_decision"].
            # Legacy one-argument handlers remain supported.
            fn = self.handlers.get("trade")
            result = _call_trade_handler(
                fn,
                question,
                node,
            )
            if result.get("pending"):
                return {"text": "", "pending": True, "needs_answer": False}

            # the engine asked a question back. Pause once per node; if it asks
            # again after the user answered, take the question as the answer and
            # let the flow finish rather than looping.
            if result.get("needs_answer") and node["id"] not in self.clarified:
                return {"text": "", "pending": False, "needs_answer": True}

            text = (result.get("text") or "").strip()

            if route == "WEB" or not result.get("ok"):
                web = self.handlers.get("web")
                extra = (web(question) if web else "").strip()
                if extra:
                    text = f"{text}\n\n{extra}".strip() if text else extra

            return {"text": text, "pending": False, "needs_answer": False}

        except Exception as exc:
            print(f"connector: {node['id']} handler failed:",
                  type(exc).__name__, exc)
            return {"text": "", "pending": False, "needs_answer": False}

    # ---- finishing ----------------------------------------------

    def _combine(self) -> str:
        if not self.answers:
            return "Nothing came back for that. Try rephrasing it."

        # Always run the final presentation pass, even for a single answered
        # node. Previously single-node flows returned the raw engine text directly,
        # which allowed inconsistent tone/Markdown/spacing to reach the UI.
        blocks = []
        for qid in self.queue:
            if qid in self.answers:
                blocks.append(f"QUESTION: {self.asked.get(qid, '')}\n"
                              f"ANSWER: {self.answers[qid]}")
            elif qid in self.failed:
                blocks.append(f"QUESTION: {self.asked.get(qid, '')}\n"
                              f"ANSWER: (nothing came back)")

        prompt = PROMPT_COMBINE.format(
            question=self.original_question or "the user's question",
            answers="\n\n".join(blocks),
        )
        try:
            combined = _chat(prompt, COMBINE_MAX_TOKENS)
            if combined:
                return _clean_final_answer(combined)
        except Exception as exc:
            print("connector: combine failed:", type(exc).__name__, exc)

        # Fall back to the parts rather than losing the work. Even on fallback,
        # normalize only presentation artifacts; never rewrite the underlying facts.
        return _clean_final_answer(
            "\n\n".join(
                self.answers[qid]
                for qid in self.queue
                if qid in self.answers
            )
        )

    def _done(self) -> dict:
        return {
            "status": "done",
            "answer": self._combine(),
            "answers": dict(self.answers),
            "asked": dict(self.asked),
            "failed": list(self.failed),
            "flow": self.flow,
        }

    def _stopped(self) -> dict:
        parts = [self.answers[qid] for qid in self.queue if qid in self.answers]
        answer = "\n\n".join(parts + [self.stopped_reason]) if parts \
            else self.stopped_reason
        return {
            "status": "stopped",
            "answer": _clean_final_answer(answer),
            "answers": dict(self.answers),
            "asked": dict(self.asked),
            "failed": list(self.failed),
            "flow": self.flow,
        }


# ───────────────────────── run directly ─────────────────────────
#
#   python -m summarizer.connector
#
# Stub handlers, so the real classifier, builder, rewrite and combine all run
# without touching Snowflake or sonar. Watch the rewritten questions, and the
# third query exercises the clarification pause.

if __name__ == "__main__":
    from summarizer.question_flow_builder import build_flow
    from summarizer.classifier import tag_flow

    MEMORY = {
        "user_product_info": (
            "ABC EXPORTS, Tirupur. Exports cotton men's t-shirts (HS 610910) and "
            "pullovers (HS 611020) to the USA, Spain and Hong Kong. Imports "
            "polyester fabric (HS 540752) from Ningbo, China. Ports: Chennai Sea, "
            "Nhava Sheva."
        ),
        "old_session_summary": "",
        "current_session_queries": [],
        "persona": "",
    }

    def stub_personal(q):
        print(f"      [personal] <- {q}")
        return f"(from the profile: {q})"

    def stub_trade(q, node=None):
        print(f"      [trade]    <- {q}")
        return {"text": "The user's largest import is polyester fabric, HS 540752, "
                        "from Ningbo, China.",
                "ok": True, "pending": False, "needs_answer": False}

    def stub_web(q):
        print(f"      [web]      <- {q}")
        return f"(from the web: {q})"

    HANDLERS = {"personal": stub_personal, "trade": stub_trade, "web": stub_web}

    QUERIES = [
        "Identify the product the user imports most and state which it is. Then "
        "recommend adjacent product categories for that product, and transhipment "
        "routes and destination markets worth entering.",
        "Who are the top buyers of HS 610910 in the last 24 months?",
    ]

    for query in QUERIES:
        print(f"\n{'='*70}\n{query[:90]}\n{'='*70}")
        flow = tag_flow(build_flow(query, MEMORY), MEMORY)
        run = FlowRun(flow, MEMORY, HANDLERS, original_question=query)
        out = run.start()
        print(f"\n  status: {out['status']}")
        print(f"\n  ANSWER:\n{out['answer']}\n")

    # ---- the clarification pause, with a trade stub that asks once ----
    print(f"\n{'='*70}\nCLARIFICATION PAUSE\n{'='*70}")

    state = {"asked": False}

    def stub_trade_asks(q, node=None):
        print(f"      [trade]    <- {q}")
        if not state["asked"]:
            state["asked"] = True
            print("      [trade]    -> asks a clarifying question")
            return {"text": "", "ok": False, "pending": False, "needs_answer": True}
        return {"text": "Germany, Netherlands and Canada show the strongest demand.",
                "ok": True, "pending": False, "needs_answer": False}

    flow = tag_flow(build_flow("Show me the best markets for cotton", MEMORY), MEMORY)
    run = FlowRun(flow, MEMORY,
                  {**HANDLERS, "trade": stub_trade_asks},
                  original_question="Show me the best markets for cotton")

    out = run.start()
    print(f"\n  status: {out['status']}  (expected needs_answer)")

    if out["status"] == "needs_answer":
        # the caller would show options, take the reply, send it to the engine,
        # and pass the engine's real answer back in
        out = run.supply_answer(
            "Germany, Netherlands and Canada show the strongest demand."
        )

    print(f"  status: {out['status']}  (expected done)")
    print(f"\n  ANSWER:\n{out['answer']}\n")