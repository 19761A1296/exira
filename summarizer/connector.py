# summarizer/connector.py
"""Walks a tagged flow, runs each sub-question, and combines the answers.

    analyzer ──► question_flow_builder ──► classifier.tag_flow ──► connector

    run = FlowRun(flow, memory, handlers)
    out = run.start()

    while out["status"] == "needs_hs_pick":
        # the caller shows the engine's domain options and sends the user's pick
        # to the engine itself, then:
        out = run.resume()

    out["status"] == "done"     -> out["answer"] is the combined reply
    out["status"] == "stopped"  -> out["answer"] explains why it stopped

FlowRun is resumable on purpose. The engine can interrupt a node to ask which
HS industry a product belongs to, and in Streamlit that pause has to survive a
rerun, so the object is kept in session state rather than the call stack.

HANDLERS — injected by the caller, so this module imports no engine and no web.

    personal(question) -> str

    trade(question)    -> {"text": str,        what to show and pass on
                           "ok": bool,         True only if data came back
                           "pending": bool}    True if the engine wants an HS pick

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

import json
import os

import requests
from dotenv import load_dotenv

from summarizer.question_flow_builder import dependants_of

load_dotenv()

URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.getenv("MODEL_CONNECTOR")
REWRITE_MAX_TOKENS = int(os.getenv("CONNECTOR_REWRITE_MAX_TOKENS", "400"))
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

Their message held several questions. Each was answered separately. Combine the
answers into ONE reply.

RULES
- Use only what the answers below contain. Add no figures, names or claims of
  your own.
- Follow the user's own order. Answer what they asked first, first.
- Keep names, companies, HS codes, ports, countries and numbers exactly as the
  answers wrote them.
- Where one answer builds on another, say so in plain words: name the product or
  the market that the later part rests on, so the user can check the premise.
- If a part could not be answered, say so in one short clause and move on. Do not
  dwell on it and do not apologise at length.
- Do not name the sources, do not mention sub-questions, q1, q2, or any process.
  Write it as one answer from one assistant.
- Plain prose. No headings, no markdown. As many short paragraphs as the question
  genuinely has parts, and no more.

THE USER'S QUESTION
{question}

THE ANSWERS
{answers}

Write the combined answer now."""


# ───────────────────────── llm helper ─────────────────────────

def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
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
        self.position = 0          # index into self.queue
        self.stopped_reason = ""

    # ---- public -------------------------------------------------

    def start(self) -> dict:
        return self._walk()

    def resume(self) -> dict:
        """Call after the caller has handled a pause. Retries the same node."""
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
        """Dispatch one node. Returns {'text', 'pending'}."""
        route = node.get("route", "TRADE")
        _log(f"{node['id']} {route}", question)

        try:
            if route == "PERSONAL":
                fn = self.handlers.get("personal")
                return {"text": (fn(question) if fn else "").strip(),
                        "pending": False}

            if route == "WEB" and self.multi:
                # grounding comes from the TRADE nodes this one depends on
                fn = self.handlers.get("web")
                return {"text": (fn(question) if fn else "").strip(),
                        "pending": False}

            # TRADE, and a single-node WEB flow: the engine runs first
            fn = self.handlers.get("trade")
            result = fn(question) if fn else {"text": "", "ok": False, "pending": False}
            if result.get("pending"):
                return {"text": "", "pending": True}

            text = (result.get("text") or "").strip()

            if route == "WEB" or not result.get("ok"):
                web = self.handlers.get("web")
                extra = (web(question) if web else "").strip()
                if extra:
                    text = f"{text}\n\n{extra}".strip() if text else extra

            return {"text": text, "pending": False}

        except Exception as exc:
            print(f"connector: {node['id']} handler failed:",
                  type(exc).__name__, exc)
            return {"text": "", "pending": False}

    # ---- finishing ----------------------------------------------

    def _combine(self) -> str:
        if not self.answers:
            return "Nothing came back for that. Try rephrasing it."

        if len(self.answers) == 1 and not self.failed:
            return next(iter(self.answers.values()))

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
                return combined
        except Exception as exc:
            print("connector: combine failed:", type(exc).__name__, exc)

        # fall back to the parts rather than losing the work
        return "\n\n".join(self.answers[qid] for qid in self.queue
                           if qid in self.answers)

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
            "answer": answer,
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
# without touching Snowflake or sonar. Watch the rewritten questions.

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

    def stub_trade(q):
        print(f"      [trade]    <- {q}")
        # pretend the engine found the top import
        return {"text": "The user's largest import is polyester fabric, HS 540752, "
                        "from Ningbo, China.",
                "ok": True, "pending": False}

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