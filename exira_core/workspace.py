# exira_core/workspace.py
"""
Everything a user can do outside the onboarding wizard, as plain functions,
plus view(): the JSON-ready snapshot a UI renders.

Moved from app_ui_v2_stable.py (page_* button handlers, begin_with,
open_company, resume_session, end_session). Logic unchanged; st.error /
st.warning became notices, and ownership checks were added so one user
can't open another user's company or session through the API.
"""

import json
import re

from engine_adapter import ENGINE, HS2_DOMAIN_MAP
from hs_memory import collect_hs_from_memory

from database import info_database as db
from persona.tweaked_exira import load_persona, persona_to_text
from summarizer.session_summarizer import query_summary, session_summary

from exira_core import chat
from exira_core.chat import (
    MAX_SESSION_QUERIES, build_memory, push_turn, remember_hs,
    start_engine_session, handle_message, switch_scope,
)
from exira_core.onboarding import MAX_ATTEMPTS, NARROW_THRESHOLD
from exira_core.state import S, UserError, error, warn

STAGES = ("login", "onboarding", "company_picker", "session_picker", "scope", "chat", "done")


# ───────────────────────── checks ─────────────────────────

def _need_user():
    if not S.userid:
        raise UserError("Sign in first.")


def _own_company(company_id: int) -> dict:
    company = db.get_company(company_id)
    if not company or company["user_id"] != S.userid:
        raise UserError("That company is no longer there.")
    return company


def _own_session(session_id: int) -> dict:
    row = db.get_session(session_id)
    if not row or row["user_id"] != S.userid:
        raise UserError("That session is no longer there.")
    return row


def _need_stage(*stages):
    if S.stage not in stages:
        raise UserError(f"Not available on the '{S.stage}' screen.")


# ───────────────────────── sign in ─────────────────────────

def login(user_id: str):
    user_id = (user_id or "").strip()
    if not user_id:
        raise UserError("User id cannot be empty.")
    S.userid = user_id
    db.ensure_user(user_id)
    S.companies = db.list_companies(user_id)
    # a user with no company has to onboard one first
    if S.companies:
        S.stage = "company_picker"
    else:
        from exira_core import onboarding
        onboarding.start()


# ───────────────────────── companies ─────────────────────────

def go_companies():
    _need_user()
    S.companies = db.list_companies(S.userid)
    S.stage = "company_picker"


def open_company(company_id: int):
    """Load a company and move to its session list."""
    _need_user()
    company = _own_company(company_id)

    S.company_id = company_id
    S.company_slot = company["slot"]
    S.company_name = company["company_name"]
    S.user_product_info = company["company_profile"] or ""

    # a company switch resets everything below it
    S.session_id = None
    S.session_slot = None
    S.old_session = ""
    S.persona = None
    S.answers = []
    S.turns = []
    S.append_queries = []
    S.scope_label = ""
    S.scope_history = []
    S.hs_code = None
    S.options = []
    S.candidates = collect_hs_from_memory(build_memory(), HS2_DOMAIN_MAP)

    db.touch_company(company_id)
    S.sessions = db.list_sessions(company_id)
    S.stage = "session_picker"


def delete_company(company_id: int):
    _need_user()
    _own_company(company_id)
    db.delete_company(company_id)
    if S.company_id == company_id:
        S.company_id = None
        S.session_id = None
    S.companies = db.list_companies(S.userid)
    if S.stage not in ("company_picker", "onboarding"):
        S.stage = "company_picker"


def company_report(company_id: int):
    _need_user()
    company = _own_company(company_id)
    return {"report": db.get_onboarding_report(company_id), "profile": company.get("company_profile")}


# ───────────────────────── sessions ─────────────────────────

def go_sessions():
    _need_user()
    if S.company_id is None:
        S.stage = "company_picker"
        return
    S.sessions = db.list_sessions(S.company_id)
    S.stage = "session_picker"


def new_session():
    """'+ New session': clear the chat and open it with no scope.

    The first product question sets the scope through the existing engine
    flow. begin_with() / POST /scope/start still work for clients that want
    to start on a chosen scope.
    """
    _need_user()
    if S.company_id is None:
        raise UserError("Open a company first.")
    if db.session_slots_free(S.company_id) <= 0:
        raise UserError("That company already has 10 sessions. Delete one first.")
    S.session_id = None
    S.session_slot = None
    S.old_session = ""
    S.persona = None
    S.answers = []
    S.turns = []
    S.append_queries = []
    S.scope_label = ""
    S.scope_history = []
    S.hs_code = None
    S.options = []
    S.selecting = False
    S.pending_clarify = None
    S.flow_run = None
    S.candidates = collect_hs_from_memory(build_memory(), HS2_DOMAIN_MAP)
    begin_without_scope()


def begin_without_scope():
    """Create a session with no scope and open the chat straight away."""
    S.hs_code = None
    S.scope_label = ""

    if S.session_id is None:
        sid = db.create_session(S.company_id, scope_label="", hs_code="")
        if sid is None:
            error("That company already has 10 sessions. Delete one first.")
            S.stage = "session_picker"
            return
        S.session_id = sid
        row = db.get_session(sid)
        S.session_slot = row["slot"] if row else None
        S.persona = load_persona(sid)

    S.stage = "chat"
    start_engine_session("")
    S.scope_history = []


def resume_session(session_id: int):
    """Reopen a past session: summary, persona, scope, answers and chat."""
    _need_user()
    row = _own_session(session_id)

    if row["company_id"] != S.company_id:          # resumed from another company
        open_company(row["company_id"])

    S.session_id = session_id
    S.session_slot = row["slot"]
    S.old_session = row["session_summary"] or ""
    S.persona = load_persona(session_id)
    S.scope_label = row["scope_label"] or ""
    S.hs_code = row["hs_code"]
    S.scope_history = [S.scope_label] if S.scope_label else []

    # rebuild the chat window from the stored question/answer pairs
    S.answers = db.get_answers(session_id)
    S.turns = []
    for item in S.answers:
        if item.get("question"):
            push_turn("user", item["question"])
        push_turn("assistant", item["answer"], item.get("trace"))

    S.append_queries = [a["question"] for a in S.answers if a.get("question")][-MAX_SESSION_QUERIES:]
    S.options = []
    S.selecting = False
    S.pending_clarify = None
    S.pending_input = None
    S.flow_run = None
    S.candidates = collect_hs_from_memory(build_memory(), HS2_DOMAIN_MAP)
    db.touch_session(session_id)

    # reattach to the stored engine session if it hasn't expired,
    # otherwise start a fresh one and point it at the saved scope
    if ENGINE.exists(row.get("engine_sid")):
        S.sid = row["engine_sid"]
        ENGINE.inject_memory(S.sid, build_memory())
    else:
        S.sid = ENGINE.start()
        db.save_session(session_id, engine_sid=S.sid)
        ENGINE.inject_memory(S.sid, build_memory())
        if S.hs_code:
            try:
                ENGINE.set_hs(S.sid, S.hs_code)
            except Exception:
                pass
        else:
            ENGINE.skip_setup(S.sid)
    print(f"[RESUME] session_id={session_id} stored={row.get('engine_sid')} using={S.sid}")
    S.stage = "chat"


def delete_session(session_id: int):
    _need_user()
    _own_session(session_id)
    db.delete_session(session_id)
    if S.session_id == session_id:
        S.session_id = None
        S.turns = []
        if S.stage in ("chat", "done"):
            S.stage = "session_picker"
    if S.company_id is not None:
        S.sessions = db.list_sessions(S.company_id)


# ───────────────────────── scope ─────────────────────────

def begin_with(choice: str):
    """Create a session for the chosen scope, then open the chat."""
    _need_user()
    choice = (choice or "").strip()
    if not choice:
        raise UserError("Enter an HS code or a product name.")
    if S.company_id is None:
        raise UserError("Open a company first.")

    code = re.sub(r"[.\-\s]", "", choice)
    if re.fullmatch(r"\d{2,10}", code):
        S.hs_code = code
        S.scope_label = f"HS {code}"
        remember_hs(code)
        opening = f"HS {code}"
    else:
        S.hs_code = None
        S.scope_label = choice
        opening = choice

    if S.session_id is None:
        sid = db.create_session(S.company_id, scope_label=S.scope_label, hs_code=S.hs_code or "")
        if sid is None:
            error("That company already has 10 sessions. Delete one first.")
            S.stage = "session_picker"
            return
        S.session_id = sid
        row = db.get_session(sid)
        S.session_slot = row["slot"] if row else None
        S.persona = load_persona(sid)
    else:
        db.save_session(S.session_id, scope_label=S.scope_label, hs_code=S.hs_code or "")

    S.stage = "chat"
    start_engine_session(opening)
    S.scope_history = [S.scope_label] if S.scope_label else []


def change_scope(choice: str):
    """Sidebar '⇄ Change HS / product scope'."""
    _need_stage("chat")
    choice = (choice or "").strip()
    if not choice:
        raise UserError("Enter an HS code or a product name.")
    switch_scope(choice)


# ───────────────────────── chat ─────────────────────────

def send_message(text: str, source: str = "typed"):
    """
    source:
      "typed"          the user typed it in the chat box
      "suggestion"     a suggested-question / domain button under the chat
      "clarification"  one of the options of an open clarification
    """
    _need_stage("chat")
    text = (text or "").strip()
    if not text:
        raise UserError("Message is empty.")

    if source == "clarification":
        handle_message(text, from_list=False)          # resume_clarification shows the pick
    elif source == "suggestion":
        push_turn("user", text)
        handle_message(text, from_list=not S.selecting)
    else:
        push_turn("user", text)
        handle_message(text, from_list=False)


def end_session():
    """Summarise the session, save it, and show the done screen."""
    _need_stage("chat")
    current = ""
    if S.append_queries:
        try:
            current = query_summary(S.append_queries)[0]
        except Exception as exc:
            warn(f"Session summary failed: {exc}")

    notes = S.old_session
    if current:
        try:
            notes = session_summary(S.old_session, current)
        except Exception as exc:
            warn(f"Session merge failed: {exc}")
            notes = current

    try:
        if S.session_id is not None:
            db.save_session(S.session_id, session_summary=notes or "", persona=S.persona,
                            scope_label=S.scope_label or "", hs_code=S.hs_code or "", ended=True)
        if S.company_id is not None:
            db.touch_company(S.company_id)
    except Exception as exc:
        error(f"Save failed: {exc}")

    S.session_notes = notes or ""
    S.stage = "done"


# ───────────────────────── the view a UI renders ─────────────────────────

_PUBLIC_TRACE_KEYS = ("visualization", "rows", "route")


def _public_trace(trace):
    """What a normal UI needs to draw charts. debug=true returns everything."""
    if not isinstance(trace, dict):
        return None
    out = {k: trace[k] for k in _PUBLIC_TRACE_KEYS if k in trace}
    if trace.get("charts"):
        out["charts"] = [
            {"qid": c.get("qid"),
             "trace": {k: (c.get("trace") or {}).get(k) for k in ("visualization", "rows")}}
            for c in trace["charts"]
        ]
    if trace.get("thinking"):
        out["thinking"] = [
            {k: r.get(k) for k in ("task_id", "query", "stopped_reason", "charts", "sources")}
            for r in trace["thinking"]
        ]
    if isinstance(trace.get("web"), dict) and trace["web"].get("sources"):
        out["web_sources"] = trace["web"]["sources"]
    return out or None


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


def view(debug: bool = False) -> dict:
    """Snapshot of the workspace for a UI. Lists come fresh from the database."""
    stage = S.stage
    v = {
        "stage": stage,
        "user_id": S.userid,
        "notices": list(S.notices or []),
        "company": None,
        "session": None,
    }

    if S.company_id is not None:
        v["company"] = {"company_id": S.company_id, "slot": S.company_slot, "name": S.company_name,
                        "profile": S.user_product_info}

    if S.userid and stage == "company_picker":
        v["companies"] = db.list_companies(S.userid)
        v["company_slots_free"] = db.company_slots_free(S.userid)

    if S.company_id is not None and stage == "session_picker":
        v["sessions"] = db.list_sessions(S.company_id)
        v["session_slots_free"] = db.session_slots_free(S.company_id)

    if stage in ("scope", "chat"):
        v["candidates"] = S.candidates

    if stage == "onboarding":
        pending = S.onb_pending or {}
        v["onboarding"] = {
            "step": S.onb_step,
            "text": S.onb_text,
            "urls": S.onb_urls,
            "attempt": S.onb_attempt,
            "max_attempts": MAX_ATTEMPTS,
            "scrape_attempt": S.onb_scrape_attempt,
            "can_retry_scrape": S.onb_scrape_attempt < MAX_ATTEMPTS,
            "matches": S.onb_matches,
            "narrow_threshold": NARROW_THRESHOLD,
            "filters": {"country": S.onb_country, "hs2": S.onb_hs2},
            "chosen_company": S.onb_company,
            "scraped": {"company_name": pending.get("company_name") or "",
                        "company_info": pending.get("company_info") or ""},
            "failure": S.onb_failure,
            "company_slots_free": db.company_slots_free(S.userid) if S.userid else 0,
        }
        if debug:
            v["onboarding"]["report"] = S.report

    if stage in ("chat", "done") and S.session_id is not None:
        v["session"] = {"session_id": S.session_id, "slot": S.session_slot}

    if stage == "chat":
        turns = [t for t in S.turns if debug or t["role"] != "system"]
        v["chat"] = {
            "scope": {"label": S.scope_label, "hs_code": S.hs_code, "history": S.scope_history},
            "turns": [{"role": t["role"], "content": t["content"],
                       "trace": t.get("trace") if debug else _public_trace(t.get("trace"))}
                      for t in turns],
            "system_notes": [t["content"] for t in S.turns if t["role"] == "system"],
            "options": [] if S.pending_clarify else S.options,
            "selecting": bool(S.selecting),
            "clarification": ({"question": S.pending_clarify.get("question"),
                               "options": S.pending_clarify.get("options")}
                              if S.pending_clarify else None),
            "input_hint": ("Pick a domain above, or type HS codes like: HS 52, 61" if S.selecting
                           else "Pick an option above, or type your own answer…" if S.pending_clarify
                           else "Ask EXIRA a trade question…"),
        }
        v["context"] = {
            "company_profile": S.user_product_info,
            "previous_summary": S.old_session,
            "current_queries": S.append_queries,
            "persona": persona_to_text(S.persona) if S.persona else "",
            "captured_answers": S.answers,
        }

    if stage == "done":
        v["session_notes"] = S.session_notes

    return _jsonable(v)
