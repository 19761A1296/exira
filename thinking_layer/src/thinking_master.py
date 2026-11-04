# thinking_layer/src/thinking_master.py
"""
thinking_master: the master of the thinking layer.

Input : query (one sub-part of the user's question, from the decision layer) + engine_id
        + dependency_answers (optional: results of other parts this one depends on,
          e.g. {"sq1": {"text": ..., "ok": ...}} from the Trade Mini Connector)
Output: final_response dict (not connected to anything yet)

    query + engine_id
          │
          ▼
    1. temp_engine_id = copy of engine_id's session   (context only)
    2. read context from temp_engine_id: product / HS, user memory, recent turns
          │
          ▼
    ┌───────────────── loop, max THINKING_MAX_STEPS (5) ─────────────────┐
    │ 3. master LLM: next sub-question, or finish if results are enough │
    │ 4. think_tagger: web or text2sql                                   │
    │ 5. run text2sql_thinking / thinking_webtool                        │
    │ 6. response goes back to the master for the next decision          │
    └────────────────────────────────────────────────────────────────────┘
          │
          ▼
    7. think_connector: final text + 1–2 charts, from query + context + all responses
    8. delete temp_engine_id   (always, even on error)
          │
          ▼
    final_response

Every sub-question counts toward the limit, including rewrites of failed ones.
The master stops early when the results already answer the query.

    from thinking_layer.src.thinking_master import run
    final = run("How concentrated are coffee buyers in Germany?", engine_id)
    final = run("Among the markets from sq1, which have low buyer concentration?",
                engine_id, dependency_answers={"sq1": {"text": "...", "ok": True}})

final_response:
    query, engine_id, temp_engine_id
    text            final answer
    charts          [{"step", "visualization", "rows", "cols"}]  0–2 items
    sources         web URLs used
    steps           trace for the "Thinking progress" dropdown:
                    [{"n", "sub_question", "why", "tool", "tag_reason", "status",
                      "sql", "response", "row_count", "citations", "error", "seconds"}]
    steps_used, stopped_reason, error, seconds

Test from the project root:
    python -m thinking_layer.src.thinking_master -q "your question"
    python -m thinking_layer.src.thinking_master -q "..." --engine-id <id>        # existing session
    python -m thinking_layer.src.thinking_master -q "..." --hs 0901 --product coffee
    python -m thinking_layer.src.thinking_master -q "..." --json out.json

.env (optional)
    THINKING_MAX_STEPS            default 5
    THINKING_MASTER_MAX_TOKENS    default 2000
    THINKING_MASTER_ROWS_PER_STEP default 50   (rows of each text2sql result shown to the master)
    THINK_TAGGER_MAX_TOKENS, THINK_CONNECTOR_MAX_TOKENS, THINK_CONNECTOR_ROWS_PER_STEP
"""

import argparse
import copy
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from text2sql.session_store import STORE

from thinking_layer import thinking_webtool
from thinking_layer.text2sql_thinking import PROMPT_A_SKELETON_BIBLE_THINKING
from thinking_layer.text2sql_thinking import run as run_text2sql
from thinking_layer.text2sql_thinking.text2sql_thinking_tool import call_gemini, dbg, extract_json_object
from thinking_layer.src.think_connector import connect, format_context, format_dependencies, format_step
from thinking_layer.src.think_tagger import TOOL_WEB, tag

MAX_STEPS = int(os.getenv("THINKING_MAX_STEPS", "5"))
MASTER_MAX_TOKENS = int(os.getenv("THINKING_MASTER_MAX_TOKENS", "2000"))
MASTER_ROWS_PER_STEP = int(os.getenv("THINKING_MASTER_ROWS_PER_STEP", "50"))
RECENT_TURNS = 6
TEMP_PREFIX = "think-"

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"
STATUS_OUT_OF_SCOPE = "out_of_scope"


# =========================================================
# 1–2 / 8. TEMP ENGINE SESSION
# =========================================================
def create_temp_engine(engine_id: Optional[str]) -> str:
    """Copy the engine session into a new temp id. Returns the temp id."""
    temp_id = f"{TEMP_PREFIX}{uuid.uuid4().hex}"
    state = {}
    if engine_id:
        try:
            state = copy.deepcopy(STORE.load(engine_id) or {})
        except Exception as exc:
            dbg("THINKING_MASTER", f"could not read engine session {engine_id}: {exc}")
        if not state:
            dbg("THINKING_MASTER", f"engine session {engine_id} not found; continuing without context")
    try:
        STORE.save(temp_id, state)
    except Exception as exc:
        dbg("THINKING_MASTER", f"could not save temp engine session: {exc}")
    return temp_id


def read_context(temp_engine_id: str) -> Dict[str, Any]:
    try:
        state = STORE.load(temp_engine_id) or {}
    except Exception:
        state = {}
    focus = state.get("active_focus") or {}
    turns = state.get("conversation_last_10") or []
    return {
        "product_name": focus.get("product_name"),
        "hs_code": focus.get("hs_code"),
        "selected_hs_codes": list(focus.get("selected_hs_codes") or []),
        "product_hs_memory": dict(focus.get("product_hs_memory") or {}),
        "user_memory": focus.get("user_memory") or {},
        "recent_turns": [{"role": t.get("role"), "content": t.get("content")} for t in turns[-RECENT_TURNS:]],
    }


def delete_temp_engine(temp_engine_id: str) -> None:
    try:
        STORE.delete(temp_engine_id)
    except Exception as exc:
        dbg("THINKING_MASTER", f"could not delete temp engine session {temp_engine_id}: {exc}")


# =========================================================
# 3. MASTER DECISION
# =========================================================
THINKING_MASTER_PROMPT = """You are the master of Exira's thinking layer. You answer a trade question that no
predefined analysis covers, by asking ONE sub-question at a time to the tools below and
reading each result before deciding the next sub-question.

USER QUERY
{query}

CONTEXT (from the user's current session)
{context}

EARLIER RESULTS (other parts of the user's question that this one builds on; use them
directly, do not ask for them again)
{dependencies}

TOOLS (a separate router picks the tool for each sub-question; you only write the question)
1) Trade shipment database (text2sql). Can answer:
<<<SKELETON
{skeleton}
SKELETON>>>
2) Web search. Answers facts not in shipment records: tariffs, duties, regulations,
   agreements, market size, production, macro figures, commodity/freight prices,
   exchange rates, company background, news and events. Trade and business only.

RESULTS SO FAR
{steps}

STEPS LEFT: {steps_left} of {max_steps}

HOW TO WRITE A SUB-QUESTION
- Make it complete on its own. Write in the product or HS code, geography, direction
  (imports into / exports from), time period and metric. Take missing product / HS details
  from CONTEXT when the user query clearly refers to them.
- When a formula is needed, define it exactly, for example "share of the top 5 buyers in
  total trade value" or "HHI computed on each buyer's share of total trade value".
  Prefer one database question that computes the whole formula over several raw pulls.
- Ask for one clear piece of information per sub-question.
- Use names and numbers from earlier results (EARLIER RESULTS and RESULTS SO FAR) when the
  next question depends on them, for example list the markets from EARLIER RESULTS by name.

HOW TO HANDLE RESULTS
- A failed or empty result: ask a rewritten version that is different (different wording,
  broader period, product name instead of HS code, simpler metric). Never repeat the same
  sub-question.
- A web result marked out_of_scope: do not ask the web for that again.
- Every sub-question, including a rewrite, uses one step.

WHEN TO FINISH
- Finish as soon as the results are enough to answer the user query. Do not use steps you
  do not need.
- EARLIER RESULTS are usually short summaries (for example only the top few rows), not the
  full data. If the query asks for a metric (concentration, HHI, top-N share, ratio, rate,
  ranking by risk) and that exact number is not stated in EARLIER RESULTS, compute it with
  at least one database sub-question before finishing.
- Finish if further steps would not add anything useful.
- With 1 step left, ask only the single most important missing piece, or finish.

OUTPUT
Return only JSON, no other text. Either:
{"action": "ask", "sub_question": "<the sub-question>", "reason": "<why, one sentence>"}
or:
{"action": "finish", "reason": "<why the results are enough, one sentence>"}"""


def _format_steps_for_master(steps: List[Dict[str, Any]]) -> str:
    if not steps:
        return "(none yet)"
    return "\n\n".join(format_step(s, MASTER_ROWS_PER_STEP) for s in steps)


def decide_next(query: str, context: Dict[str, Any], steps: List[Dict[str, Any]],
                dependency_answers: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return {"action": "ask"|"finish", "sub_question", "reason", "fallback"}."""
    prompt = (THINKING_MASTER_PROMPT
              .replace("{query}", query)
              .replace("{context}", format_context(context))
              .replace("{dependencies}", format_dependencies(dependency_answers))
              .replace("{skeleton}", PROMPT_A_SKELETON_BIBLE_THINKING)
              .replace("{steps}", _format_steps_for_master(steps))
              .replace("{steps_left}", str(MAX_STEPS - len(steps)))
              .replace("{max_steps}", str(MAX_STEPS)))
    try:
        data = extract_json_object(call_gemini(prompt, max_tokens=MASTER_MAX_TOKENS, temperature=0.0))
        action = str(data.get("action", "")).strip().lower()
        sub_q = str(data.get("sub_question", "")).strip()
        reason = str(data.get("reason", "")).strip()
        if action == "ask" and sub_q:
            return {"action": "ask", "sub_question": sub_q, "reason": reason, "fallback": False}
        if action == "finish":
            return {"action": "finish", "sub_question": "", "reason": reason, "fallback": False}
        raise ValueError(f"unusable master output: {data}")
    except Exception as exc:
        # nothing asked yet → ask the query itself; otherwise finish with what we have
        if not steps:
            return {"action": "ask", "sub_question": query, "fallback": True,
                    "reason": f"master fallback, asking the query directly ({type(exc).__name__})"}
        return {"action": "finish", "sub_question": "", "fallback": True,
                "reason": f"master fallback, finishing with current results ({type(exc).__name__})"}


# =========================================================
# 5–6. RUN ONE SUB-QUESTION
# =========================================================
def _from_text2sql(out: Dict[str, Any]) -> Dict[str, Any]:
    rows = out.get("rows") or []
    if out.get("stage") == "success":
        status = STATUS_OK if rows else STATUS_EMPTY
    else:
        status = STATUS_FAILED
    return {
        "status": status,
        "response": out.get("narrative", ""),
        "sql": out.get("sql", ""),
        "rows": rows,
        "cols": out.get("cols") or [],
        "visualization": out.get("visualization") or {},
        "citations": [],
        "error": out.get("error") or "",
    }


def _from_web(out: Dict[str, Any]) -> Dict[str, Any]:
    stage = out.get("stage")
    status = {"success": STATUS_OK, "out_of_scope": STATUS_OUT_OF_SCOPE}.get(stage, STATUS_FAILED)
    return {
        "status": status,
        "response": out.get("answer", ""),
        "sql": "",
        "rows": [],
        "cols": [],
        "visualization": {},
        "citations": out.get("citations") or [],
        "error": out.get("error") or "",
    }


def run_step(n: int, decision: Dict[str, Any]) -> Dict[str, Any]:
    started = time.time()
    sub_q = decision["sub_question"]
    tagged = tag(sub_q)

    try:
        if tagged["tool"] == TOOL_WEB:
            result = _from_web(thinking_webtool.run(sub_q))
        else:
            result = _from_text2sql(run_text2sql(sub_q))
    except Exception as exc:                      # both tools are no-raise; this is a last guard
        result = {"status": STATUS_FAILED, "response": "", "sql": "", "rows": [], "cols": [],
                  "visualization": {}, "citations": [], "error": f"{type(exc).__name__}: {exc}"}

    step = {
        "n": n,
        "sub_question": sub_q,
        "why": decision.get("reason", ""),
        "tool": tagged["tool"],
        "tag_reason": tagged["reason"],
        **result,
        "seconds": round(time.time() - started, 2),
    }
    dbg("THINKING_MASTER STEP", f"{n} [{step['tool']}] {step['status']}: {sub_q}")
    return step


def _trace(step: Dict[str, Any]) -> Dict[str, Any]:
    """Step as shown in the 'Thinking progress' dropdown (no full row set)."""
    return {
        "n": step["n"],
        "sub_question": step["sub_question"],
        "why": step["why"],
        "tool": step["tool"],
        "tag_reason": step["tag_reason"],
        "status": step["status"],
        "sql": step["sql"],
        "response": step["response"],
        "row_count": len(step.get("rows") or []),
        "citations": step["citations"],
        "error": step["error"],
        "seconds": step["seconds"],
    }


# =========================================================
# ENTRY POINT
# =========================================================
def run(query: str, engine_id: Optional[str],
        dependency_answers: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Thinking layer for one query. Never raises; the temp engine session is always deleted."""
    started = time.time()
    query = (query or "").strip()
    final = {
        "query": query, "engine_id": engine_id, "temp_engine_id": None,
        "text": "", "charts": [], "sources": [], "steps": [],
        "steps_used": 0, "stopped_reason": "", "error": "", "seconds": 0.0,
    }
    if not query:
        final.update(text="No question given.", error="empty query")
        return final

    temp_engine_id = create_temp_engine(engine_id)
    final["temp_engine_id"] = temp_engine_id
    steps: List[Dict[str, Any]] = []

    try:
        context = read_context(temp_engine_id)
        dbg("THINKING_MASTER CONTEXT", format_context(context))

        stopped = f"step limit reached ({MAX_STEPS})"
        while len(steps) < MAX_STEPS:
            decision = decide_next(query, context, steps, dependency_answers)
            dbg("THINKING_MASTER DECISION", decision)
            if decision["action"] == "finish":
                stopped = decision["reason"] or "master finished"
                break
            steps.append(run_step(len(steps) + 1, decision))

        connected = connect(query, context, steps, dependency_answers)
        final.update(text=connected["text"], charts=connected["charts"], sources=connected["sources"],
                     error=connected["error"], stopped_reason=stopped)
    except Exception as exc:
        final.update(text="Sorry, I could not complete this analysis.",
                     error=f"{type(exc).__name__}: {exc}", stopped_reason="error")
    finally:
        delete_temp_engine(temp_engine_id)

    final.update(steps=[_trace(s) for s in steps], steps_used=len(steps),
                 seconds=round(time.time() - started, 2))
    return final


# =========================================================
# CLI
# =========================================================
LINE = "─" * 72


def _make_test_engine(hs: Optional[str], product: Optional[str]) -> str:
    """A fresh engine session for testing, optionally with HS / product scope."""
    from text2sql.text2sql_tool import start_session      # only needed for the CLI
    sid = start_session()["session_id"]
    if hs or product:
        state = STORE.load(sid)
        state["active_focus"]["hs_code"] = hs
        state["active_focus"]["product_name"] = product
        if hs:
            state["active_focus"]["selected_hs_codes"] = [hs]
        STORE.save(sid, state)
    return sid


def show(final: Dict[str, Any]) -> None:
    print(f"\n{LINE}\nQUERY\n{LINE}\n{final['query']}")
    for s in final["steps"]:
        print(f"\n{LINE}\nSTEP {s['n']}  [{s['tool']}]  {s['status']}  ({s['seconds']}s)\n{LINE}")
        print(f"Sub-question: {s['sub_question']}")
        print(f"Why: {s['why']}")
        print(f"Tagged: {s['tag_reason']}")
        if s["sql"]:
            print(f"SQL:\n{s['sql']}")
        if s["tool"] == "text2sql":
            print(f"Rows: {s['row_count']}")
        if s["response"]:
            print(f"Response:\n{s['response']}")
        if s["error"]:
            print(f"Error: {s['error']}")
    print(f"\n{LINE}\nSTOPPED: {final['stopped_reason']}   "
          f"(steps used {final['steps_used']}, {final['seconds']}s)\n{LINE}")
    print(f"\n{LINE}\nFINAL ANSWER\n{LINE}\n{final['text']}")
    if final["charts"]:
        print(f"\nCharts: " + ", ".join(
            f"step {c['step']} {c['visualization'].get('type')} ({c['visualization'].get('title')})"
            for c in final["charts"]))
    if final["sources"]:
        print("\nSources:\n" + "\n".join(f"- {u}" for u in final["sources"]))
    if final["error"]:
        print(f"\nError: {final['error']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Thinking layer tester")
    ap.add_argument("-q", "--question", help="run one question and exit")
    ap.add_argument("--engine-id", help="existing engine session id (needs Redis to share with the app)")
    ap.add_argument("--hs", help="fresh test session only: set this HS code as current scope")
    ap.add_argument("--product", help="fresh test session only: set this product as current scope")
    ap.add_argument("--json", help="save the full final_response to this file")
    args = ap.parse_args()

    test_engine = None
    engine_id = args.engine_id
    if not engine_id:
        test_engine = _make_test_engine(args.hs, args.product)
        engine_id = test_engine
        print(f"(created fresh engine session {engine_id})")

    def ask(q: str) -> None:
        final = run(q, engine_id)
        show(final)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(final, f, indent=2, ensure_ascii=False, default=str)
            print(f"\nSaved full result to {args.json}")

    try:
        if args.question:
            ask(args.question)
            return
        print("thinking layer — type a question, or 'exit' to quit.")
        while True:
            try:
                q = input("\nQuestion: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if q.lower() in {"exit", "quit", "q"}:
                break
            if q:
                ask(q)
    finally:
        if test_engine:
            delete_temp_engine(test_engine)


if __name__ == "__main__":
    main()
