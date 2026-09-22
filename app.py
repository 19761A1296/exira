# app.py — Streamlit UI. Does not modify any backend file.
#
# Run with:  streamlit run app.py
#
# The CLI modules (src/router.py, src/run_pipeline.py) use input(), which
# Streamlit cannot drive. This file reimplements those two loops as UI steps
# while calling the same backend functions. The onboarding wizard follows
# run_pipeline's flow exactly: DB lookup -> narrow -> pick from list -> scrape
# -> approve each field -> resolve again -> build profile, with 3 attempts.

import json
import re

import altair as alt

import pandas as pd
import streamlit as st

from engine_adapter import ENGINE, HS2_DOMAIN_MAP
from hs_memory import collect_hs_from_memory

from database import info_database as db
from database.snowflake_db import companies_list, build_query, run_query
from extraction.extracting_urls import get_company_information_links
from reporting.report_generator import init_report, update_report, log_report
from persona.tweaked_exira import build_and_save_persona, load_persona, persona_to_text
from summarizer.session_summarizer import query_summary, session_summary
from summarizer.classifier import classify_query
from summarizer.analyzer import resolve_query
from summarizer.web_search import web_search, as_context
from summarizer.memory_answer import answer_from_memory
from summarizer.user_summary import build_user_summary

MAX_SESSION_QUERIES = 5
MAX_ATTEMPTS = 3
NARROW_THRESHOLD = 30

st.set_page_config(page_title="Trade Intelligence", page_icon="🌐", layout="wide")


# ───────────────────────── styling ─────────────────────────

st.markdown(
    """
    <style>
      div[data-testid="stVerticalBlock"] div.suggest-zone button {
          background-color: #eef4fb;
          color: #33475b;
          border: 1px solid #d6e3f0;
          text-align: left;
          font-weight: 400;
          transition: background-color .15s ease, color .15s ease;
      }
      div[data-testid="stVerticalBlock"] div.suggest-zone button:hover {
          background-color: #1f3b57;
          color: #ffffff;
          border-color: #1f3b57;
      }
      .scope-pill {
          display:inline-block; padding:2px 10px; border-radius:12px;
          background:#eef4fb; color:#33475b; font-size:0.85rem;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


# ───────────────────────── state ─────────────────────────

def init_state():
    d = st.session_state
    d.setdefault("stage", "login")
    d.setdefault("userid", "")
    d.setdefault("user_product_info", "")
    d.setdefault("old_session", "")
    d.setdefault("report", None)
    d.setdefault("persona", None)
    d.setdefault("persona_turns", [])
    d.setdefault("append_queries", [])
    d.setdefault("sid", None)
    d.setdefault("turns", [])
    d.setdefault("options", [])
    d.setdefault("selecting", False)
    d.setdefault("pending_confirm", None)
    d.setdefault("scope_label", "")
    d.setdefault("scope_history", [])
    d.setdefault("hs_code", None)
    d.setdefault("candidates", [])
    d.setdefault("show_hs_panel", False)
    d.setdefault("session_notes", "")
    d.setdefault("last_resolution", None)
    d.setdefault("last_route", "")
    d.setdefault("pending_input", None)     # (text, from_list) awaiting work

    # onboarding wizard
    d.setdefault("onb_step", "input")
    d.setdefault("onb_text", "")
    d.setdefault("onb_urls", "")
    d.setdefault("onb_attempt", 0)
    d.setdefault("onb_scrape_attempt", 0)
    d.setdefault("onb_matches", [])
    d.setdefault("onb_company", "")
    d.setdefault("onb_scraped_info", "")
    d.setdefault("onb_scraped_name", "")
    d.setdefault("onb_pending", {})
    d.setdefault("onb_after_urls", "")
    # narrowing
    d.setdefault("onb_search_name", "")      # the name the matches came from
    d.setdefault("onb_country", "")
    d.setdefault("onb_hs2", "")
    d.setdefault("onb_next_after_pick", "")


init_state()
S = st.session_state


def build_memory() -> dict:
    return {
        "user_product_info": S.user_product_info or "",
        "old_session_summary": S.old_session or "",
        "current_session_queries": S.append_queries[-MAX_SESSION_QUERIES:],
        "persona": persona_to_text(S.persona) if S.persona else "",
    }


# ═════════════════════════ ONBOARDING (mirrors run_pipeline) ═════════════════════════

def onb_reset_for_retry():
    S.onb_matches = []
    S.onb_company = ""
    S.onb_scraped_info = ""
    S.onb_scraped_name = ""
    S.onb_pending = {}
    S.onb_scrape_attempt = 0
    S.onb_country = ""
    S.onb_hs2 = ""


def onb_lookup(name: str, country: str = "", hs2: str = "") -> list:
    """companies_list wrapper — the DB half of resolve_in_db."""
    if not name:
        return []
    try:
        return companies_list(name, limit=500,
                              country=country or None,
                              hs2=hs2 or None) or []
    except TypeError:
        # snowflake_db not yet updated with the filter arguments
        if country or hs2:
            st.warning("companies_list does not support country/HS filters yet.")
        try:
            return companies_list(name) or []
        except Exception as exc:
            st.error(f"Company lookup failed: {exc}")
            return []
    except Exception as exc:
        st.error(f"Company lookup failed: {exc}")
        return []


def onb_scrape(urls: str) -> dict:
    try:
        return get_company_information_links(urls) or {}
    except Exception as exc:
        st.error(f"Scrape failed: {exc}")
        return {}


def onb_build_profile(company_name: str, scraped_info: str):
    """run_db_pipeline equivalent, then save and move on."""
    data = []
    if company_name:
        try:
            df = run_query(build_query(company_name))
            if isinstance(df, pd.DataFrame) and not df.empty:
                data.append(df)
                st.caption(f"Pulled {len(df)} shipment rows for {company_name}.")
            else:
                st.caption(f"No shipment rows found for {company_name}.")
        except Exception as exc:
            st.warning(f"Snowflake query failed: {exc}")
    if scraped_info:
        data.append(scraped_info)

    update_report(S.report,
                  source="db_pipeline" if company_name else "scraped_info")

    summary = ""
    try:
        summary = build_user_summary(company_name, data)
    except Exception as exc:
        st.error(f"Profile build failed: {exc}")

    log_report(S.report, tag="ui-onboarding")

    S.user_product_info = summary or ""
    S.old_session = ""
    S.persona = load_persona(S.userid)
    S.candidates = collect_hs_from_memory(build_memory(), HS2_DOMAIN_MAP)
    S.stage = "scope"


def onb_fail_or_retry(reason: str):
    """Nothing worked this attempt — offer another, up to MAX_ATTEMPTS."""
    st.warning(reason)
    if S.onb_attempt >= MAX_ATTEMPTS:
        st.error(f"Max attempts ({MAX_ATTEMPTS}) reached.")
        update_report(S.report, source=None)
        log_report(S.report, tag="exhausted")
        if st.button("Continue without a profile"):
            S.user_product_info = ""
            S.old_session = ""
            S.persona = load_persona(S.userid)
            S.candidates = []
            S.stage = "scope"
            st.rerun()
        return
    st.caption(f"Attempt {S.onb_attempt} of {MAX_ATTEMPTS}.")
    extra = st.text_input("Add more information and try again", key="onb_more").strip()
    if st.button("Try again", type="primary"):
        if extra:
            S.onb_text = f"{S.onb_text} {extra}".strip()
        S.onb_urls = ""
        onb_reset_for_retry()
        S.onb_step = "input"
        st.rerun()


def enter_pick(matches: list, search_name: str, next_step: str):
    """Route to the narrow step when the list is long, else straight to picking."""
    S.onb_matches = matches
    S.onb_search_name = search_name
    S.onb_next_after_pick = next_step
    S.onb_step = "narrow" if len(matches) > NARROW_THRESHOLD else next_step


# ── step: initial input ───────────────────────────────────────────

def step_input():
    st.subheader("Step 1 — who are you?")
    st.info("Fill in at least one of the two. Both is better.")

    text = st.text_area("Company name / description (optional)",
                        value=S.onb_text, height=100).strip()
    urls = st.text_area("Company URLs, one or more (optional)",
                        value=S.onb_urls, height=80).strip()

    if not st.button("Continue", type="primary"):
        return
    if not text and not urls:
        st.error("Enter a company name or at least one URL.")
        return

    S.onb_text, S.onb_urls = text, urls
    S.onb_attempt += 1

    if S.report is None:
        S.report = init_report()
    update_report(S.report, attempt=S.onb_attempt,
                  company_input=text, url=urls)

    # resolve_from_text -> resolve_in_db
    if text:
        matches = onb_lookup(text)
        if matches:
            enter_pick(matches, text, "pick_text")
            st.rerun()

    # no DB hit from text -> URL path
    if urls:
        S.onb_step = "review_urls"
    else:
        S.onb_after_urls = "review_urls"
        S.onb_step = "ask_urls"
    st.rerun()


# ── step: narrow a long list by country, then HS chapter ──────────

def step_narrow():
    st.subheader("Too many matches — narrow it down")
    st.caption(f"{len(S.onb_matches)} companies matched “{S.onb_search_name}”.")

    active = []
    if S.onb_country:
        active.append(f"country = {S.onb_country}")
    if S.onb_hs2:
        active.append(f"HS {S.onb_hs2}")
    if active:
        st.caption("Filters applied: " + ", ".join(active))

    country = st.text_input("Country (leave blank to skip)",
                            value=S.onb_country, key="nw_country").strip()

    hs2 = ""
    if S.onb_country or country:
        hs2 = st.text_input("HS code — first 2 digits are used (leave blank to skip)",
                            value=S.onb_hs2, key="nw_hs2").strip()

    c1, c2 = st.columns(2)

    if c1.button("Apply filter", type="primary", use_container_width=True):
        filtered = onb_lookup(S.onb_search_name, country=country, hs2=hs2)
        if filtered:
            S.onb_matches = filtered
            S.onb_country, S.onb_hs2 = country, hs2
            if len(filtered) <= NARROW_THRESHOLD:
                S.onb_step = S.onb_next_after_pick
            st.rerun()
        else:
            st.warning("Nothing matched those filters — the previous list is kept.")

    if c2.button(f"Show all {len(S.onb_matches)} anyway", use_container_width=True):
        S.onb_step = S.onb_next_after_pick
        st.rerun()


# ── step: pick a company matched from the typed text ──────────────

def step_pick_text():
    st.subheader("Step 2 — which company is yours?")
    st.caption(f"Matches for “{S.onb_search_name or S.onb_text}” in the trade database:")

    if len(S.onb_matches) > NARROW_THRESHOLD and st.button("🔎 Narrow this list"):
        S.onb_step = "narrow"
        st.rerun()

    for i, name in enumerate(S.onb_matches, 1):
        if st.button(f"{i}. {name}", key=f"pt_{i}", use_container_width=True):
            S.onb_company = name
            update_report(S.report, company_name=name, company_found_in_db=True)
            if S.onb_urls:
                S.onb_step = "scrape_known"
            else:
                S.onb_after_urls = "scrape_known"
                S.onb_step = "ask_urls"
            st.rerun()

    st.divider()
    if st.button("None of these"):
        if S.onb_urls:
            S.onb_step = "review_urls"
        else:
            S.onb_after_urls = "review_urls"
            S.onb_step = "ask_urls"
        st.rerun()


# ── step: ask for URLs when they weren't given ────────────────────

def step_ask_urls():
    st.subheader("Links")
    if S.onb_company:
        st.caption(f"Found **{S.onb_company}** in the database. "
                   "Links are optional extra context.")
    else:
        st.caption("Not found in the database. Links are needed to build a profile.")

    urls = st.text_input("Company URLs (optional)", key="onb_ask_urls").strip()
    c1, c2 = st.columns(2)

    if c1.button("Use these links", type="primary") and urls:
        S.onb_urls = urls
        update_report(S.report, url=urls)
        S.onb_step = S.onb_after_urls
        st.rerun()

    if c2.button("Skip"):
        if S.onb_company:
            onb_build_profile(S.onb_company, "")
            st.rerun()
        else:
            onb_fail_or_retry("No company and no links, so nothing to work with.")


# ── step: known company, scrape for extra context (y/n loop) ──────

def step_scrape_known():
    st.subheader(f"Step 3 — extra context for {S.onb_company}")

    if not S.onb_pending:
        S.onb_scrape_attempt += 1
        with st.spinner(f"Reading {S.onb_urls} ..."):
            S.onb_pending = onb_scrape(S.onb_urls)

    info = S.onb_pending.get("company_info") or ""
    st.caption(f"Attempt {S.onb_scrape_attempt} of {MAX_ATTEMPTS}")

    if info:
        st.markdown("**Scraped information**")
        st.write(info)
    else:
        st.write("_Nothing was scraped from those links._")

    c1, c2, c3 = st.columns(3)

    if c1.button("✅ Accept", type="primary", use_container_width=True):
        update_report(S.report, scraped_info=info, source="db_pipeline")
        onb_build_profile(S.onb_company, info)
        st.rerun()

    if c2.button("🚫 Skip links", use_container_width=True):
        onb_build_profile(S.onb_company, "")
        st.rerun()

    with c3:
        retry_disabled = S.onb_scrape_attempt >= MAX_ATTEMPTS
        new_urls = st.text_input("Different links", key="sk_urls",
                                 disabled=retry_disabled).strip()
        if st.button("🔁 Retry", use_container_width=True, disabled=retry_disabled):
            if new_urls:
                S.onb_urls = new_urls
                update_report(S.report, url=new_urls)
            S.onb_pending = {}
            st.rerun()


# ── step: unknown company, approve name and info separately ───────

def step_review_urls():
    st.subheader("Step 3 — reading your links")

    if not S.onb_pending:
        S.onb_scrape_attempt += 1
        with st.spinner(f"Reading {S.onb_urls} ..."):
            S.onb_pending = onb_scrape(S.onb_urls)

    name = S.onb_pending.get("company_name") or ""
    info = S.onb_pending.get("company_info") or ""
    st.caption(f"Attempt {S.onb_scrape_attempt} of {MAX_ATTEMPTS}")

    st.markdown("**company_name**")
    if name:
        st.code(name)
        take_name = st.checkbox("Accept this company name", value=True, key="rv_name")
    else:
        st.write("_nothing scraped_")
        take_name = False

    st.markdown("**company_info**")
    if info:
        st.write(info)
        take_info = st.checkbox("Accept this information", value=True, key="rv_info")
    else:
        st.write("_nothing scraped_")
        take_info = False

    st.divider()
    c1, c2 = st.columns(2)

    if c1.button("Continue", type="primary", use_container_width=True):
        S.onb_scraped_name = name if take_name else ""
        S.onb_scraped_info = info if take_info else ""
        if S.onb_scraped_info:
            update_report(S.report, scraped_info=S.onb_scraped_info)

        if S.onb_scraped_name:
            matches = onb_lookup(S.onb_scraped_name)
            if matches:
                enter_pick(matches, S.onb_scraped_name, "pick_url")
                st.rerun()

        if S.onb_scraped_info:
            update_report(S.report,
                          company_name=S.onb_scraped_name,
                          company_found_in_db=False,
                          source="scraped_info")
            onb_build_profile(S.onb_scraped_name, S.onb_scraped_info)
            st.rerun()

        onb_fail_or_retry("Nothing was accepted from those links.")

    with c2:
        retry_disabled = S.onb_scrape_attempt >= MAX_ATTEMPTS
        new_urls = st.text_input("Different links", key="rv_urls",
                                 disabled=retry_disabled).strip()
        if st.button("🔁 Retry scrape", use_container_width=True,
                     disabled=retry_disabled):
            if new_urls:
                S.onb_urls = new_urls
                update_report(S.report, url=new_urls)
            S.onb_pending = {}
            st.rerun()


# ── step: pick a company matched from the scraped name ────────────

def step_pick_url():
    st.subheader("Found in the database")
    st.caption(f"Matches for “{S.onb_scraped_name}”:")

    if len(S.onb_matches) > NARROW_THRESHOLD and st.button("🔎 Narrow this list"):
        S.onb_step = "narrow"
        st.rerun()

    for i, name in enumerate(S.onb_matches, 1):
        if st.button(f"{i}. {name}", key=f"pu_{i}", use_container_width=True):
            update_report(S.report, company_name=name,
                          company_found_in_db=True, source="db_pipeline")
            onb_build_profile(name, S.onb_scraped_info)
            st.rerun()

    st.divider()
    if st.button("None of these"):
        update_report(S.report, company_name=S.onb_scraped_name,
                      company_found_in_db=False, source="scraped_info")
        if S.onb_scraped_info:
            onb_build_profile(S.onb_scraped_name, S.onb_scraped_info)
            st.rerun()
        else:
            onb_fail_or_retry("No database match and no scraped information.")


ONB_STEPS = {
    "input": step_input,
    "narrow": step_narrow,
    "pick_text": step_pick_text,
    "ask_urls": step_ask_urls,
    "scrape_known": step_scrape_known,
    "review_urls": step_review_urls,
    "pick_url": step_pick_url,
}


def page_onboarding():
    st.title("New user setup")
    st.caption(f"User id: {S.userid}")
    ONB_STEPS[S.onb_step]()
    with st.sidebar:
        st.markdown("**Onboarding**")
        st.write(f"Step: `{S.onb_step}`")
        with st.expander("report"):
            st.json(S.report or {})


# ═════════════════════════ CHAT ═════════════════════════

def consume(resp: dict) -> dict:
    out = {"texts": [], "confirm": None, "options": [], "trace": None,
           "state": resp.get("state")}
    for m in resp.get("messages", []):
        kind = m.get("type")
        if kind == "domain_selector":
            continue
        if kind in ("answer", "clarification"):
            out["texts"].append(m["content"])
            if m.get("data"):
                out["trace"] = m["data"]
        elif kind == "confirm":
            out["confirm"] = m["data"]["resolved_query"]
        elif kind == "followup":
            out["options"] = m["data"]["items"]
    return out


def push_turn(role: str, content: str, trace=None):
    S.turns.append({"role": role, "content": content, "trace": trace})


def sync_scope(announce: bool = True):
    """Pull the real scope back from the engine; log it if it changed."""
    scope = ENGINE.current_scope(S.sid)
    label = scope["label"]
    if not label or label == S.scope_label:
        return

    previous = S.scope_label
    S.scope_label = label
    S.hs_code = scope["hs_code"]

    if not S.scope_history or S.scope_history[-1] != label:
        S.scope_history.append(label)

    if scope["hs_code"]:
        remember_hs(scope["hs_code"])

    if announce:
        note = f"Scope: {label}" if not previous else f"Scope changed: {previous} → {label}"
        push_turn("system", note)


def apply_engine_response(resp: dict):
    parsed = consume(resp)
    for t in parsed["texts"]:
        push_turn("assistant", t, parsed["trace"])
    S.options = parsed["options"] or S.options
    S.selecting = ENGINE.scope_pending(S.sid)
    S.pending_confirm = parsed["confirm"]
    sync_scope()
    return parsed


def record_query(text: str):
    S.append_queries.append(text)
    if len(S.append_queries) > MAX_SESSION_QUERIES:
        try:
            S.append_queries = [query_summary(S.append_queries)[0]]
        except Exception as exc:
            st.warning(f"Query summary failed: {exc}")

    if S.userid:
        S.persona_turns.append({"role": "user", "content": text})
        try:
            S.persona = build_and_save_persona(
                S.userid, S.persona_turns, recent_turns=1, persona=S.persona
            )
        except Exception as exc:
            st.warning(f"Persona update failed: {exc}")


def attach_meta():
    """Put the routing and resolution on the newest assistant turn."""
    if not S.turns or S.turns[-1]["role"] != "assistant":
        return
    tr = S.turns[-1].get("trace") or {}
    tr["route"] = S.last_route
    if S.last_resolution:
        tr["resolution"] = S.last_resolution
    S.turns[-1]["trace"] = tr


def trade_succeeded() -> bool:
    """True only when the engine actually returned rows."""
    if not S.turns or S.turns[-1]["role"] != "assistant":
        return False
    trace = S.turns[-1].get("trace") or {}
    stage = (trace.get("meta") or {}).get("stage")
    if stage and stage != "success":
        return False
    if trace.get("error"):
        return False
    if "rows" in trace:
        return bool(trace.get("rows"))
    return True


def maybe_web(query: str, memory: dict, route: str):
    """Run sonar-pro when the route asked for it, or when the data came back
    empty or errored. The heading tells the user which case it is."""
    data_ok = trade_succeeded()
    if route != "WEB" and data_ok:
        return

    if data_ok:
        heading = "Additional context from the web"
    else:
        heading = ("Your trade records had nothing to answer this, "
                   "so here is what the web says")

    with st.spinner("Looking this up on the web..."):
        result = web_search(query, memory)

    block = as_context(result, heading)
    if not block:
        if not data_ok:
            push_turn("assistant",
                      "Nothing came back from your trade records or the web "
                      "for that. Try rephrasing it.",
                      {"route": route, "web": result})
        return

    push_turn("assistant", block,
              {"route": "WEB", "web": result, "data_ok": data_ok,
               "resolution": S.last_resolution})


def chat_history() -> list:
    """S.turns -> the USER/EXIRA history the analyzer expects."""
    out = []
    for t in S.turns:
        if t["role"] == "user":
            out.append({"role": "USER", "content": t["content"]})
        elif t["role"] == "assistant":
            out.append({"role": "EXIRA", "content": t["content"]})
    return out[-12:]


def handle_message(text: str, from_list: bool = False):
    text = (text or "").strip()
    if not text:
        return

    # the user turn is already on screen; drop it from the history we analyse
    history = chat_history()
    while history and history[-1]["role"] == "USER":
        history.pop()

    ENGINE.inject_memory(S.sid, build_memory())

    if S.selecting:
        try:
            apply_engine_response(ENGINE.send(S.sid, text))
        except Exception as exc:
            push_turn("assistant", f"Error: {exc}")
        return

    memory = build_memory()

    # ---- resolve the message against what is still pending ----
    if from_list:
        sent = text                       # a suggested question stands alone
        S.last_resolution = None
        route = "TRADE"
    else:
        S.last_resolution = resolve_query(text, history, memory)
        if S.last_resolution["blocked"]:
            push_turn("assistant", S.last_resolution["message"],
                      {"route": "BLOCKED", "resolution": S.last_resolution})
            return 
        sent = S.last_resolution["resolved_query"] or text
        try:
            route = classify_query(sent, memory)
        except Exception:
            route = "TRADE"
    S.last_route = route

    try:
        if route == "PERSONAL":
            push_turn("assistant", answer_from_memory(sent, memory),
                      {"route": "PERSONAL", "resolution": S.last_resolution})
        else:
            # TRADE and WEB both run the engine first
            parsed = apply_engine_response(
                ENGINE.send(S.sid, sent, source="followup" if from_list else None)
            )
            attach_meta()
            if S.selecting:
                return
            if parsed["confirm"]:
                run_confirm("proceed", sent, memory, route)
                return
            maybe_web(sent, memory, route)
    except Exception as exc:
        push_turn("assistant", f"Error: {exc}")
        return

    record_query(text)


def run_confirm(decision: str, sent: str = "", memory: dict = None,
                route: str = "TRADE"):
    S.pending_confirm = None
    try:
        apply_engine_response(ENGINE.confirm(S.sid, decision))
    except Exception as exc:
        push_turn("assistant", f"Error: {exc}")
        return
    if decision == "proceed":
        attach_meta()
        if sent:
            maybe_web(sent, memory or build_memory(), route)
        last_user = next((t["content"] for t in reversed(S.turns)
                          if t["role"] == "user"), "")
        if last_user:
            record_query(last_user)


def remember_hs(code: str):
    if any(c["code"] == code for c in S.candidates):
        return
    S.candidates.append({
        "code": code,
        "domain": HS2_DOMAIN_MAP.get(code[:2].zfill(2), "Unknown / Other"),
        "source": "current_session",
    })
    S.candidates.sort(key=lambda x: x["code"])


def switch_scope(choice: str):
    code = re.sub(r"[.\-\s]", "", choice or "")
    if re.fullmatch(r"\d{2,10}", code):
        S.hs_code = code
        S.scope_label = f"HS {code}"
        remember_hs(code)
        ENGINE.set_hs(S.sid, code)
        S.selecting = False
        S.options = [
            f"Who are the top buyers of HS {code} in the last 24 months?",
            f"Which countries are showing increasing demand for HS {code} in the last 24 months?",
            f"Which month does demand for HS {code} usually peak over the last 36 months?",
        ]
        push_turn("system", f"Scope changed to HS {code}")
    else:
        S.hs_code = None
        S.scope_label = choice
        push_turn("user", choice)
        try:
            apply_engine_response(ENGINE.send(S.sid, choice))
        except Exception as exc:
            push_turn("assistant", f"Error: {exc}")
    S.show_hs_panel = False


def start_engine_session(opening: str):
    S.sid = ENGINE.start()
    ENGINE.inject_memory(S.sid, build_memory())
    try:
        apply_engine_response(ENGINE.send(S.sid, opening))
    except Exception as exc:
        push_turn("assistant", f"Error starting session: {exc}")


# ───────────────────────── pages ─────────────────────────

def page_login():
    st.title("🌐 Trade Intelligence")
    st.caption("Enter your user id to begin.")

    try:
        db.init_db()
    except Exception as exc:
        st.error(f"Database init failed: {exc}")

    userid = st.text_input("User id", key="login_userid").strip()
    if not st.button("Continue", type="primary"):
        return
    if not userid:
        st.error("User id cannot be empty.")
        return

    S.userid = userid
    try:
        exists = db.user_exists(userid)
    except Exception as exc:
        st.error(f"Lookup failed: {exc}")
        return

    if exists:
        S.user_product_info = db.get_product_info(userid) or ""
        S.old_session = db.get_past_sesion_info(userid) or ""
        S.persona = load_persona(userid)
        S.candidates = collect_hs_from_memory(build_memory(), HS2_DOMAIN_MAP)
        S.stage = "scope"
    else:
        S.stage = "onboarding"
    st.rerun()


def begin_with(choice: str):
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
    S.stage = "chat"
    start_engine_session(opening)
    S.scope_history = [S.scope_label] if S.scope_label else []


def page_scope():
    st.title("Choose a scope")
    st.caption("Pick an HS code from your profile, or type a code or product name.")

    if S.candidates:
        st.subheader("From your profile")
        for c in S.candidates:
            if st.button(f"HS {c['code']} — {c['domain']}", key=f"cand_{c['code']}",
                         use_container_width=True):
                begin_with(c["code"])
                st.rerun()
        st.divider()

    typed = st.text_input("Or enter an HS code (digits) or a product name").strip()
    if st.button("Start", type="primary") and typed:
        begin_with(typed)
        st.rerun()


def sidebar():
    with st.sidebar:
        st.markdown(f"**User:** {S.userid}")
        st.markdown(f"<span class='scope-pill'>Now: {S.scope_label or '—'}</span>",
                    unsafe_allow_html=True)
        if len(S.scope_history) > 1:
            with st.expander(f"scope history ({len(S.scope_history)})"):
                for i, s in enumerate(S.scope_history, 1):
                    marker = " ← current" if i == len(S.scope_history) else ""
                    st.write(f"{i}. {s}{marker}")
        st.divider()

        if st.button("🔀 Change HS code", use_container_width=True):
            S.show_hs_panel = not S.show_hs_panel

        if S.show_hs_panel:
            with st.container(border=True):
                st.caption("HS codes from your memory")
                if not S.candidates:
                    st.write("None found.")
                for c in S.candidates:
                    if st.button(f"HS {c['code']} — {c['domain']}",
                                 key=f"sw_{c['code']}", use_container_width=True):
                        switch_scope(c["code"])
                        st.rerun()
                other = st.text_input("Other code or product", key="sw_other").strip()
                if st.button("Use this", key="sw_go") and other:
                    switch_scope(other)
                    st.rerun()

        st.divider()

        with st.expander("User_memory"):
            st.write(S.user_product_info or "_empty_")
        with st.expander("past_memory"):
            st.write(S.old_session or "_empty_")
        with st.expander("current_queries"):
            if S.append_queries:
                for i, q in enumerate(S.append_queries, 1):
                    st.write(f"{i}. {q}")
            else:
                st.write("_empty_")
        with st.expander("persona"):
            st.write(persona_to_text(S.persona) if S.persona else "_empty_")
        with st.expander("report"):
            if S.report:
                st.json(S.report)
            else:
                st.write("_no onboarding report for this user_")

        st.divider()
        if st.button("End session & save", type="primary", use_container_width=True):
            end_session()
            st.rerun()


def render_visual(trace: dict):
    """Draw the chart Prompt C asked for, using the returned rows."""
    if not trace:
        return
    spec = trace.get("visualization") or {}
    rows = trace.get("rows") or []
    vtype = spec.get("type", "none")
    if vtype == "none" or not rows:
        return

    df = pd.DataFrame(rows)
    title = spec.get("title")
    x, y = spec.get("x_column"), spec.get("y_column")
    series, trend = spec.get("series_column"), spec.get("trendline_column")

    if title:
        st.markdown(f"**{title}**")

    try:
        if vtype == "structured_table":
            cols = [c for c in (spec.get("columns") or []) if c in df.columns]
            st.dataframe(df[cols] if cols else df, use_container_width=True)
            return

        if not x or not y or x not in df.columns or y not in df.columns:
            st.dataframe(df, use_container_width=True)
            return

        if vtype == "bar_chart":
            if series and series in df.columns:
                st.bar_chart(df.pivot_table(index=x, columns=series,
                                            values=y, aggfunc="sum"))
            else:
                st.bar_chart(df.set_index(x)[y])

        elif vtype == "line_chart":
            ycols = [y]
            if trend and trend in df.columns:
                ycols.append(trend)
            if series and series in df.columns and not trend:
                st.line_chart(df.pivot_table(index=x, columns=series,
                                             values=y, aggfunc="sum"))
            else:
                st.line_chart(df.set_index(x)[ycols])

        elif vtype == "scatter_chart":
            st.scatter_chart(df, x=x, y=y,
                             color=series if series in df.columns else None)

        elif vtype == "pie_chart":
            chart = (
                alt.Chart(df)
                .mark_arc()
                .encode(theta=alt.Theta(f"{y}:Q"),
                        color=alt.Color(f"{x}:N"),
                        tooltip=[x, y])
            )
            st.altair_chart(chart, use_container_width=True)

        else:
            st.dataframe(df, use_container_width=True)

    except Exception as exc:
        st.caption(f"Could not draw the chart ({exc}) — showing the data instead.")
        st.dataframe(df, use_container_width=True)


def render_resolution(trace: dict):
    """How the analyzer rewrote the message, and where it was routed."""
    res = (trace or {}).get("resolution")
    route = (trace or {}).get("route") or ""
    if not res and not route:
        return
    label = "query resolution"
    if res:
        label += f" — {res['relation']} ({res['confidence']})"
    if route:
        label += f" → {route}"
    with st.expander(label):
        if res:
            st.markdown("**Sent to Exira**")
            st.write(res["resolved_query"])
            carried = res.get("carried_context") or {}
            if carried.get("original_ask"):
                st.markdown("**Carried forward**")
                st.write(carried["original_ask"])
            if carried.get("entities"):
                st.caption("entities: " + ", ".join(carried["entities"]))
            if carried.get("filters"):
                st.caption("filters: " + ", ".join(carried["filters"]))
            if res.get("unresolved_slots"):
                st.warning("Still open: " + ", ".join(res["unresolved_slots"]))
            if res.get("notes"):
                st.caption(res["notes"])
        web = (trace or {}).get("web") or {}
        if web.get("citations"):
            st.markdown("**Web sources**")
            for url in web["citations"]:
                st.write(url)
        if web.get("error"):
            st.caption(f"web lookup: {web['error']}")


def render_trace(trace: dict):
    if not trace:
        return
    if trace.get("route") == "WEB" and "sql" not in trace:
        return
    if trace.get("route") == "PERSONAL":
        with st.expander("details — answered from your memory"):
            st.write("Route: PERSONAL (no database query)")
        return

    stage = (trace.get("meta") or {}).get("stage", "")
    with st.expander(f"details — text2sql ({stage or 'n/a'})"):
        parts = trace.get("prompt_a_parts") or {}
        if parts.get("interpretation_summary"):
            st.markdown("**Interpretation**")
            st.write(parts["interpretation_summary"])
        if parts.get("sql_plan"):
            st.markdown("**Plan**")
            st.write(parts["sql_plan"])
        if trace.get("sql"):
            st.markdown("**SQL**")
            st.code(trace["sql"], language="sql")
        rows = trace.get("rows") or []
        if rows:
            st.markdown(f"**Rows ({len(rows)})**")
            st.dataframe(pd.DataFrame(rows), use_container_width=True)
        if trace.get("visualization"):
            st.markdown("**Visualization spec**")
            st.json(trace["visualization"])
        if trace.get("error"):
            st.markdown("**Error**")
            st.error(trace["error"])
        if trace.get("prompt_c_raw"):
            st.markdown("**Prompt C raw**")
            st.code(trace["prompt_c_raw"][:4000])


def page_chat():
    sidebar()
    st.title("Ask about your market")

    for turn in S.turns:
        if turn["role"] == "system":
            st.caption(f"— {turn['content']} —")
            continue
        with st.chat_message(turn["role"]):
            st.write(turn["content"])
            if turn["role"] == "assistant":
                render_visual(turn.get("trace"))
                render_resolution(turn.get("trace"))
                render_trace(turn.get("trace"))

    if S.options:
        st.caption("Select an HS domain:" if S.selecting else "Suggested questions:")
        st.markdown("<div class='suggest-zone'>", unsafe_allow_html=True)
        for i, opt in enumerate(S.options):
            if st.button(opt, key=f"opt_{len(S.turns)}_{i}", use_container_width=True):
                push_turn("user", opt)
                S.pending_input = (opt, not S.selecting)
                st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    placeholder = "Pick a domain above, or type HS codes like: HS 52, 61" if S.selecting \
        else "Ask a trade question..."
    typed = st.chat_input(placeholder)
    if typed:
        push_turn("user", typed)
        S.pending_input = (typed, False)
        st.rerun()

    # ---- the question is on screen now; do the work ----
    if S.pending_input:
        text, from_list = S.pending_input
        S.pending_input = None
        with st.spinner("Working..."):
            handle_message(text, from_list=from_list)
        st.rerun()


def end_session():
    current = ""
    if S.append_queries:
        try:
            current = query_summary(S.append_queries)[0]
        except Exception as exc:
            st.warning(f"Session summary failed: {exc}")

    notes = S.old_session
    if current:
        try:
            notes = session_summary(S.old_session, current)
        except Exception as exc:
            st.warning(f"Session merge failed: {exc}")
            notes = current

    try:
        if db.user_exists(S.userid):
            db.update_session_info(S.userid, notes)
        else:
            db.insert_user(S.userid,
                           user_product_info=S.user_product_info,
                           user_session_info=notes)
    except Exception as exc:
        st.error(f"Save failed: {exc}")

    S.session_notes = notes or ""
    S.stage = "done"


def page_done():
    st.title("Session saved")
    st.success(f"Saved for {S.userid}.")
    with st.expander("Session notes", expanded=True):
        st.write(S.session_notes or "_nothing recorded_")
    if st.button("Start over"):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()


PAGES = {
    "login": page_login,
    "onboarding": page_onboarding,
    "scope": page_scope,
    "chat": page_chat,
    "done": page_done,
}

PAGES[S.stage]()