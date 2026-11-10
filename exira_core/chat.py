# exira_core/chat.py
"""
Chat logic: one user message -> resolve -> flow -> trade / web / personal
handlers -> answer, plus the HS-scope and clarification pauses.

Moved out of app_ui_v2_stable.py unchanged, except:
  st.warning(...)          -> warn(...)      (returned to the UI as a notice)
  with st.spinner(...):    -> with spinner(...)  (reported as job progress)
  S                        -> the current workspace (exira_core.state.S)
No Streamlit is imported here.
"""

import json
import re

from engine_adapter import ENGINE, HS2_DOMAIN_MAP
from hs_memory import collect_hs_from_memory

from database import info_database as db
from persona.tweaked_exira import build_and_save_persona, load_persona, persona_to_text
from summarizer.session_summarizer import query_summary, session_summary
from summarizer.classifier import classify_query, tag_flow
from summarizer.question_flow_builder import build_flow
from summarizer.connector import FlowRun
from summarizer.analyzer import resolve_query
from summarizer.web_search import web_search, as_context
from summarizer.memory_answer import answer_from_memory
from summarizer.clarify import options_from, answers_question, rejection_message
from summarizer.answer_recall import summarise_answers, capture, preview
from trade_decision.trade_decision_orchestrator import attach_trade_decisions
from trade_decision.trade_mini_connector import execute_trade_parent
from thinking_layer.src.thinking_master import run as thinking_run

from exira_core.state import S, warn, spinner, progress

MAX_SESSION_QUERIES = 5


def build_memory() -> dict:

    return {
        "user_product_info":
            S.user_product_info or "",

        "old_session_summary":
            S.old_session or "",

        "current_session_queries":
            S.append_queries[
                -MAX_SESSION_QUERIES:
            ],

        "persona":
            persona_to_text(S.persona)
            if S.persona
            else "",
    }


def consume(
    resp: dict
) -> dict:

    out = {
        "texts": [],
        "confirm": None,
        "options": [],
        "trace": None,
        "state": resp.get(
            "state"
        )
    }

    for m in resp.get(
        "messages",
        []
    ):

        kind = m.get(
            "type"
        )

        if (
            kind
            == "domain_selector"
        ):

            continue

        if kind in (
            "answer",
            "clarification"
        ):

            out[
                "texts"
            ].append(
                m["content"]
            )

            if m.get(
                "data"
            ):

                out[
                    "trace"
                ] = m[
                    "data"
                ]

        elif (
            kind
            == "confirm"
        ):

            out[
                "confirm"
            ] = m[
                "data"
            ][
                "resolved_query"
            ]

        elif (
            kind
            == "followup"
        ):

            out[
                "options"
            ] = m[
                "data"
            ][
                "items"
            ]

    return out


def push_turn(
    role: str,
    content: str,
    trace=None
):

    S.turns.append(
        {
            "role":
                role,

            "content":
                content,

            "trace":
                trace
        }
    )


def sync_scope(
    announce: bool = True
):

    scope = (
        ENGINE.current_scope(
            S.sid
        )
    )

    label = scope[
        "label"
    ]

    if (
        not label
        or label == S.scope_label
    ):

        return

    previous = (
        S.scope_label
    )

    S.scope_label = (
        label
    )

    S.hs_code = (
        scope[
            "hs_code"
        ]
    )

    if (
        not S.scope_history
        or S.scope_history[-1]
        != label
    ):

        S.scope_history.append(
            label
        )

    if scope[
        "hs_code"
    ]:

        remember_hs(
            scope[
                "hs_code"
            ]
        )

    if announce:

        note = (
            f"Scope: {label}"
            if not previous
            else
            f"Scope changed: "
            f"{previous} → {label}"
        )

        push_turn(
            "system",
            note
        )


def apply_engine_response(
    resp: dict
):

    parsed = consume(
        resp
    )

    for t in parsed[
        "texts"
    ]:

        push_turn(
            "assistant",
            t,
            parsed["trace"]
        )

    S.options = (
        parsed["options"]
        or S.options
    )

    S.selecting = (
        ENGINE.scope_pending(
            S.sid
        )
    )

    S.pending_confirm = (
        parsed["confirm"]
    )

    sync_scope()

    return parsed


def record_query(
    text: str
):

    S.append_queries.append(
        text
    )

    if (
        len(S.append_queries)
        > MAX_SESSION_QUERIES
    ):

        try:

            S.append_queries = [
                query_summary(
                    S.append_queries
                )[0]
            ]

        except Exception as exc:

            warn(
                f"Query summary failed: "
                f"{exc}"
            )

    if S.session_id is not None:

        S.persona_turns.append(
            {
                "role":
                    "user",

                "content":
                    text
            }
        )

        try:

            S.persona = (
                build_and_save_persona(
                    S.session_id,
                    S.persona_turns,
                    recent_turns=1,
                    persona=S.persona
                )
            )

        except Exception as exc:

            warn(
                f"Persona update failed: "
                f"{exc}"
            )


def attach_meta():

    if (
        not S.turns
        or S.turns[-1]["role"]
        != "assistant"
    ):

        return

    tr = (
        S.turns[-1].get(
            "trace"
        )
        or {}
    )

    tr[
        "route"
    ] = S.last_route

    if S.last_resolution:

        tr[
            "resolution"
        ] = (
            S.last_resolution
        )

    S.turns[-1][
        "trace"
    ] = tr


def trade_succeeded() -> bool:

    if (
        not S.turns
        or S.turns[-1]["role"]
        != "assistant"
    ):

        return False

    trace = (
        S.turns[-1].get(
            "trace"
        )
        or {}
    )

    stage = (
        trace.get(
            "meta"
        )
        or {}
    ).get(
        "stage"
    )

    if (
        stage
        and stage
        != "success"
    ):

        return False

    if trace.get(
        "error"
    ):

        return False

    if "rows" in trace:

        return bool(
            trace.get(
                "rows"
            )
        )

    return True


def maybe_web(
    query: str,
    memory: dict,
    route: str
):

    data_ok = (
        trade_succeeded()
    )

    if (
        route != "WEB"
        and data_ok
    ):

        return

    if data_ok:

        heading = (
            "Additional context "
            "from the web"
        )

    else:

        heading = (
            "Your trade records had "
            "nothing to answer this, "
            "so here is what the web says"
        )

    with spinner(
        "Looking this up on the web..."
    ):

        result = web_search(
            query,
            memory
        )

    block = as_context(
        result,
        heading
    )

    if not block:

        if not data_ok:

            push_turn(
                "assistant",

                "Nothing came back "
                "from your trade records "
                "or the web for that. "
                "Try rephrasing it.",

                {
                    "route":
                        route,

                    "web":
                        result
                }
            )

        return

    push_turn(
        "assistant",
        block,
        {
            "route":
                "WEB",

            "web":
                result,

            "data_ok":
                data_ok,

            "resolution":
                S.last_resolution
        }
    )


def chat_history() -> list:

    out = []

    for t in S.turns:

        if (
            t["role"]
            == "user"
        ):

            out.append(
                {
                    "role":
                        "USER",

                    "content":
                        t["content"]
                }
            )

        elif (
            t["role"]
            == "assistant"
        ):

            out.append(
                {
                    "role":
                        "EXIRA",

                    "content":
                        t["content"]
                }
            )

    return out[-12:]



RESOLVED_CHARS = 600


def memory_entry(text: str, sent: str) -> str:
    """What goes into current_session_queries: the user's own words only."""
    return (text or "").strip()


def trace_ok(trace: dict) -> bool:
    """True only when the engine actually returned rows."""
    trace = trace or {}
    stage = (trace.get("meta") or {}).get("stage")
    if stage and stage != "success":
        return False
    if trace.get("error"):
        return False
    if "rows" in trace:
        return bool(trace.get("rows"))
    return True


# ─────────────── handlers the connector calls ───────────────

def ui_personal(question: str) -> str:
    progress("Checking your company profile…")
    return answer_from_memory(question, build_memory())


def ui_web(question: str) -> str:
    with spinner("Looking this up on the web..."):
        result = web_search(question, build_memory())
    S.last_web = result
    return as_context(result, "From the web")


def _trade_task_query(task: dict) -> str:
    """Read the executable query from a Trade Mini Connector task."""
    return str(
        task.get("resolved_query")
        or task.get("query")
        or task.get("question")
        or task.get("text")
        or ""
    ).strip()


def _text2sql_task_handler(task: dict) -> dict:
    """
    Execute ONE atomic Trade task through the existing Text2SQL engine.

    This is the old ui_trade engine behavior, moved behind the new
    Trade Router / Mini Connector architecture.
    """
    question = _trade_task_query(task)

    if not question:
        return {
            "text": "",
            "ok": False,
            "pending": False,
            "needs_answer": False,
            "error": "TEXT2SQL task has an empty query.",
        }

    resp = ENGINE.send(S.sid, question)

    print(
        "RAW TEXT2SQL ENGINE RESP:",
        json.dumps(resp, default=str)[:],
    )

    parsed = consume(resp)

    if parsed["options"]:
        S.options = parsed["options"]

    S.selecting = ENGINE.scope_pending(S.sid)
    sync_scope()

    if S.selecting:
        # Show the domain question so the user knows what is being asked.
        for t in parsed["texts"]:
            push_turn("assistant", t)

        return {
            "text": "",
            "ok": False,
            "pending": True,
            "needs_answer": False,
        }
    
    if task.get("subquery_id") and ENGINE.clarification_pending(S.sid):
        asked = "\n".join(t for t in parsed["texts"] if t).strip()
        opts = options_from(asked) or (parsed["options"] or [])[:3]
        pick = opts[0] if opts else question
        print(f"[TEXT2SQL] {task.get('task_id')} clarification auto-answered: {pick}")
        parsed = consume(ENGINE.send(S.sid, pick))
        sync_scope()

    if ENGINE.clarification_pending(S.sid):
        asked = "\n".join(
            t
            for t in parsed["texts"]
            if t
        ).strip()

        for t in parsed["texts"]:
            push_turn("assistant", t)

        opts = options_from(asked)

        if not opts:
            opts = (
                parsed["options"]
                or S.options
            )[:3]

        if not opts:
            return {
                "text": asked,
                "ok": False,
                "pending": False,
                "needs_answer": False,
            }

        S.pending_clarify = {
            "qid": "",
            "question": asked,
            "options": opts,
        }

        return {
            "text": "",
            "ok": False,
            "pending": False,
            "needs_answer": True,
        }

    trace = parsed["trace"]

    if parsed["confirm"]:
        done = ENGINE.confirm(
            S.sid,
            "proceed",
        )

        parsed = consume(done)

        if parsed["options"]:
            S.options = parsed["options"]

        sync_scope()

        trace = (
            parsed["trace"]
            or trace
        )

    if trace:
        S.last_trade_trace = trace

        S.flow_traces.append(
            {
                "question": question,
                "task_id": str(
                    task.get("task_id")
                    or task.get("id")
                    or ""
                ),
                "trace": trace,
            }
        )

    answer_text = "\n".join(
        t
        for t in parsed["texts"]
        if t
    ).strip()

    return {
        "status": "FINAL",
        "text": answer_text,
        "ok": trace_ok(trace),
        "pending": False,
        "needs_answer": False,
        "data": trace,
        "generated_questions": [],
    }


def _thinking_task_handler(task: dict) -> dict:
    progress("Thinking through the question…")
    """
    THINKING executor: runs the Thinking Layer for one trade task.

        Decision Layer -> Mini Connector -> Trade Router -> HERE
            -> thinking_layer.src.thinking_master.run(query, S.sid, dependency_answers)

    The thinking master runs its own sub-question loop (max 5) and returns a
    final answer, so this always returns status FINAL. The full result (charts,
    steps) is kept in S.thinking_results and attached to the turn by finish_flow.
    """
    query = _trade_task_query(task)

    dependency_answers = (
        task.get("dependency_answers")
        or {}
    )

    final = thinking_run(
        query,
        S.sid,
        dependency_answers=dependency_answers,
    )

    print(
        "\n========== THINKING LAYER =========="
    )
    for step in final.get("steps") or []:
        print(
            f"  step {step['n']} [{step['tool']}] {step['status']}: "
            f"{step['sub_question']}"
        )
    print(
        f"  stopped: {final.get('stopped_reason')}  ({final.get('seconds')}s)"
    )
    if final.get("error"):
        print(f"  error: {final['error']}")
    print(
        "====================================\n"
    )

    S.thinking_results.append(
        {
            "task_id": (
                task.get("task_id")
                or task.get("id")
            ),
            **final,
        }
    )

    answered = any(
        step.get("status") == "ok"
        for step in final.get("steps") or []
    ) or (
        not final.get("steps")
        and bool(dependency_answers)
        and not final.get("error")
    )

    return {
        "status": "FINAL",
        "answer": final.get("text", ""),
        "ok": bool(final.get("text")) and answered,
        "data": {
            "thinking": True,
            "task_id": task.get("task_id") or task.get("id"),
            "stopped_reason": final.get("stopped_reason"),
            "steps_used": final.get("steps_used"),
        },
        "error": final.get("error") or "",
        "generated_questions": [],
    }


def ui_trade(
    question: str,
    node: dict = None,
) -> dict:
    """
    Execute one outer TRADE parent through the new Trade Mini Connector.

    Flow:
        FlowRun
            -> ui_trade(question, node)
            -> execute_trade_parent(...)
            -> trade_router.py
               -> TEXT2SQL
               -> THINKING

    The complete Decision Layer result is attached to node["trade_decision"]
    before FlowRun starts.
    """
    progress("Running trade analysis…")

    # ---------------------------------------------------------
    # Safety / backward-compatible fallback
    # ---------------------------------------------------------
    #
    # If this function is called outside the new FlowRun path,
    # there may be no full node yet. Preserve the old Text2SQL
    # behavior rather than crashing.
    # ---------------------------------------------------------

    if not isinstance(node, dict):
        return _text2sql_task_handler(
            {
                "task_id": "legacy-trade",
                "id": "legacy-trade",
                "query": question,
                "text": question,
                "route": "TRADE",
                "trade_executor": "TEXT2SQL",
            }
        )

    decision = node.get(
        "trade_decision"
    )

    if not isinstance(
        decision,
        dict,
    ):
        return _text2sql_task_handler(
            {
                **node,
                "query": question,
                "text": question,
                "resolved_query": question,
                "trade_executor": "TEXT2SQL",
            }
        )

    # ---------------------------------------------------------
    # Preserve FlowRun's rewritten runtime question.
    # ---------------------------------------------------------
    #
    # This matters when the outer Flow Builder node depends on
    # an earlier outer node. FlowRun may rewrite the question
    # using the earlier answer before dispatching it here.
    # ---------------------------------------------------------

    runtime_node = dict(node)

    runtime_node[
        "resolved_query"
    ] = question

    runtime_node[
        "query"
    ] = question

    runtime_node[
        "text"
    ] = question

    # ---------------------------------------------------------
    # Execute DIRECT / SPLIT through the Mini Connector.
    # ---------------------------------------------------------

    result = execute_trade_parent(
        parent_node=runtime_node,
        text2sql_handler=
            _text2sql_task_handler,
        thinking_handler=
            _thinking_task_handler,
    )

    S.last_trade_execution = result

    print(f"\n===== TRADE RESULT {result.get('parent_id')} · {result.get('decision_type')} =====")
    for part_id, part in (result.get("parts") or {}).items():
        print(f"  [{part_id}] {part.get('executor')} · ok={part.get('ok')}")
        print(f"  {str(part.get('text') or part.get('error') or '').strip()[:800]}")
    print("=" * 50 + "\n")

    return {
        "text": str(
            result.get(
                "text",
                "",
            )
            or ""
        ).strip(),

        "ok": bool(
            result.get(
                "ok",
                False,
            )
        ),

        "pending": bool(
            result.get(
                "pending",
                False,
            )
        ),

        "needs_answer": bool(
            result.get(
                "needs_answer",
                False,
            )
        ),

        "trade_execution":
            result,
    }


FLOW_HANDLERS = {"personal": ui_personal, "trade": ui_trade, "web": ui_web}


# ─────────────── the flow ───────────────

def last_assistant_text() -> str:
    """The newest assistant turn, for capturing the button path."""
    return next((t["content"] for t in reversed(S.turns)
                 if t["role"] == "assistant" and t["content"]), "")


def finish_flow(out: dict):
    """Called with a FlowRun result. Pushes the answer, or waits for a pick."""
    if out["status"] == "needs_hs_pick":
        # ui_trade already showed the question and the options; S.selecting is on
        return
    if out["status"] == "needs_answer":
        if S.pending_clarify:
            S.pending_clarify["qid"] = out.get("qid", "")
        return

    S.flow_run = None
    trace = dict(S.last_trade_trace or {})
    trace["route"] = S.last_route
    trace["resolution"] = S.last_resolution
    trace["flow"] = S.last_flow
    if S.last_trade_execution:
        trace["trade_execution"] = S.last_trade_execution
    trace["parts"] = out.get("answers") or {}
    trace["asked"] = out.get("asked") or {}

    # one entry per TRADE node, matched back to its question id
    asked = out.get("asked") or {}
    by_question = {v: k for k, v in asked.items()}
    charts = []
    for item in S.flow_traces:
        qid = by_question.get(item["question"])
        if qid:
            charts.append({"qid": qid, "trace": item["trace"]})
    charts.sort(key=lambda c: c["qid"])
    if len(charts) > 1:
        trace["charts"] = charts
    if S.last_web:
        trace["web"] = S.last_web
    if S.thinking_results:
        trace["thinking"] = S.thinking_results

    push_turn("assistant", out["answer"], trace)
    record_query(memory_entry(S.flow_text, S.flow_sent))
    capture(S.answers, S.flow_text, out["answer"])
    if S.session_id is not None:
        db.save_answer(S.session_id, S.flow_text, out["answer"], trace=trace)

def resume_clarification(reply: str):
    """Send the user's clarification answer back into the paused node."""
    pending = S.pending_clarify
    if not pending:
        return

    options = pending["options"]
    chosen = ""

    if reply.strip().isdigit() and 1 <= int(reply) <= len(options):
        chosen = options[int(reply) - 1]
    else:
        verdict = answers_question(pending["question"], reply)
        if verdict["ok"]:
            chosen = reply.strip()
        else:
            chosen = options[0]
            push_turn("system", rejection_message(verdict["reason"], chosen))

    S.pending_clarify = None
    push_turn("user", chosen)

    try:
        resp = ENGINE.send(S.sid, chosen)
        parsed = consume(resp)
        if parsed["options"]:
            S.options = parsed["options"]
        sync_scope()

        # the answer may have named a new product, which the engine scopes first
        S.selecting = ENGINE.scope_pending(S.sid)
        if S.selecting:
            for t in parsed["texts"]:
                push_turn("assistant", t)
            return          # the HS gate in handle_message takes it from here

        # we pause once per node only, so a second question is taken as the answer
        if ENGINE.clarification_pending(S.sid):
            asked_again = "\n".join(t for t in parsed["texts"] if t).strip()
            for t in parsed["texts"]:
                push_turn("assistant", t)
            if S.flow_run:
                finish_flow(S.flow_run.supply_answer(asked_again))
            return

        if parsed["confirm"]:
            done = ENGINE.confirm(S.sid, "proceed")
            parsed = consume(done)
            if parsed["options"]:
                S.options = parsed["options"]
            sync_scope()

        if parsed["trace"]:
            S.last_trade_trace = parsed["trace"]
            S.flow_traces.append({"question": chosen, "trace": parsed["trace"]})

        text = "\n".join(t for t in parsed["texts"] if t).strip()
        if S.flow_run:
            finish_flow(S.flow_run.supply_answer(text))
    except Exception as exc:
        S.flow_run = None
        push_turn("assistant", f"Error: {exc}")

def start_flow(text: str, sent: str, memory: dict):
    """Build, classify, apply trade decisions, and start a flow."""
    progress("Planning the analysis…")

    # Existing Flow Builder + high-level classifier.
    flow = tag_flow(
        build_flow(sent, memory),
        memory,
    )

    # New Trade Decision Layer.
    # PERSONAL / WEB nodes remain unchanged.
    # TRADE nodes receive trade_decision and, for DIRECT decisions,
    # trade_executor = TEXT2SQL or THINKING.
    flow = attach_trade_decisions(flow)

    # Temporary debug output while integrating the new architecture.
    print("\n===== TRADE DECISIONS =====")
    for node in flow.get("questions", []):
        d = node.get("trade_decision")
        if not d:
            continue
        if d.get("decision_type") == "SPLIT":
            print(f"  {node['id']}: SPLIT")
            for sq in d.get("subqueries") or []:
                print(f"     {sq['subquery_id']} -> {sq['execution_route']}: {sq['query']}")
        else:
            print(f"  {node['id']}: DIRECT -> {d.get('execution_route')}")
    print("===========================\n")
   

    S.last_flow = flow
    S.last_route = (
        flow["questions"][0].get("route", "TRADE")
        if len(flow["questions"]) == 1
        else "FLOW"
    )
    S.flow_text, S.flow_sent = text, sent
    S.last_trade_trace = None
    S.last_trade_execution = None
    S.flow_traces = []
    S.thinking_results = []
    S.last_web = None

    # Existing outer connector remains unchanged at this stage.
    S.flow_run = FlowRun(
        flow,
        memory,
        FLOW_HANDLERS,
        original_question=sent,
    )
    finish_flow(S.flow_run.start())


def ensure_engine_session() -> bool:
    """Make sure this chat's engine session still exists in the store.

    If it expired (idle past the TTL) or was removed, start a new one,
    save its id on the chat session and restore the HS scope.
    Returns True when the engine session was replaced.
    """
    old = S.get("sid")
    if ENGINE.exists(old):
        return False

    S.sid = ENGINE.start()
    if S.get("session_id") is not None:
        db.save_session(S.session_id, engine_sid=S.sid)
    ENGINE.inject_memory(S.sid, build_memory())
    if S.get("hs_code"):
        try:
            ENGINE.set_hs(S.sid, S.hs_code)
        except Exception:
            pass
    else:
        ENGINE.skip_setup(S.sid)

    # whatever the old engine session was waiting for is gone with it
    S.selecting = False
    S.pending_clarify = None
    S.pending_confirm = None
    S.flow_run = None

    print(f"[ENGINE RECOVER] session_id={S.get('session_id')} old={old} new={S.sid}")
    return True


def handle_message(text: str, from_list: bool = False):
    text = (text or "").strip()
    if not text:
        return

    # the user turn is already on screen; drop it from the history we analyse
    history = chat_history()
    while history and history[-1]["role"] == "USER":
        history.pop()

    ensure_engine_session()
    ENGINE.inject_memory(S.sid, build_memory())

    # ---- an HS domain pick, possibly in the middle of a flow ----
    if S.pending_clarify:
        resume_clarification(text)
        return
    if S.selecting:
        try:
            apply_engine_response(ENGINE.send(S.sid, text))
        except Exception as exc:
            push_turn("assistant", f"Error: {exc}")
            return
        if not S.selecting and S.flow_run is not None:
            try:
                finish_flow(S.flow_run.resume())
            except Exception as exc:
                S.flow_run = None
                push_turn("assistant", f"Error: {exc}")
        return

    memory = build_memory()

    # ---- suggested question is already standalone ----
    #
    # Previously this path called ENGINE.send() directly, which bypassed
    # Flow Builder -> Classifier -> Trade Decision Layer.
    # Suggested questions now use the same orchestration path as normal queries.
    if from_list:
        S.last_resolution = None
        sent = text

        try:
            start_flow(
                text=text,
                sent=sent,
                memory=memory,
            )
        except Exception as exc:
            S.flow_run = None
            push_turn(
                "assistant",
                f"Error: {exc}",
            )

        return

    # ---- resolve, then split, tag and walk ----
    progress("Understanding your question…")
    S.last_resolution = resolve_query(text, history, memory)

    if S.last_resolution["blocked"]:
        push_turn("assistant", S.last_resolution["message"],
                  {"route": "BLOCKED", "resolution": S.last_resolution})
        return

    # asking about an answer they already have: nothing downstream runs
    if S.last_resolution.get("recall"):
        S.last_route = "ANSWER_RECALL"
        S.last_flow = None
        answer = summarise_answers(text, S.answers,
                                   S.last_resolution.get("recall_scope", "last"))
        push_turn("assistant", answer,
                  {"route": "ANSWER_RECALL", "resolution": S.last_resolution})
        return

    if S.last_resolution["dropped_note"]:
        push_turn("system", S.last_resolution["dropped_note"])

    sent = S.last_resolution["resolved_query"] or text

    try:
        start_flow(text, sent, memory)
    except Exception as exc:
        S.flow_run = None
        push_turn("assistant", f"Error: {exc}")

def run_confirm(
    decision: str,
    sent: str = "",
    memory: dict = None,
    route: str = "TRADE"
):

    S.pending_confirm = None

    if ensure_engine_session():
        push_turn(
            "assistant",
            "This session was idle for too long, so the pending query was cleared. "
            "Please ask it again."
        )
        return

    try:

        apply_engine_response(
            ENGINE.confirm(
                S.sid,
                decision
            )
        )

    except Exception as exc:

        push_turn(
            "assistant",
            f"Error: {exc}"
        )

        return

    if decision == "proceed":

        attach_meta()

        if sent:

            maybe_web(
                sent,
                memory or build_memory(),
                route
            )

        last_user = next(
            (
                t["content"]
                for t in reversed(
                    S.turns
                )
                if t["role"]
                == "user"
            ),
            ""
        )

        if last_user:

            record_query(
                last_user
            )

            answer_text = last_assistant_text()
            capture(
                S.answers,
                last_user,
                answer_text
            )
            if S.session_id is not None:
                db.save_answer(S.session_id, last_user, answer_text, trace=(S.turns[-1].get("trace") if S.turns else None))


def remember_hs(
    code: str
):

    if any(
        c["code"] == code
        for c in S.candidates
    ):

        return

    S.candidates.append(
        {
            "code":
                code,

            "domain":
                HS2_DOMAIN_MAP.get(
                    code[:2].zfill(2),
                    "Unknown / Other"
                ),

            "source":
                "current_session",
        }
    )

    S.candidates.sort(
        key=lambda x:
            x["code"]
    )


def switch_scope(
    choice: str
):

    ensure_engine_session()

    code = re.sub(
        r"[.\-\s]",
        "",
        choice or ""
    )

    if re.fullmatch(
        r"\d{2,10}",
        code
    ):

        S.hs_code = code

        S.scope_label = (
            f"HS {code}"
        )

        remember_hs(
            code
        )

        ENGINE.set_hs(
            S.sid,
            code
        )

        S.selecting = False

        S.options = [
            f"Who are the top buyers "
            f"of HS {code} "
            f"in the last 24 months?",

            f"Which countries are showing "
            f"increasing demand for "
            f"HS {code} "
            f"in the last 24 months?",

            f"Which month does demand "
            f"for HS {code} usually peak "
            f"over the last 36 months?",
        ]

        push_turn(
            "system",
            f"Scope changed to HS {code}"
        )

    else:

        S.hs_code = None

        S.scope_label = (
            choice
        )

        push_turn(
            "user",
            choice
        )

        try:

            apply_engine_response(
                ENGINE.send(
                    S.sid,
                    choice
                )
            )

        except Exception as exc:

            push_turn(
                "assistant",
                f"Error: {exc}"
            )

    S.show_hs_panel = False


WELCOME_MESSAGE = (
    "Welcome! Ask about any product, HS code, market, buyer or supplier. "
    "There is no trade scope yet: it is set automatically from your first "
    "product question and keeps updating as you go."
)


def start_engine_session(
    opening: str
):

    S.sid = (
        ENGINE.start()
    )
    print(f"[NEW ENGINE] session_id={S.get('session_id')} engine_sid={S.sid}")

    # link the engine session to this chat so a resume can reattach to it
    if S.get("session_id") is not None:
        db.save_session(S.session_id, engine_sid=S.sid)

    ENGINE.inject_memory(
        S.sid,
        build_memory()
    )

    # no opening (new session without a scope): just greet; the first
    # product question sets the scope through the normal engine flow
    if not opening:
        ENGINE.skip_setup(S.sid)
        push_turn("assistant", WELCOME_MESSAGE)
        return

    try:

        apply_engine_response(
            ENGINE.send(
                S.sid,
                opening
            )
        )

    except Exception as exc:

        push_turn(
            "assistant",
            f"Error starting session: "
            f"{exc}"
        )
