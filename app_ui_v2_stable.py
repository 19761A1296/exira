# app_ui_v2.py — Premium Streamlit UI for EXIRA. Does not modify any backend file.
#
# This file is a presentation-only parallel entry point built from app.py.
# Backend modules, routing, prompts, SQL logic, memory, persona and web search are untouched.
#
# Run with:  python -m streamlit run app_ui_v2.py
#
# The CLI modules (src/router.py, src/run_pipeline.py) use input(), which
# Streamlit cannot drive. This file reimplements those two loops as UI steps
# while calling the same backend functions. The onboarding wizard follows
# run_pipeline's flow exactly: DB lookup -> narrow -> pick from list -> scrape
# -> approve each field -> resolve again -> build profile, with 3 attempts.

import json
import re
import html

import altair as alt

import pandas as pd
import streamlit as st

try:
    import plotly.express as px
    import plotly.graph_objects as go
    PLOTLY_AVAILABLE = True
except Exception:
    px = None
    go = None
    PLOTLY_AVAILABLE = False

from engine_adapter import ENGINE, HS2_DOMAIN_MAP
from hs_memory import collect_hs_from_memory

from database import info_database as db
from database.snowflake_db import companies_list, build_query, run_query
from extraction.extracting_urls import get_company_information_links
from reporting.report_generator import init_report, update_report, log_report
from persona.tweaked_exira import build_and_save_persona, load_persona, persona_to_text
from summarizer.session_summarizer import query_summary, session_summary
from summarizer.classifier import classify_query, tag_flow
from summarizer.question_flow_builder import build_flow
from summarizer.connector import FlowRun
from summarizer.analyzer import resolve_query
from summarizer.web_search import web_search, as_context
from summarizer.memory_answer import answer_from_memory
from summarizer.user_summary import build_user_summary

MAX_SESSION_QUERIES = 5
MAX_ATTEMPTS = 3
NARROW_THRESHOLD = 30

st.set_page_config(
    page_title="EXIRA | Trade Intelligence",
    page_icon="✦",
    layout="wide",
    initial_sidebar_state="expanded"
)


# =========================================================
# PREMIUM UI STYLING
# =========================================================

EXIRA_PALETTE = [
    "#5B8CFF",
    "#22D3EE",
    "#8B5CF6",
    "#2DD4BF",
    "#F59E0B",
    "#FB7185",
    "#60A5FA",
    "#34D399",
    "#C084FC",
    "#F97316",
]

st.markdown(
    """
<style>
:root {
  --ex-bg: #07111f;
  --ex-bg2: #0b1727;
  --ex-panel: #0f1d2c;
  --ex-panel2: #13263b;
  --ex-panel3: #0b1828;
  --ex-border: rgba(148, 163, 184, 0.20);
  --ex-primary: #5b8cff;
  --ex-cyan: #22d3ee;
  --ex-purple: #8b5cf6;
  --ex-green: #22c55e;
  --ex-text: #f8fafc;
  --ex-text2: #dce7f5;
  --ex-muted: #94a3b8;
}

html, body, [class*="css"] {
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

.stApp {
  background:
    radial-gradient(circle at 78% 0%, rgba(91,140,255,.12), transparent 28%),
    radial-gradient(circle at 12% 8%, rgba(34,211,238,.06), transparent 24%),
    linear-gradient(180deg, #07111f 0%, #091321 46%, #07111f 100%) !important;
  color: var(--ex-text) !important;
}

[data-testid="stHeader"] {
  background: #07111f !important;
  border-bottom: 1px solid rgba(148,163,184,.08) !important;
  backdrop-filter: blur(14px);
}

#MainMenu, footer { visibility: hidden; }

.block-container {
  max-width: 1480px;
  padding-top: 1.65rem !important;
  padding-bottom: 7rem !important;
}

/* ---------- sidebar ---------- */
[data-testid="stSidebar"] {
  background: linear-gradient(180deg, #081626 0%, #07111f 100%) !important;
  border-right: 1px solid var(--ex-border) !important;
}
[data-testid="stSidebar"] > div:first-child { padding-top: 1rem !important; }
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] span,
[data-testid="stSidebar"] div,
[data-testid="stSidebar"] label { color: #dce7f5; }

.exira-brand { padding: .65rem .55rem .9rem .55rem; margin-bottom: .25rem; }
.exira-brand-row { display:flex; align-items:center; gap:.68rem; }
.exira-mark {
  width:38px; height:38px; border-radius:12px; display:flex; align-items:center; justify-content:center;
  font-size:1.15rem; font-weight:800; color:#fff;
  background:linear-gradient(135deg,#5b8cff,#22d3ee 60%,#8b5cf6);
  box-shadow:0 10px 28px rgba(91,140,255,.30);
}
.exira-brand-name { color:#fff; font-size:1.12rem; font-weight:800; letter-spacing:.08em; }
.exira-brand-sub { color:#91a2b8; font-size:.72rem; margin-top:.05rem; }
.exira-section-label {
  color:#86a0bf !important; font-size:.66rem; font-weight:800; letter-spacing:.12em;
  text-transform:uppercase; margin:.65rem 0 .45rem 0;
}
.exira-mini-card {
  border:1px solid var(--ex-border); background:#0f1d2c; border-radius:14px;
  padding:.68rem .76rem; margin:.34rem 0;
}
.exira-mini-kicker { color:#7890ab !important; font-size:.65rem; text-transform:uppercase; letter-spacing:.08em; }
.exira-mini-value { color:#f8fafc !important; font-size:.9rem; font-weight:700; margin-top:.12rem; overflow-wrap:anywhere; }
.exira-status-row { display:flex; gap:.38rem; flex-wrap:wrap; margin:.42rem 0 .72rem 0; }
.exira-status-pill {
  border:1px solid rgba(34,197,94,.22); background:rgba(34,197,94,.09); color:#baf7ce !important;
  border-radius:999px; padding:.26rem .48rem; font-size:.67rem;
}

/* ---------- hero ---------- */
.exira-hero {
  border:1px solid var(--ex-border);
  background:linear-gradient(135deg, rgba(15,31,50,.95), rgba(10,24,40,.86));
  border-radius:20px;
  padding:1.18rem 1.25rem 1.12rem 1.25rem;
  margin:.7rem 0 1rem 0 !important;
  box-shadow:0 18px 50px rgba(0,0,0,.14);
  overflow:visible !important;
  position:relative;
  z-index:1;
}
.exira-hero-top { display:flex; align-items:flex-start; justify-content:space-between; gap:1rem; }
.exira-eyebrow {
  color:#70dff0 !important; font-size:.7rem; font-weight:800; letter-spacing:.12em;
  text-transform:uppercase; line-height:1.35 !important; display:block; padding-top:.08rem; margin-bottom:.18rem;
}
.exira-title { color:#f8fafc !important; font-size:1.58rem; font-weight:800; margin-top:0; line-height:1.24 !important; }
.exira-subtitle { color:#b3c0d0 !important; font-size:.88rem; margin-top:.35rem; max-width:800px; line-height:1.58 !important; }
.exira-scope-badge {
  display:inline-flex; align-items:center; gap:.4rem; padding:.5rem .72rem; border-radius:999px;
  background:rgba(91,140,255,.12); border:1px solid rgba(91,140,255,.28); color:#dbe8ff !important;
  font-size:.76rem; line-height:1.15 !important; white-space:nowrap; flex-shrink:0; margin-top:.05rem;
}
.exira-dot { width:7px; height:7px; border-radius:50%; background:#22d3ee; box-shadow:0 0 12px #22d3ee; }

/* ---------- ALL Streamlit buttons: force dark readable surface ---------- */
.stButton > button,
button[data-testid="stBaseButton-secondary"],
button[data-testid="stBaseButton-tertiary"],
button[kind="secondary"],
button[kind="tertiary"] {
  background:#112033 !important;
  background-color:#112033 !important;
  color:#eef5ff !important;
  border:1px solid rgba(148,163,184,.23) !important;
  border-radius:12px !important;
  box-shadow:none !important;
  font-weight:600 !important;
  white-space:normal !important;
  height:auto !important;
  min-height:2.65rem !important;
  line-height:1.35 !important;
  padding:.64rem .8rem !important;
}
.stButton > button *,
button[data-testid="stBaseButton-secondary"] *,
button[data-testid="stBaseButton-tertiary"] *,
button[kind="secondary"] *,
button[kind="tertiary"] * {
  color:#eef5ff !important;
  background:transparent !important;
  white-space:normal !important;
  overflow:visible !important;
  text-overflow:clip !important;
}
.stButton > button:hover,
button[data-testid="stBaseButton-secondary"]:hover,
button[data-testid="stBaseButton-tertiary"]:hover,
button[kind="secondary"]:hover,
button[kind="tertiary"]:hover {
  background:#172c45 !important;
  background-color:#172c45 !important;
  border-color:rgba(91,140,255,.60) !important;
  color:#fff !important;
}
button[data-testid="stBaseButton-primary"],
button[kind="primary"],
.stButton > button[kind="primary"] {
  background:linear-gradient(135deg,#4f7cff,#2f9cf4 55%,#22c7d8) !important;
  background-color:#4f7cff !important;
  color:#fff !important;
  border:none !important;
  box-shadow:0 8px 24px rgba(79,124,255,.24) !important;
}
button[data-testid="stBaseButton-primary"] *,
button[kind="primary"] * { color:#fff !important; background:transparent !important; }
button[data-testid="stBaseButton-primary"]:hover,
button[kind="primary"]:hover {
  filter:brightness(1.06);
  color:#fff !important;
}

/* ---------- chat messages ---------- */
[data-testid="stChatMessage"] {
  background:#0f1d2c !important;
  border:1px solid rgba(148,163,184,.20) !important;
  border-radius:18px !important;
  padding:.82rem .95rem !important;
  margin-bottom:.72rem !important;
  box-shadow:0 10px 30px rgba(0,0,0,.10) !important;
}
[data-testid="stChatMessage"] p,
[data-testid="stChatMessage"] span,
[data-testid="stChatMessage"] div,
[data-testid="stChatMessage"] li,
[data-testid="stChatMessage"] strong,
[data-testid="stChatMessage"] em,
[data-testid="stChatMessage"] h1,
[data-testid="stChatMessage"] h2,
[data-testid="stChatMessage"] h3,
[data-testid="stChatMessage"] h4 { color:#f3f7fb !important; }
[data-testid="stChatMessage"] p,
[data-testid="stChatMessage"] li { line-height:1.62 !important; }
[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"]) {
  background:#13263b !important;
  border-color:rgba(91,140,255,.28) !important;
}

/* ---------- pinned chat input: kill every default white wrapper ---------- */
[data-testid="stBottom"],
[data-testid="stBottom"] > div,
[data-testid="stBottom"] > div > div,
[data-testid="stBottomBlockContainer"],
[data-testid="stBottomBlockContainer"] > div,
[data-testid="stBottomBlockContainer"] > div > div,
[data-testid="stChatFloatingInputContainer"],
.stChatFloatingInputContainer,
section.main > div.block-container ~ div {
  background:#07111f !important;
  background-color:#07111f !important;
  box-shadow:none !important;
  border-color:transparent !important;
}
[data-testid="stBottomBlockContainer"] {
  border-top:1px solid rgba(148,163,184,.08) !important;
  padding-top:.5rem !important;
  padding-bottom:.7rem !important;
}
[data-testid="stBottomBlockContainer"]::before,
[data-testid="stBottomBlockContainer"]::after,
[data-testid="stBottom"]::before,
[data-testid="stBottom"]::after,
[data-testid="stChatFloatingInputContainer"]::before,
[data-testid="stChatFloatingInputContainer"]::after {
  content:none !important;
  display:none !important;
  background:none !important;
  box-shadow:none !important;
}
[data-testid="stChatInput"] {
  background:#0d1b2a !important;
  background-color:#0d1b2a !important;
  border:1px solid rgba(91,140,255,.38) !important;
  border-radius:16px !important;
  box-shadow:0 12px 32px rgba(0,0,0,.25) !important;
  overflow:hidden !important;
}
[data-testid="stChatInput"] > div,
[data-testid="stChatInput"] > div > div,
[data-testid="stChatInput"] [data-baseweb="textarea"] {
  background:#0d1b2a !important;
  background-color:#0d1b2a !important;
}
[data-testid="stChatInput"] textarea {
  background:#0d1b2a !important;
  background-color:#0d1b2a !important;
  color:#f8fafc !important;
  caret-color:#22d3ee !important;
}
[data-testid="stChatInput"] textarea::placeholder { color:#8194aa !important; opacity:1 !important; }
[data-testid="stChatInput"] button {
  background:rgba(91,140,255,.18) !important;
  background-color:rgba(91,140,255,.18) !important;
  color:#b8ccff !important;
  border-radius:10px !important;
}
[data-testid="stChatInput"] button * { color:#b8ccff !important; background:transparent !important; }
[data-testid="stChatInput"] button:hover { background:rgba(91,140,255,.30) !important; color:#fff !important; }

/* ---------- suggested questions ---------- */
.suggest-title { color:#91a6bf !important; font-size:.78rem; font-weight:700; margin:.8rem 0 .48rem 0; }

/* ---------- custom analysis card ---------- */
.exira-analysis-card {
  display:flex; align-items:flex-start; gap:.72rem;
  background:linear-gradient(135deg,#10243a,#0d1c2c);
  border:1px solid rgba(34,211,238,.28);
  border-radius:15px; padding:.8rem .9rem; margin:.75rem 0;
  box-shadow:0 10px 28px rgba(0,0,0,.12);
}
.exira-analysis-pulse {
  width:10px; height:10px; border-radius:50%; margin-top:.3rem; flex:0 0 auto;
  background:#22d3ee; box-shadow:0 0 0 0 rgba(34,211,238,.55);
  animation:exiraPulse 1.25s infinite;
}
@keyframes exiraPulse {
  0% { box-shadow:0 0 0 0 rgba(34,211,238,.55); }
  70% { box-shadow:0 0 0 10px rgba(34,211,238,0); }
  100% { box-shadow:0 0 0 0 rgba(34,211,238,0); }
}
.exira-analysis-title { color:#f8fafc !important; font-weight:750; font-size:.9rem; }
.exira-analysis-sub { color:#9eb0c4 !important; font-size:.76rem; margin-top:.12rem; }

/* ---------- expanders ---------- */
[data-testid="stExpander"],
[data-testid="stExpander"] details,
[data-testid="stExpander"] summary,
[data-testid="stExpander"] [data-testid="stExpanderToggleIcon"],
[data-testid="stExpander"] [data-testid="stExpanderDetails"] {
  background:#0d1b2a !important;
  background-color:#0d1b2a !important;
  color:#e8eef7 !important;
}
[data-testid="stExpander"] {
  border:1px solid rgba(148,163,184,.18) !important;
  border-radius:12px !important;
  overflow:hidden !important;
}
[data-testid="stExpander"] summary { min-height:2.8rem !important; padding:.2rem .7rem !important; }
[data-testid="stExpander"] summary:hover,
[data-testid="stExpander"] details[open] summary { background:#12243a !important; background-color:#12243a !important; }
[data-testid="stExpander"] summary *,
[data-testid="stExpander"] [data-testid="stExpanderDetails"] * { color:#e8eef7 !important; }
[data-testid="stSidebar"] [data-testid="stExpander"],
[data-testid="stSidebar"] [data-testid="stExpander"] details,
[data-testid="stSidebar"] [data-testid="stExpander"] summary,
[data-testid="stSidebar"] [data-testid="stExpander"] [data-testid="stExpanderDetails"] {
  background:#0f2032 !important;
  background-color:#0f2032 !important;
}
[data-testid="stSidebar"] [data-testid="stExpander"] { margin-bottom:.42rem !important; }
[data-testid="stSidebar"] [data-testid="stExpander"] summary:hover,
[data-testid="stSidebar"] [data-testid="stExpander"] details[open] summary {
  background:#162c43 !important;
  background-color:#162c43 !important;
}

/* ---------- developer context ---------- */
.exira-dev-content {
  color:#dce7f5 !important; font-size:.78rem; line-height:1.58; white-space:pre-wrap; overflow-wrap:anywhere;
  background:#081522; border:1px solid rgba(148,163,184,.14); border-radius:9px; padding:.72rem; margin-top:.2rem;
}
.exira-query-item {
  display:flex; align-items:flex-start; gap:.5rem; color:#e2e8f0 !important; font-size:.77rem; line-height:1.45;
  background:#081522; border:1px solid rgba(148,163,184,.14); border-radius:8px; padding:.55rem; margin-bottom:.4rem;
}
.exira-query-item span { color:#e2e8f0 !important; }
.exira-query-num {
  flex:0 0 auto; display:inline-flex; align-items:center; justify-content:center; min-width:21px; height:21px;
  border-radius:6px; background:rgba(91,140,255,.22); color:#dbeafe !important; font-size:.67rem; font-weight:800;
}

/* ---------- tabs/debug blocks ---------- */
[data-baseweb="tab-list"] { background:transparent !important; gap:.15rem !important; }
button[role="tab"] { background:transparent !important; color:#aebed1 !important; }
button[role="tab"] * { color:#aebed1 !important; background:transparent !important; }
button[role="tab"][aria-selected="true"],
button[role="tab"][aria-selected="true"] * { color:#fff !important; }
[data-baseweb="tab-panel"] { background:transparent !important; color:#e8eef7 !important; }
.exira-debug-block {
  background:#07131f; border:1px solid rgba(148,163,184,.17); border-radius:10px;
  padding:.8rem .9rem; margin:.4rem 0; overflow:auto;
  color:#dce7f5 !important; font-family:Consolas, "Courier New", monospace;
  font-size:.76rem; line-height:1.52; white-space:pre-wrap; overflow-wrap:anywhere;
}
.exira-debug-block * { color:#dce7f5 !important; }
.exira-debug-heading { color:#f8fafc !important; font-size:.84rem; font-weight:750; margin:.7rem 0 .25rem 0; }

/* ---------- tables/dataframes ---------- */
[data-testid="stDataFrame"] {
  background:#07131f !important; border:1px solid rgba(148,163,184,.17) !important;
  border-radius:12px !important; overflow:hidden !important;
}

/* ---------- chart cards ---------- */
.exira-chart-card {
  border:1px solid var(--ex-border); border-radius:16px;
  background:linear-gradient(180deg,#102033,#0b1828); padding:.78rem .9rem; margin:.75rem 0 .35rem 0;
}
.exira-chart-title { color:#f8fafc !important; font-size:1rem; font-weight:750; }
.exira-chart-sub { color:#8194aa !important; font-size:.71rem; margin-top:.12rem; }
.exira-route {
  display:inline-flex; align-items:center; gap:.35rem; padding:.24rem .5rem; border-radius:999px;
  background:rgba(139,92,246,.10); border:1px solid rgba(139,92,246,.25); color:#d9c8ff !important;
  font-size:.67rem; margin-top:.28rem;
}

/* ---------- inputs outside chat ---------- */
[data-testid="stTextInput"] input,
[data-testid="stTextArea"] textarea {
  background:#0d1b2a !important; color:#f8fafc !important; border-color:rgba(148,163,184,.20) !important;
}
[data-testid="stTextInput"] input::placeholder,
[data-testid="stTextArea"] textarea::placeholder { color:#7e91a8 !important; opacity:1 !important; }

.stAlert { border-radius:12px !important; }
hr { border-color:rgba(148,163,184,.12) !important; }
.modebar { opacity:.25 !important; }
.modebar:hover { opacity:.82 !important; }
::-webkit-scrollbar { width:8px; height:8px; }
::-webkit-scrollbar-track { background:#081522; }
::-webkit-scrollbar-thumb { background:#294059; border-radius:10px; }
::-webkit-scrollbar-thumb:hover { background:#3b5876; }

@media (max-width:860px) {
  .block-container { padding-left:1rem !important; padding-right:1rem !important; }
  .exira-hero-top { flex-direction:column; }
}
</style>
    """,
    unsafe_allow_html=True,
)


# =========================================================
# UI HELPERS
# =========================================================

def _brand_block():
    brand_html = (
        '<div class="exira-brand">'
        '<div class="exira-brand-row">'
        '<div class="exira-mark">✦</div>'
        '<div>'
        '<div class="exira-brand-name">EXIRA</div>'
        '<div class="exira-brand-sub">'
        'Trade Intelligence Copilot'
        '</div>'
        '</div>'
        '</div>'
        '</div>'
    )

    st.markdown(
        brand_html,
        unsafe_allow_html=True
    )


def _hero(title: str, subtitle: str, badge: str = ""):
    safe_title = html.escape(str(title))
    safe_subtitle = html.escape(str(subtitle))
    safe_badge = html.escape(str(badge))

    if badge:
        badge_html = (
            '<div class="exira-scope-badge">'
            '<span class="exira-dot"></span>'
            f'{safe_badge}'
            '</div>'
        )
    else:
        badge_html = ""

    hero_html = (
        '<div class="exira-hero">'
        '<div class="exira-hero-top">'
        '<div>'
        '<div class="exira-eyebrow">EXIRA Intelligence</div>'
        f'<div class="exira-title">{safe_title}</div>'
        f'<div class="exira-subtitle">{safe_subtitle}</div>'
        '</div>'
        f'{badge_html}'
        '</div>'
        '</div>'
    )

    st.markdown(
        hero_html,
        unsafe_allow_html=True
    )

def _compact_number(value):
    try:
        n = float(value)
    except Exception:
        return str(value)

    sign = "-" if n < 0 else ""
    n = abs(n)

    if n >= 1_000_000_000:
        return f"{sign}{n / 1_000_000_000:.2f}B"

    if n >= 1_000_000:
        return f"{sign}{n / 1_000_000:.2f}M"

    if n >= 1_000:
        return f"{sign}{n / 1_000:.1f}K"

    if n.is_integer():
        return f"{sign}{int(n):,}"

    return f"{sign}{n:,.2f}"


def _number_prefix(
    column: str,
    title: str = ""
) -> str:

    blob = f"{column} {title}".upper()

    if "USD" in blob or "US$" in blob:
        return "$"

    if "INR" in blob or "RUPEE" in blob:
        return "₹"

    return ""


def _plotly_layout(
    fig,
    title: str = "",
    horizontal: bool = False
):

    if not PLOTLY_AVAILABLE:
        return fig

    fig.update_layout(
        title=None,

        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",

        font=dict(
            color="#dce6f4",
            family="Inter, Segoe UI, sans-serif"
        ),

        margin=dict(
            l=18,
            r=18,
            t=18,
            b=18
        ),

        hoverlabel=dict(
            bgcolor="#0f1f32",
            font_color="#f8fafc",
            bordercolor="#31445e"
        ),

        legend=dict(
            bgcolor="rgba(0,0,0,0)",
            orientation="h",
            yanchor="bottom",
            y=1.01,
            xanchor="left",
            x=0,
            font=dict(
                size=11,
                color="#aebed1"
            ),
        ),
    )

    fig.update_xaxes(
        showgrid=False,
        zeroline=False,
        linecolor="rgba(148,163,184,.16)",

        tickfont=dict(
            color="#9fb0c5",
            size=11
        ),

        title_font=dict(
            color="#9fb0c5",
            size=11
        ),
    )

    fig.update_yaxes(
        gridcolor="rgba(148,163,184,.10)",
        zeroline=False,
        linecolor="rgba(148,163,184,.16)",

        tickfont=dict(
            color="#9fb0c5",
            size=11
        ),

        title_font=dict(
            color="#9fb0c5",
            size=11
        ),
    )

    return fig


# =========================================================
# SESSION STATE
# =========================================================

def init_state():

    d = st.session_state

    d.setdefault("stage", "login")

    d.setdefault("userid", "")

    d.setdefault(
        "user_product_info",
        ""
    )

    d.setdefault(
        "old_session",
        ""
    )

    d.setdefault(
        "report",
        None
    )

    d.setdefault(
        "persona",
        None
    )

    d.setdefault(
        "persona_turns",
        []
    )

    d.setdefault(
        "append_queries",
        []
    )

    d.setdefault(
        "sid",
        None
    )

    d.setdefault(
        "turns",
        []
    )

    d.setdefault(
        "options",
        []
    )

    d.setdefault(
        "selecting",
        False
    )

    d.setdefault(
        "pending_confirm",
        None
    )

    d.setdefault(
        "scope_label",
        ""
    )

    d.setdefault(
        "scope_history",
        []
    )

    d.setdefault(
        "hs_code",
        None
    )

    d.setdefault(
        "candidates",
        []
    )

    d.setdefault(
        "show_hs_panel",
        False
    )

    d.setdefault(
        "session_notes",
        ""
    )

    d.setdefault(
        "last_resolution",
        None
    )

    d.setdefault(
        "last_route",
        ""
    )

    d.setdefault(
        "pending_input",
        None
    )

    d.setdefault("flow_run", None)          # a FlowRun paused on an HS pick
    d.setdefault("last_flow", None)
    d.setdefault("flow_text", "")           # what the user typed
    d.setdefault("flow_sent", "")           # the resolved query that ran
    d.setdefault("last_trade_trace", None)
    d.setdefault("last_web", None)

    # onboarding wizard

    d.setdefault(
        "onb_step",
        "input"
    )

    d.setdefault(
        "onb_text",
        ""
    )

    d.setdefault(
        "onb_urls",
        ""
    )

    d.setdefault(
        "onb_attempt",
        0
    )

    d.setdefault(
        "onb_scrape_attempt",
        0
    )

    d.setdefault(
        "onb_matches",
        []
    )

    d.setdefault(
        "onb_company",
        ""
    )

    d.setdefault(
        "onb_scraped_info",
        ""
    )

    d.setdefault(
        "onb_scraped_name",
        ""
    )

    d.setdefault(
        "onb_pending",
        {}
    )

    d.setdefault(
        "onb_after_urls",
        ""
    )

    d.setdefault(
        "onb_search_name",
        ""
    )

    d.setdefault(
        "onb_country",
        ""
    )

    d.setdefault(
        "onb_hs2",
        ""
    )

    d.setdefault(
        "onb_next_after_pick",
        ""
    )


init_state()

S = st.session_state


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


# =========================================================
# ONBOARDING
# =========================================================

def onb_reset_for_retry():

    S.onb_matches = []
    S.onb_company = ""
    S.onb_scraped_info = ""
    S.onb_scraped_name = ""
    S.onb_pending = {}
    S.onb_scrape_attempt = 0
    S.onb_country = ""
    S.onb_hs2 = ""


def onb_lookup(
    name: str,
    country: str = "",
    hs2: str = ""
) -> list:

    if not name:
        return []

    try:

        return (
            companies_list(
                name,
                limit=500,
                country=country or None,
                hs2=hs2 or None
            )
            or []
        )

    except TypeError:

        if country or hs2:

            st.warning(
                "companies_list does not support "
                "country/HS filters yet."
            )

        try:

            return (
                companies_list(name)
                or []
            )

        except Exception as exc:

            st.error(
                f"Company lookup failed: {exc}"
            )

            return []

    except Exception as exc:

        st.error(
            f"Company lookup failed: {exc}"
        )

        return []


def onb_scrape(urls: str) -> dict:

    try:

        return (
            get_company_information_links(
                urls
            )
            or {}
        )

    except Exception as exc:

        st.error(
            f"Scrape failed: {exc}"
        )

        return {}


def onb_build_profile(
    company_name: str,
    scraped_info: str
):

    data = []

    if company_name:

        try:

            df = run_query(
                build_query(
                    company_name
                )
            )

            if (
                isinstance(
                    df,
                    pd.DataFrame
                )
                and not df.empty
            ):

                data.append(df)

                st.caption(
                    f"Pulled {len(df)} shipment rows "
                    f"for {company_name}."
                )

            else:

                st.caption(
                    f"No shipment rows found "
                    f"for {company_name}."
                )

        except Exception as exc:

            st.warning(
                f"Snowflake query failed: {exc}"
            )

    if scraped_info:

        data.append(
            scraped_info
        )

    update_report(
        S.report,

        source=(
            "db_pipeline"
            if company_name
            else "scraped_info"
        )
    )

    summary = ""

    try:

        summary = build_user_summary(
            company_name,
            data
        )

    except Exception as exc:

        st.error(
            f"Profile build failed: {exc}"
        )

    log_report(
        S.report,
        tag="ui-onboarding"
    )

    S.user_product_info = (
        summary
        or ""
    )

    S.old_session = ""

    S.persona = load_persona(
        S.userid
    )

    S.candidates = collect_hs_from_memory(
        build_memory(),
        HS2_DOMAIN_MAP
    )

    S.stage = "scope"


def onb_fail_or_retry(
    reason: str
):

    st.warning(reason)

    if (
        S.onb_attempt
        >= MAX_ATTEMPTS
    ):

        st.error(
            f"Max attempts "
            f"({MAX_ATTEMPTS}) reached."
        )

        update_report(
            S.report,
            source=None
        )

        log_report(
            S.report,
            tag="exhausted"
        )

        if st.button(
            "Continue without a profile"
        ):

            S.user_product_info = ""
            S.old_session = ""

            S.persona = load_persona(
                S.userid
            )

            S.candidates = []

            S.stage = "scope"

            st.rerun()

        return

    st.caption(
        f"Attempt {S.onb_attempt} "
        f"of {MAX_ATTEMPTS}."
    )

    extra = st.text_input(
        "Add more information "
        "and try again",
        key="onb_more"
    ).strip()

    if st.button(
        "Try again",
        type="primary"
    ):

        if extra:

            S.onb_text = (
                f"{S.onb_text} "
                f"{extra}"
            ).strip()

        S.onb_urls = ""

        onb_reset_for_retry()

        S.onb_step = "input"

        st.rerun()


def enter_pick(
    matches: list,
    search_name: str,
    next_step: str
):

    S.onb_matches = matches

    S.onb_search_name = (
        search_name
    )

    S.onb_next_after_pick = (
        next_step
    )

    S.onb_step = (
        "narrow"
        if len(matches) > NARROW_THRESHOLD
        else next_step
    )


# =========================================================
# ONBOARDING STEP 1
# =========================================================

def step_input():

    st.subheader(
        "Step 1 — who are you?"
    )

    st.info(
        "Fill in at least one of the two. "
        "Both is better."
    )

    text = st.text_area(
        "Company name / description "
        "(optional)",
        value=S.onb_text,
        height=100
    ).strip()

    urls = st.text_area(
        "Company URLs, one or more "
        "(optional)",
        value=S.onb_urls,
        height=80
    ).strip()

    if not st.button(
        "Continue",
        type="primary"
    ):
        return

    if not text and not urls:

        st.error(
            "Enter a company name "
            "or at least one URL."
        )

        return

    S.onb_text = text
    S.onb_urls = urls

    S.onb_attempt += 1

    if S.report is None:

        S.report = init_report()

    update_report(
        S.report,
        attempt=S.onb_attempt,
        company_input=text,
        url=urls
    )

    if text:

        matches = onb_lookup(
            text
        )

        if matches:

            enter_pick(
                matches,
                text,
                "pick_text"
            )

            st.rerun()

    if urls:

        S.onb_step = (
            "review_urls"
        )

    else:

        S.onb_after_urls = (
            "review_urls"
        )

        S.onb_step = (
            "ask_urls"
        )

    st.rerun()


# =========================================================
# NARROW COMPANY LIST
# =========================================================

def step_narrow():

    st.subheader(
        "Too many matches — narrow it down"
    )

    st.caption(
        f"{len(S.onb_matches)} companies "
        f"matched “{S.onb_search_name}”."
    )

    active = []

    if S.onb_country:

        active.append(
            f"country = {S.onb_country}"
        )

    if S.onb_hs2:

        active.append(
            f"HS {S.onb_hs2}"
        )

    if active:

        st.caption(
            "Filters applied: "
            + ", ".join(active)
        )

    country = st.text_input(
        "Country "
        "(leave blank to skip)",
        value=S.onb_country,
        key="nw_country"
    ).strip()

    hs2 = ""

    if (
        S.onb_country
        or country
    ):

        hs2 = st.text_input(
            "HS code — first 2 digits are used "
            "(leave blank to skip)",
            value=S.onb_hs2,
            key="nw_hs2"
        ).strip()

    c1, c2 = st.columns(2)

    if c1.button(
        "Apply filter",
        type="primary",
        use_container_width=True
    ):

        filtered = onb_lookup(
            S.onb_search_name,
            country=country,
            hs2=hs2
        )

        if filtered:

            S.onb_matches = (
                filtered
            )

            S.onb_country = (
                country
            )

            S.onb_hs2 = (
                hs2
            )

            if (
                len(filtered)
                <= NARROW_THRESHOLD
            ):

                S.onb_step = (
                    S.onb_next_after_pick
                )

            st.rerun()

        else:

            st.warning(
                "Nothing matched those filters — "
                "the previous list is kept."
            )

    if c2.button(
        f"Show all "
        f"{len(S.onb_matches)} anyway",
        use_container_width=True
    ):

        S.onb_step = (
            S.onb_next_after_pick
        )

        st.rerun()


# =========================================================
# PICK COMPANY FROM TEXT SEARCH
# =========================================================

def step_pick_text():

    st.subheader(
        "Step 2 — which company is yours?"
    )

    st.caption(
        f"Matches for "
        f"“{S.onb_search_name or S.onb_text}” "
        f"in the trade database:"
    )

    if (
        len(S.onb_matches)
        > NARROW_THRESHOLD
        and st.button(
            "🔎 Narrow this list"
        )
    ):

        S.onb_step = "narrow"
        st.rerun()

    for i, name in enumerate(
        S.onb_matches,
        1
    ):

        if st.button(
            f"{i}. {name}",
            key=f"pt_{i}",
            use_container_width=True
        ):

            S.onb_company = name

            update_report(
                S.report,
                company_name=name,
                company_found_in_db=True
            )

            if S.onb_urls:

                S.onb_step = (
                    "scrape_known"
                )

            else:

                S.onb_after_urls = (
                    "scrape_known"
                )

                S.onb_step = (
                    "ask_urls"
                )

            st.rerun()

    st.divider()

    if st.button(
        "None of these"
    ):

        if S.onb_urls:

            S.onb_step = (
                "review_urls"
            )

        else:

            S.onb_after_urls = (
                "review_urls"
            )

            S.onb_step = (
                "ask_urls"
            )

        st.rerun()


# =========================================================
# ASK URLS
# =========================================================

def step_ask_urls():

    st.subheader(
        "Links"
    )

    if S.onb_company:

        st.caption(
            f"Found **{S.onb_company}** "
            "in the database. "
            "Links are optional extra context."
        )

    else:

        st.caption(
            "Not found in the database. "
            "Links are needed to build a profile."
        )

    urls = st.text_input(
        "Company URLs (optional)",
        key="onb_ask_urls"
    ).strip()

    c1, c2 = st.columns(2)

    if (
        c1.button(
            "Use these links",
            type="primary"
        )
        and urls
    ):

        S.onb_urls = urls

        update_report(
            S.report,
            url=urls
        )

        S.onb_step = (
            S.onb_after_urls
        )

        st.rerun()

    if c2.button(
        "Skip"
    ):

        if S.onb_company:

            onb_build_profile(
                S.onb_company,
                ""
            )

            st.rerun()

        else:

            onb_fail_or_retry(
                "No company and no links, "
                "so nothing to work with."
            )


# =========================================================
# SCRAPE KNOWN COMPANY
# =========================================================

def step_scrape_known():

    st.subheader(
        f"Step 3 — extra context "
        f"for {S.onb_company}"
    )

    if not S.onb_pending:

        S.onb_scrape_attempt += 1

        with st.spinner(
            f"Reading "
            f"{S.onb_urls} ..."
        ):

            S.onb_pending = (
                onb_scrape(
                    S.onb_urls
                )
            )

    info = (
        S.onb_pending.get(
            "company_info"
        )
        or ""
    )

    st.caption(
        f"Attempt "
        f"{S.onb_scrape_attempt} "
        f"of {MAX_ATTEMPTS}"
    )

    if info:

        st.markdown(
            "**Scraped information**"
        )

        st.write(
            info
        )

    else:

        st.write(
            "_Nothing was scraped "
            "from those links._"
        )

    c1, c2, c3 = st.columns(3)

    if c1.button(
        "✅ Accept",
        type="primary",
        use_container_width=True
    ):

        update_report(
            S.report,
            scraped_info=info,
            source="db_pipeline"
        )

        onb_build_profile(
            S.onb_company,
            info
        )

        st.rerun()

    if c2.button(
        "🚫 Skip links",
        use_container_width=True
    ):

        onb_build_profile(
            S.onb_company,
            ""
        )

        st.rerun()

    with c3:

        retry_disabled = (
            S.onb_scrape_attempt
            >= MAX_ATTEMPTS
        )

        new_urls = st.text_input(
            "Different links",
            key="sk_urls",
            disabled=retry_disabled
        ).strip()

        if st.button(
            "🔁 Retry",
            use_container_width=True,
            disabled=retry_disabled
        ):

            if new_urls:

                S.onb_urls = (
                    new_urls
                )

                update_report(
                    S.report,
                    url=new_urls
                )

            S.onb_pending = {}

            st.rerun()


# =========================================================
# REVIEW URL EXTRACTION
# =========================================================

def step_review_urls():

    st.subheader(
        "Step 3 — reading your links"
    )

    if not S.onb_pending:

        S.onb_scrape_attempt += 1

        with st.spinner(
            f"Reading "
            f"{S.onb_urls} ..."
        ):

            S.onb_pending = (
                onb_scrape(
                    S.onb_urls
                )
            )

    name = (
        S.onb_pending.get(
            "company_name"
        )
        or ""
    )

    info = (
        S.onb_pending.get(
            "company_info"
        )
        or ""
    )

    st.caption(
        f"Attempt "
        f"{S.onb_scrape_attempt} "
        f"of {MAX_ATTEMPTS}"
    )

    st.markdown(
        "**company_name**"
    )

    if name:

        st.code(
            name
        )

        take_name = st.checkbox(
            "Accept this company name",
            value=True,
            key="rv_name"
        )

    else:

        st.write(
            "_nothing scraped_"
        )

        take_name = False

    st.markdown(
        "**company_info**"
    )

    if info:

        st.write(
            info
        )

        take_info = st.checkbox(
            "Accept this information",
            value=True,
            key="rv_info"
        )

    else:

        st.write(
            "_nothing scraped_"
        )

        take_info = False

    st.divider()

    c1, c2 = st.columns(2)

    if c1.button(
        "Continue",
        type="primary",
        use_container_width=True
    ):

        S.onb_scraped_name = (
            name
            if take_name
            else ""
        )

        S.onb_scraped_info = (
            info
            if take_info
            else ""
        )

        if S.onb_scraped_info:

            update_report(
                S.report,
                scraped_info=
                    S.onb_scraped_info
            )

        if S.onb_scraped_name:

            matches = onb_lookup(
                S.onb_scraped_name
            )

            if matches:

                enter_pick(
                    matches,
                    S.onb_scraped_name,
                    "pick_url"
                )

                st.rerun()

        if S.onb_scraped_info:

            update_report(
                S.report,
                company_name=
                    S.onb_scraped_name,
                company_found_in_db=False,
                source="scraped_info"
            )

            onb_build_profile(
                S.onb_scraped_name,
                S.onb_scraped_info
            )

            st.rerun()

        onb_fail_or_retry(
            "Nothing was accepted "
            "from those links."
        )

    with c2:

        retry_disabled = (
            S.onb_scrape_attempt
            >= MAX_ATTEMPTS
        )

        new_urls = st.text_input(
            "Different links",
            key="rv_urls",
            disabled=
                retry_disabled
        ).strip()

        if st.button(
            "🔁 Retry scrape",
            use_container_width=True,
            disabled=
                retry_disabled
        ):

            if new_urls:

                S.onb_urls = (
                    new_urls
                )

                update_report(
                    S.report,
                    url=new_urls
                )

            S.onb_pending = {}

            st.rerun()


# =========================================================
# PICK COMPANY FROM URL NAME
# =========================================================

def step_pick_url():

    st.subheader(
        "Found in the database"
    )

    st.caption(
        f"Matches for "
        f"“{S.onb_scraped_name}”:"
    )

    if (
        len(S.onb_matches)
        > NARROW_THRESHOLD
        and st.button(
            "🔎 Narrow this list"
        )
    ):

        S.onb_step = (
            "narrow"
        )

        st.rerun()

    for i, name in enumerate(
        S.onb_matches,
        1
    ):

        if st.button(
            f"{i}. {name}",
            key=f"pu_{i}",
            use_container_width=True
        ):

            update_report(
                S.report,
                company_name=name,
                company_found_in_db=True,
                source="db_pipeline"
            )

            onb_build_profile(
                name,
                S.onb_scraped_info
            )

            st.rerun()

    st.divider()

    if st.button(
        "None of these"
    ):

        update_report(
            S.report,
            company_name=
                S.onb_scraped_name,
            company_found_in_db=False,
            source="scraped_info"
        )

        if S.onb_scraped_info:

            onb_build_profile(
                S.onb_scraped_name,
                S.onb_scraped_info
            )

            st.rerun()

        else:

            onb_fail_or_retry(
                "No database match "
                "and no scraped information."
            )


ONB_STEPS = {
    "input":
        step_input,

    "narrow":
        step_narrow,

    "pick_text":
        step_pick_text,

    "ask_urls":
        step_ask_urls,

    "scrape_known":
        step_scrape_known,

    "review_urls":
        step_review_urls,

    "pick_url":
        step_pick_url,
}


def page_onboarding():

    _hero(
        "Set up your EXIRA profile",

        "Connect your company identity "
        "so EXIRA can initialize the same "
        "onboarding flow and trade context "
        "used by the current app.",

        f"User · {S.userid}",
    )

    ONB_STEPS[
        S.onb_step
    ]()

    with st.sidebar:

        _brand_block()

        st.markdown(
            '<div class="exira-section-label">'
            'Onboarding'
            '</div>',
            unsafe_allow_html=True
        )

        st.write(
            f"Step: "
            f"`{S.onb_step}`"
        )

        with st.expander(
            "Onboarding report"
        ):

            report_text = json.dumps(S.report or {}, indent=2, ensure_ascii=False, default=str)
            st.markdown(
                '<div class="exira-debug-block">'
                f'{html.escape(report_text)}'
                '</div>',
                unsafe_allow_html=True,
            )


# =========================================================
# CHAT CORE
# =========================================================

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

            st.warning(
                f"Query summary failed: "
                f"{exc}"
            )

    if S.userid:

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
                    S.userid,
                    S.persona_turns,
                    recent_turns=1,
                    persona=S.persona
                )
            )

        except Exception as exc:

            st.warning(
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

    with st.spinner(
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
    return answer_from_memory(question, build_memory())


def ui_web(question: str) -> str:
    with st.spinner("Looking this up on the web..."):
        result = web_search(question, build_memory())
    S.last_web = result
    return as_context(result, "From the web")


def ui_trade(question: str) -> dict:
    """One sub-question through the engine. Auto-proceeds past the confirm gate.

    Returns {"text", "ok", "pending"}. pending means the engine wants an HS
    domain pick before it can go any further.
    """
    resp = ENGINE.send(S.sid, question)
    parsed = consume(resp)

    if parsed["options"]:
        S.options = parsed["options"]
    S.selecting = ENGINE.scope_pending(S.sid)
    sync_scope()

    if S.selecting:
        # show the domain question so the user knows what is being asked
        for t in parsed["texts"]:
            push_turn("assistant", t)
        return {"text": "", "ok": False, "pending": True}

    trace = parsed["trace"]

    if parsed["confirm"]:
        done = ENGINE.confirm(S.sid, "proceed")
        parsed = consume(done)
        if parsed["options"]:
            S.options = parsed["options"]
        sync_scope()
        trace = parsed["trace"] or trace

    if trace:
        S.last_trade_trace = trace

    text = "\n".join(t for t in parsed["texts"] if t).strip()
    return {"text": text, "ok": trace_ok(trace), "pending": False}


FLOW_HANDLERS = {"personal": ui_personal, "trade": ui_trade, "web": ui_web}


# ─────────────── the flow ───────────────

def finish_flow(out: dict):
    """Called with a FlowRun result. Pushes the answer, or waits for a pick."""
    if out["status"] == "needs_hs_pick":
        # ui_trade already showed the question and the options; S.selecting is on
        return

    S.flow_run = None
    trace = dict(S.last_trade_trace or {})
    trace["route"] = S.last_route
    trace["resolution"] = S.last_resolution
    trace["flow"] = S.last_flow
    trace["parts"] = out.get("answers") or {}
    trace["asked"] = out.get("asked") or {}
    if S.last_web:
        trace["web"] = S.last_web

    push_turn("assistant", out["answer"], trace)
    record_query(memory_entry(S.flow_text, S.flow_sent))


def start_flow(text: str, sent: str, memory: dict):
    """Build, tag and start a flow for one resolved query."""
    flow = tag_flow(build_flow(sent, memory), memory)
    S.last_flow = flow
    S.last_route = (flow["questions"][0].get("route", "TRADE")
                    if len(flow["questions"]) == 1 else "FLOW")
    S.flow_text, S.flow_sent = text, sent
    S.last_trade_trace = None
    S.last_web = None

    S.flow_run = FlowRun(flow, memory, FLOW_HANDLERS, original_question=sent)
    finish_flow(S.flow_run.start())


def handle_message(text: str, from_list: bool = False):
    text = (text or "").strip()
    if not text:
        return

    # the user turn is already on screen; drop it from the history we analyse
    history = chat_history()
    while history and history[-1]["role"] == "USER":
        history.pop()

    ENGINE.inject_memory(S.sid, build_memory())

    # ---- an HS domain pick, possibly in the middle of a flow ----
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

    # ---- a suggested question is already standalone: today's path ----
    if from_list:
        S.last_resolution = None
        S.last_route = "TRADE"
        sent = text
        try:
            parsed = apply_engine_response(
                ENGINE.send(S.sid, sent, source="followup")
            )
            attach_meta()
            if S.selecting:
                return
            if parsed["confirm"]:
                run_confirm("proceed", sent, memory, "TRADE")
                return
            maybe_web(sent, memory, "TRADE")
        except Exception as exc:
            push_turn("assistant", f"Error: {exc}")
            return
        record_query(memory_entry(text, sent))
        return

    # ---- resolve, then split, tag and walk ----
    S.last_resolution = resolve_query(text, history, memory)

    if S.last_resolution["blocked"]:
        push_turn("assistant", S.last_resolution["message"],
                  {"route": "BLOCKED", "resolution": S.last_resolution})
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


def start_engine_session(
    opening: str
):

    S.sid = (
        ENGINE.start()
    )

    ENGINE.inject_memory(
        S.sid,
        build_memory()
    )

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


# =========================================================
# LOGIN PAGE
# =========================================================

def page_login():

    _hero(
        "Welcome to EXIRA",

        "Your trade-intelligence workspace "
        "for products, markets, buyers, "
        "suppliers and shipment analytics.",

        "Secure workspace",
    )

    left, center, right = st.columns(
        [1.15, 1.7, 1.15]
    )

    with center:

        with st.container(
            border=True
        ):

            st.markdown(
                "### Sign in"
            )

            st.caption(
                "Enter your user ID "
                "to load your company context "
                "and continue."
            )

            try:

                db.init_db()

            except Exception as exc:

                st.error(
                    f"Database init failed: "
                    f"{exc}"
                )

            userid = st.text_input(
                "User ID",
                key="login_userid",
                placeholder=
                    "e.g. baxter_user_001"
            ).strip()

            if not st.button(
                "Continue to EXIRA",
                type="primary",
                use_container_width=True
            ):

                return

            if not userid:

                st.error(
                    "User id cannot be empty."
                )

                return

            S.userid = (
                userid
            )

            try:

                exists = (
                    db.user_exists(
                        userid
                    )
                )

            except Exception as exc:

                st.error(
                    f"Lookup failed: "
                    f"{exc}"
                )

                return

            if exists:

                S.user_product_info = (
                    db.get_product_info(
                        userid
                    )
                    or ""
                )

                S.old_session = (
                    db.get_past_sesion_info(
                        userid
                    )
                    or ""
                )

                S.persona = (
                    load_persona(
                        userid
                    )
                )

                S.candidates = (
                    collect_hs_from_memory(
                        build_memory(),
                        HS2_DOMAIN_MAP
                    )
                )

                S.stage = "scope"

            else:

                S.stage = (
                    "onboarding"
                )

            st.rerun()


# =========================================================
# START SCOPE
# =========================================================

def begin_with(
    choice: str
):

    code = re.sub(
        r"[.\-\s]",
        "",
        choice
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

        opening = (
            f"HS {code}"
        )

    else:

        S.hs_code = None

        S.scope_label = (
            choice
        )

        opening = (
            choice
        )

    S.stage = "chat"

    start_engine_session(
        opening
    )

    S.scope_history = (
        [S.scope_label]
        if S.scope_label
        else []
    )


def page_scope():

    _hero(
        "Choose your trade scope",

        "Start from an HS code already "
        "associated with your profile, "
        "or enter a new HS code "
        "or product name.",

        S.userid
        or "Profile loaded",
    )

    if S.candidates:

        st.markdown(
            '<div class="exira-section-label">'
            'From your profile'
            '</div>',
            unsafe_allow_html=True
        )

        cols = st.columns(
            2
        )

        for i, c in enumerate(
            S.candidates
        ):

            with cols[
                i % 2
            ]:

                if st.button(
                    f"HS {c['code']}  ·  "
                    f"{c['domain']}",

                    key=
                        f"cand_{c['code']}",

                    use_container_width=True,
                ):

                    begin_with(
                        c["code"]
                    )

                    st.rerun()

        st.divider()

    st.markdown(
        '<div class="exira-section-label">'
        'Custom scope'
        '</div>',
        unsafe_allow_html=True
    )

    c1, c2 = st.columns(
        [4, 1]
    )

    with c1:

        typed = st.text_input(
            "HS code or product",
            label_visibility="collapsed",
            placeholder=
                "Enter an HS code "
                "or product name…",
        ).strip()

    with c2:

        if (
            st.button(
                "Start analysis",
                type="primary",
                use_container_width=True
            )
            and typed
        ):

            begin_with(
                typed
            )

            st.rerun()


# =========================================================
# SIDEBAR
# =========================================================

def sidebar():
    with st.sidebar:
        _brand_block()

        st.markdown(
            '<div class="exira-section-label">Workspace</div>',
            unsafe_allow_html=True,
        )

        st.markdown(
            '<div class="exira-mini-card">'
            '<div class="exira-mini-kicker">User</div>'
            f'<div class="exira-mini-value">{html.escape(str(S.userid or "—"))}</div>'
            '</div>',
            unsafe_allow_html=True,
        )

        st.markdown(
            '<div class="exira-mini-card">'
            '<div class="exira-mini-kicker">Active scope</div>'
            f'<div class="exira-mini-value">{html.escape(str(S.scope_label or "—"))}</div>'
            '</div>',
            unsafe_allow_html=True,
        )

        st.markdown(
            '<div class="exira-status-row">'
            '<span class="exira-status-pill">● Company memory</span>'
            '<span class="exira-status-pill">● Persona</span>'
            '<span class="exira-status-pill">● Session context</span>'
            '</div>',
            unsafe_allow_html=True,
        )

        if len(S.scope_history) > 1:
            with st.expander(f"Scope history · {len(S.scope_history)}"):
                for i, scope_name in enumerate(S.scope_history, 1):
                    marker = " ← current" if i == len(S.scope_history) else ""
                    st.write(f"{i}. {scope_name}{marker}")

        if st.button("⇄ Change HS / product scope", use_container_width=True):
            S.show_hs_panel = not S.show_hs_panel

        if S.show_hs_panel:
            with st.container(border=True):
                st.caption("HS codes from your memory")

                if not S.candidates:
                    st.write("None found.")

                for c in S.candidates:
                    if st.button(
                        f"HS {c['code']} · {c['domain']}",
                        key=f"sw_{c['code']}",
                        use_container_width=True,
                    ):
                        switch_scope(c["code"])
                        st.rerun()

                other = st.text_input("Other code or product", key="sw_other").strip()

                if st.button("Use this scope", key="sw_go", use_container_width=True) and other:
                    switch_scope(other)
                    st.rerun()

        st.divider()

        st.markdown(
            '<div class="exira-section-label">Developer context</div>',
            unsafe_allow_html=True,
        )

        with st.expander("🏢 Company profile"):
            if S.user_product_info:
                st.markdown(
                    '<div class="exira-dev-content">'
                    f'{html.escape(str(S.user_product_info))}'
                    '</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.caption("No company profile loaded.")

        with st.expander("🕘 Previous session"):
            if S.old_session:
                st.markdown(
                    '<div class="exira-dev-content">'
                    f'{html.escape(str(S.old_session))}'
                    '</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.caption("No previous session memory.")

        with st.expander(f"💬 Current queries ({len(S.append_queries)})"):
            if S.append_queries:
                for i, query in enumerate(S.append_queries, 1):
                    st.markdown(
                        '<div class="exira-query-item">'
                        f'<span class="exira-query-num">{i}</span>'
                        f'<span>{html.escape(str(query))}</span>'
                        '</div>',
                        unsafe_allow_html=True,
                    )
            else:
                st.caption("No queries in the current session.")

        with st.expander("🧠 Persona"):
            if S.persona:
                persona_text = persona_to_text(S.persona)
                st.markdown(
                    '<div class="exira-dev-content">'
                    f'{html.escape(str(persona_text))}'
                    '</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.caption("No persona loaded.")

        with st.expander("📋 Onboarding report"):
            if S.report:
                report_text = json.dumps(S.report, indent=2, ensure_ascii=False, default=str)
                st.markdown(
                    '<div class="exira-debug-block">'
                    f'{html.escape(report_text)}'
                    '</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.caption("No onboarding report for this user.")

        st.divider()

        if st.button("End session & save", type="primary", use_container_width=True):
            end_session()
            st.rerun()


# =========================================================
# PREMIUM CHART RENDERER
# =========================================================

def render_visual(
    trace: dict
):

    if not trace:

        return

    spec = (
        trace.get(
            "visualization"
        )
        or {}
    )

    rows = (
        trace.get(
            "rows"
        )
        or []
    )

    vtype = spec.get(
        "type",
        "none"
    )

    if (
        vtype == "none"
        or not rows
    ):

        return

    df = pd.DataFrame(
        rows
    )

    title = (
        spec.get(
            "title"
        )
        or "Trade analysis"
    )

    x = spec.get(
        "x_column"
    )

    y = spec.get(
        "y_column"
    )

    series = spec.get(
        "series_column"
    )

    trend = spec.get(
        "trendline_column"
    )
    st.markdown(
        '<div class="exira-chart-card">'
        f'<div class="exira-chart-title">{html.escape(str(title))}</div>'
        '<div class="exira-chart-sub">Interactive view generated from the returned trade data</div>'
        '</div>',
        unsafe_allow_html=True,
    )

    try:

        if (
            vtype
            == "structured_table"
        ):

            cols = [
                c
                for c in (
                    spec.get(
                        "columns"
                    )
                    or []
                )
                if c
                in df.columns
            ]

            view = (
                df[cols]
                if cols
                else df
            )

            st.dataframe(
                view,
                use_container_width=True,
                hide_index=True,
                height=min(
                    520,
                    42
                    + 35
                    * min(
                        len(view),
                        14
                    )
                )
            )

            return

        if (
            not x
            or not y
            or x not in df.columns
            or y not in df.columns
        ):

            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True
            )

            return

        plot_df = (
            df.copy()
        )

        plot_df[
            y
        ] = pd.to_numeric(
            plot_df[y],
            errors="coerce"
        )

        if (
            series
            and series
            in plot_df.columns
        ):

            plot_df[
                series
            ] = (
                plot_df[
                    series
                ].astype(
                    str
                )
            )

        if (
            plot_df[y]
            .notna()
            .sum()
            == 0
        ):

            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True
            )

            return

        prefix = _number_prefix(
            y,
            title
        )

        label_map = {
            x:
                x.replace(
                    "_",
                    " "
                ).title(),

            y:
                y.replace(
                    "_",
                    " "
                ).title()
        }

        if PLOTLY_AVAILABLE:

            fig = None

            # BAR CHART

            if (
                vtype
                == "bar_chart"
            ):

                labels = (
                    plot_df[
                        x
                    ].astype(
                        str
                    )
                )

                horizontal = (
                    len(plot_df) >= 6
                    or labels
                    .str
                    .len()
                    .mean()
                    > 13
                )

                if horizontal:

                    fig = px.bar(
                        plot_df,
                        x=y,
                        y=x,

                        color=(
                            series
                            if (
                                series
                                and series
                                in plot_df.columns
                            )
                            else None
                        ),

                        orientation="h",

                        labels=
                            label_map,

                        color_discrete_sequence=
                            EXIRA_PALETTE,

                        barmode=
                            "group",
                    )

                    fig.update_yaxes(
                        categoryorder=
                            "array",

                        categoryarray=
                            list(
                                reversed(
                                    labels.tolist()
                                )
                            )
                    )

                    fig.update_traces(
                        texttemplate=
                            "%{x:.3s}",

                        textposition=
                            "outside",

                        cliponaxis=
                            False
                    )

                else:

                    fig = px.bar(
                        plot_df,
                        x=x,
                        y=y,

                        color=(
                            series
                            if (
                                series
                                and series
                                in plot_df.columns
                            )
                            else None
                        ),

                        labels=
                            label_map,

                        color_discrete_sequence=
                            EXIRA_PALETTE,

                        barmode=
                            "group",
                    )

                    fig.update_traces(
                        texttemplate=
                            "%{y:.3s}",

                        textposition=
                            "outside",

                        cliponaxis=
                            False
                    )

                fig = _plotly_layout(
                    fig,
                    title,
                    horizontal=
                        horizontal
                )

            # LINE CHART

            elif (
                vtype
                == "line_chart"
            ):

                if (
                    trend
                    and trend
                    in plot_df.columns
                ):

                    plot_df[
                        trend
                    ] = pd.to_numeric(
                        plot_df[trend],
                        errors="coerce"
                    )

                    fig = go.Figure()

                    fig.add_trace(
                        go.Scatter(
                            x=
                                plot_df[x],

                            y=
                                plot_df[y],

                            mode=
                                "lines+markers",

                            name=
                                y.replace(
                                    "_",
                                    " "
                                ).title(),

                            line=dict(
                                color=
                                    EXIRA_PALETTE[0],

                                width=3
                            ),

                            marker=dict(
                                size=7
                            ),

                            fill=
                                "tozeroy",

                            fillcolor=
                                "rgba(91,140,255,.09)",
                        )
                    )

                    fig.add_trace(
                        go.Scatter(
                            x=
                                plot_df[x],

                            y=
                                plot_df[trend],

                            mode=
                                "lines",

                            name=
                                trend.replace(
                                    "_",
                                    " "
                                ).title(),

                            line=dict(
                                color=
                                    EXIRA_PALETTE[1],

                                width=2,

                                dash=
                                    "dot"
                            ),
                        )
                    )

                else:

                    fig = px.line(
                        plot_df,
                        x=x,
                        y=y,

                        color=(
                            series
                            if (
                                series
                                and series
                                in plot_df.columns
                            )
                            else None
                        ),

                        markers=True,

                        labels=
                            label_map,

                        color_discrete_sequence=
                            EXIRA_PALETTE,
                    )

                    fig.update_traces(
                        line=dict(
                            width=3
                        ),

                        marker=dict(
                            size=7
                        )
                    )

                fig = _plotly_layout(
                    fig,
                    title
                )

            # SCATTER CHART

            elif (
                vtype
                == "scatter_chart"
            ):

                fig = px.scatter(
                    plot_df,
                    x=x,
                    y=y,

                    color=(
                        series
                        if (
                            series
                            and series
                            in plot_df.columns
                        )
                        else None
                    ),

                    labels=
                        label_map,

                    color_discrete_sequence=
                        EXIRA_PALETTE,

                    size_max=18,
                )

                fig.update_traces(
                    marker=dict(
                        size=10,
                        opacity=.82,

                        line=dict(
                            width=1,

                            color=
                                "rgba(255,255,255,.25)"
                        )
                    )
                )

                fig = _plotly_layout(
                    fig,
                    title
                )

            # PIE / DONUT

            elif (
                vtype
                == "pie_chart"
            ):

                fig = px.pie(
                    plot_df,

                    names=x,
                    values=y,

                    hole=.58,

                    color_discrete_sequence=
                        EXIRA_PALETTE,
                )

                fig.update_traces(
                    textposition=
                        "inside",

                    textinfo=
                        "percent",

                    hovertemplate=
                        "<b>%{label}</b>"
                        "<br>%{value:,.2f}"
                        "<br>%{percent}"
                        "<extra></extra>",

                    marker=dict(
                        line=dict(
                            color=
                                "#0b1727",

                            width=2
                        )
                    ),
                )

                fig = _plotly_layout(
                    fig,
                    title
                )

            if fig is not None:

                if prefix:

                    if (
                        vtype
                        == "bar_chart"
                    ):

                        if (
                            getattr(
                                fig.layout,
                                "xaxis",
                                None
                            )
                            and len(plot_df)
                            >= 6
                        ):

                            fig.update_xaxes(
                                tickprefix=
                                    prefix
                            )

                        else:

                            fig.update_yaxes(
                                tickprefix=
                                    prefix
                            )

                    elif vtype in (
                        "line_chart",
                        "scatter_chart"
                    ):

                        fig.update_yaxes(
                            tickprefix=
                                prefix
                        )

                st.plotly_chart(
                    fig,
                    use_container_width=True,

                    config={
                        "displaylogo":
                            False,

                        "responsive":
                            True
                    }
                )

                return

        # ---------------------------------------------
        # SAFE FALLBACK IF PLOTLY IS NOT INSTALLED
        # ---------------------------------------------

        if (
            vtype
            == "bar_chart"
        ):

            if (
                series
                and series
                in df.columns
            ):

                st.bar_chart(
                    df.pivot_table(
                        index=x,
                        columns=series,
                        values=y,
                        aggfunc="sum"
                    )
                )

            else:

                st.bar_chart(
                    df.set_index(
                        x
                    )[y]
                )

        elif (
            vtype
            == "line_chart"
        ):

            ycols = [y]

            if (
                trend
                and trend
                in df.columns
            ):

                ycols.append(
                    trend
                )

            if (
                series
                and series
                in df.columns
                and not trend
            ):

                st.line_chart(
                    df.pivot_table(
                        index=x,
                        columns=series,
                        values=y,
                        aggfunc="sum"
                    )
                )

            else:

                st.line_chart(
                    df.set_index(
                        x
                    )[ycols]
                )

        elif (
            vtype
            == "scatter_chart"
        ):

            st.scatter_chart(
                df,
                x=x,
                y=y,

                color=(
                    series
                    if (
                        series
                        in df.columns
                    )
                    else None
                )
            )

        elif (
            vtype
            == "pie_chart"
        ):

            chart = (
                alt.Chart(
                    df
                )
                .mark_arc(
                    innerRadius=70
                )
                .encode(
                    theta=
                        alt.Theta(
                            f"{y}:Q"
                        ),

                    color=
                        alt.Color(
                            f"{x}:N"
                        ),

                    tooltip=[
                        x,
                        y
                    ]
                )
            )

            st.altair_chart(
                chart,
                use_container_width=True
            )

        else:

            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True
            )

    except Exception as exc:

        st.caption(
            f"Could not draw the chart "
            f"({exc}) — showing the "
            f"data instead."
        )

        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True
        )


# =========================================================
# ROUTING / CONTEXT DISPLAY
# =========================================================

def render_resolution(trace: dict):
    """One collapsed expander under the answer: what was sent, and how it split."""
    trace = trace or {}
    res = trace.get("resolution")
    flow = trace.get("flow") or {}
    route = trace.get("route") or ""
    if not res and not flow and not route:
        return

    bits = []
    if res:
        bits.append(f"{res['relation']} ({res['confidence']})")
    shape = flow.get("flow") if flow else ""
    bits.append(shape or route or "")
    label = "details — " + " → ".join(b for b in bits if b)

    with st.expander(label):
        if res and res.get("resolved_query"):
            st.markdown("**Resolved query**")
            st.write(res["resolved_query"])

        questions = flow.get("questions") or []
        if questions:
            st.markdown(f"**Flow** &nbsp; `{shape}`", unsafe_allow_html=True)
            for q in questions:
                deps = q.get("depends_on") or []
                tail = f" &nbsp;·&nbsp; needs {', '.join(deps)}" if deps else ""
                st.markdown(
                    f"`{q['id']}` &nbsp;**{q.get('route', '?')}**{tail}<br>"
                    f"<span style='color:#5a6b7d'>{q['text']}</span>",
                    unsafe_allow_html=True,
                )
        elif route:
            st.caption(f"Route: {route}")

        if res and res.get("unresolved_slots"):
            st.warning("Still open: " + ", ".join(res["unresolved_slots"]))

        if res and res.get("dropped_note"):
            st.caption(res["dropped_note"])

# =========================================================
# DEVELOPER TRACE
# =========================================================

def _debug_html(value):
    if isinstance(value, (dict, list)):
        text_value = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    else:
        text_value = str(value or "")

    st.markdown(
        '<div class="exira-debug-block">'
        f'{html.escape(text_value)}'
        '</div>',
        unsafe_allow_html=True,
    )


def render_trace(trace: dict):
    if not trace:
        return

    if trace.get("route") == "WEB" and "sql" not in trace:
        return

    if trace.get("route") == "PERSONAL":
        with st.expander("Developer details · memory answer"):
            _debug_html("Route: PERSONAL\nNo database query was executed.")
        return

    stage = (trace.get("meta") or {}).get("stage", "")
    rows = trace.get("rows") or []
    label = f"Developer details · text2sql · {stage or 'n/a'}"

    with st.expander(label):
        tabs = st.tabs(["Overview", "SQL", "Rows", "Visualization", "Prompt C"])

        with tabs[0]:
            parts = trace.get("prompt_a_parts") or {}
            overview = {
                "route": trace.get("route") or "",
                "stage": stage or "",
                "rows_returned": len(rows),
                "has_error": bool(trace.get("error")),
            }
            _debug_html(overview)

            if parts.get("interpretation_summary"):
                st.markdown('<div class="exira-debug-heading">Interpretation</div>', unsafe_allow_html=True)
                st.write(parts["interpretation_summary"])

            if parts.get("sql_plan"):
                st.markdown('<div class="exira-debug-heading">Plan</div>', unsafe_allow_html=True)
                st.write(parts["sql_plan"])

            if trace.get("error"):
                st.error(trace["error"])

        with tabs[1]:
            if trace.get("sql"):
                _debug_html(trace["sql"])
            else:
                st.caption("No SQL attached to this response.")

        with tabs[2]:
            if rows:
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            else:
                st.caption("No rows returned.")

        with tabs[3]:
            if trace.get("visualization"):
                _debug_html(trace["visualization"])
            else:
                st.caption("No visualization specification returned.")

        with tabs[4]:
            if trace.get("prompt_c_raw"):
                _debug_html(str(trace["prompt_c_raw"])[:4000])
            else:
                st.caption("No Prompt C raw output attached.")


# =========================================================
# CHAT PAGE
# =========================================================

def page_chat():
    sidebar()

    _hero(
        "Ask about your market",
        "Explore buyers, suppliers, countries, demand, pricing, trends and shipment activity through your existing EXIRA intelligence pipeline.",
        S.scope_label or "Trade scope",
    )

    if not PLOTLY_AVAILABLE:
        st.info(
            "Plotly is not installed, so EXIRA is using safe Streamlit chart fallbacks. "
            "Run `python -m pip install plotly` for the premium chart renderer."
        )

    for turn in S.turns:
        if turn["role"] == "system":
            st.markdown(
                '<div style="text-align:center;color:#71849b;font-size:.73rem;margin:.45rem 0;">'
                f'— {html.escape(str(turn["content"]))} —'
                '</div>',
                unsafe_allow_html=True,
            )
            continue

        avatar = "🤖" if turn["role"] == "assistant" else "👤"

        with st.chat_message(turn["role"], avatar=avatar):
            if turn["role"] == "assistant":
                st.markdown(turn["content"])
                render_visual(turn.get("trace"))
                render_resolution(turn.get("trace"))
                render_trace(turn.get("trace"))
            else:
                st.write(turn["content"])

    if S.options:
        st.markdown(
            '<div class="suggest-title">'
            + ("Select an HS domain" if S.selecting else "Suggested next questions")
            + '</div>',
            unsafe_allow_html=True,
        )

        # Full-width buttons intentionally avoid truncating long suggestions.
        for i, opt in enumerate(S.options):
            if st.button(
                opt,
                key=f"opt_{len(S.turns)}_{i}",
                use_container_width=True,
            ):
                push_turn("user", opt)
                S.pending_input = (opt, not S.selecting)
                st.rerun()

    placeholder = (
        "Pick a domain above, or type HS codes like: HS 52, 61"
        if S.selecting
        else "Ask EXIRA a trade question…"
    )

    typed = st.chat_input(placeholder)

    if typed:
        push_turn("user", typed)
        S.pending_input = (typed, False)
        st.rerun()

    if S.pending_input:
        text, from_list = S.pending_input
        S.pending_input = None

        analysis_slot = st.empty()
        analysis_slot.markdown(
            '<div class="exira-analysis-card">'
            '<span class="exira-analysis-pulse"></span>'
            '<div>'
            '<div class="exira-analysis-title">EXIRA is analyzing your request…</div>'
            '<div class="exira-analysis-sub">Using your existing routing, context and trade-analysis pipeline.</div>'
            '</div>'
            '</div>',
            unsafe_allow_html=True,
        )

        handle_message(text, from_list=from_list)
        analysis_slot.empty()
        st.rerun()


# =========================================================
# END SESSION
# =========================================================

def end_session():

    current = ""

    if S.append_queries:

        try:

            current = (
                query_summary(
                    S.append_queries
                )[0]
            )

        except Exception as exc:

            st.warning(
                f"Session summary failed: "
                f"{exc}"
            )

    notes = (
        S.old_session
    )

    if current:

        try:

            notes = session_summary(
                S.old_session,
                current
            )

        except Exception as exc:

            st.warning(
                f"Session merge failed: "
                f"{exc}"
            )

            notes = current

    try:

        if db.user_exists(
            S.userid
        ):

            db.update_session_info(
                S.userid,
                notes
            )

        else:

            db.insert_user(
                S.userid,

                user_product_info=
                    S.user_product_info,

                user_session_info=
                    notes
            )

    except Exception as exc:

        st.error(
            f"Save failed: "
            f"{exc}"
        )

    S.session_notes = (
        notes
        or ""
    )

    S.stage = "done"


# =========================================================
# DONE PAGE
# =========================================================

def page_done():

    _hero(
        "Session saved",

        "Your session summary has been "
        "persisted using the existing "
        "EXIRA memory flow.",

        S.userid
        or "Saved",
    )

    st.success(
        f"Saved for "
        f"{S.userid}."
    )

    with st.expander(
        "Session notes",
        expanded=True
    ):

        st.write(
            S.session_notes
            or "_nothing recorded_"
        )

    if st.button(
        "Start a new session",
        type="primary"
    ):

        for k in list(
            st.session_state.keys()
        ):

            del st.session_state[
                k
            ]

        st.rerun()


# =========================================================
# PAGE ROUTER
# =========================================================

PAGES = {
    "login":
        page_login,

    "onboarding":
        page_onboarding,

    "scope":
        page_scope,

    "chat":
        page_chat,

    "done":
        page_done,
}


PAGES[
    S.stage
]()