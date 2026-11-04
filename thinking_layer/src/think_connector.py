# thinking_layer/src/think_connector.py
"""
think_connector: builds the final response of a thinking run.

    user query + engine context + earlier results + all step results
          │
          ▼
    LLM (one call)  →  final answer text + the 1–2 most relevant charts
          │
          ▼
    {"text", "charts", "sources", "error"}

Charts are only picked, never invented: each one is the visualization spec that
text2sql_thinking's Prompt C already produced for a step, together with that
step's rows and columns, so the UI can draw it the normal way.

    from thinking_layer.src.think_connector import connect
    final = connect(query, context, steps)

Also holds format_context(), format_dependencies() and format_step(), which thinking_master uses to
show the same context and step results to its own LLM.
"""

import json
import os
from typing import Any, Dict, List

from thinking_layer.text2sql_thinking.text2sql_thinking_tool import call_gemini, dbg, extract_json_object

CONNECTOR_MAX_TOKENS = int(os.getenv("THINK_CONNECTOR_MAX_TOKENS", "3000"))
CONNECTOR_ROWS_PER_STEP = int(os.getenv("THINK_CONNECTOR_ROWS_PER_STEP", "20"))
MAX_CHARTS = 2
CELL_CHARS = 200

STATUS_OK = "ok"


# =========================================================
# SHARED FORMATTING (also used by thinking_master)
# =========================================================
def format_context(context: Dict[str, Any]) -> str:
    """Engine-session context as compact text. Empty parts are left out."""
    if not context:
        return "(no engine context)"
    lines = []
    if context.get("product_name"):
        lines.append(f"Current product: {context['product_name']}")
    if context.get("hs_code"):
        lines.append(f"Current HS code: {context['hs_code']}")
    if context.get("selected_hs_codes"):
        lines.append(f"Selected HS codes: {', '.join(map(str, context['selected_hs_codes']))}")
    if context.get("product_hs_memory"):
        lines.append(f"Known product → HS: {json.dumps(context['product_hs_memory'], ensure_ascii=False)}")
    if context.get("user_memory"):
        lines.append(f"User / company memory: {json.dumps(context['user_memory'], ensure_ascii=False, default=str)[:3000]}")
    if context.get("recent_turns"):
        turns = "\n".join(f"  {t.get('role')}: {str(t.get('content', ''))[:400]}" for t in context["recent_turns"])
        lines.append(f"Recent conversation:\n{turns}")
    return "\n".join(lines) if lines else "(no engine context)"


def _trim_rows(rows: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    out = []
    for row in (rows or [])[:limit]:
        out.append({k: (v[:CELL_CHARS] + "..." if isinstance(v, str) and len(v) > CELL_CHARS else v)
                    for k, v in row.items()})
    return out


def format_step(step: Dict[str, Any], rows_limit: int) -> str:
    """One step result as text: sub-question, tool, status, response, top rows, sources."""
    lines = [
        f"STEP {step['n']}",
        f"Sub-question: {step['sub_question']}",
        f"Tool: {step['tool']}",
        f"Status: {step['status']}",
    ]
    if step.get("response"):
        lines.append(f"Response: {step['response']}")
    if step.get("error"):
        lines.append(f"Error: {step['error']}")
    if step["tool"] == "text2sql" and step["status"] == STATUS_OK:
        rows = step.get("rows") or []
        shown = min(len(rows), rows_limit)
        lines.append(f"Rows: {len(rows)} total, top {shown} shown; columns: {', '.join(step.get('cols') or [])}")
        for row in _trim_rows(rows, rows_limit):
            lines.append(json.dumps(row, ensure_ascii=False, default=str))
    if step.get("citations"):
        lines.append("Sources: " + ", ".join(step["citations"]))
    return "\n".join(lines)


DEPENDENCY_TEXT_CHARS = 4000


def format_dependencies(dependency_answers: Dict[str, Any]) -> str:
    """Results of other parts of the user's question that this one depends on.

    Comes from the Trade Mini Connector as {"sq1": {"text": ..., "ok": ...}, ...}.
    """
    if not dependency_answers:
        return "(none)"
    blocks = []
    for key, value in dependency_answers.items():
        if isinstance(value, dict):
            text = str(value.get("text") or value.get("answer") or "").strip()
            ok = value.get("ok")
        else:
            text, ok = str(value or "").strip(), None
        if len(text) > DEPENDENCY_TEXT_CHARS:
            text = text[:DEPENDENCY_TEXT_CHARS] + "..."
        status = "" if ok is None else (" (ok)" if ok else " (failed or incomplete)")
        blocks.append(f"[{key}]{status}\n{text or '(no text)'}")
    return "\n\n".join(blocks)


# =========================================================
# PROMPT
# =========================================================
THINK_CONNECTOR_PROMPT = """You write the final answer of a trade analysis for an Exira user.
The analysis was done in steps. Each step result below came either from Exira's trade
shipment database (Tool: text2sql) or from a web search (Tool: web).

USER QUERY
{query}

CONTEXT
{context}

EARLIER RESULTS (other parts of the user's question that this one builds on)
{dependencies}

STEP RESULTS
{steps}

CHART CANDIDATES
{charts}

HOW TO WRITE THE ANSWER
- Answer the user query directly in the first sentence or two.
- Use only facts, numbers and names that appear in the step results or EARLIER RESULTS.
  Never invent or estimate numbers. Keep units, currency and periods exactly as given.
- Facts from the database: present them as "as per our trade records".
  Facts from the web: say they are from public sources.
- Combine the steps into one coherent answer; show how the numbers relate when that
  answers the question (for example share, comparison, concentration).
- If something needed could not be found (a step failed, returned no data, or was out of
  scope), say briefly what could not be determined. Do not hide gaps.
- Use the context only to make the answer relevant (product, HS code, user's business);
  do not treat it as data.
- Do not mention steps, sub-questions, tools, SQL, agents or how the analysis was done.
- Plain text. Short paragraphs or short bullet points. No headings, no markdown tables.
- Usually under 250 words.

HOW TO PICK CHARTS
- From CHART CANDIDATES, pick at most 2 step numbers whose chart best supports the answer.
- Pick none if no chart helps (for example the answer is one number).

OUTPUT
Return only JSON, no other text:
{"answer": "<final answer text>", "chart_steps": [<step numbers, 0 to 2>]}"""


def _chart_candidates(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cands = []
    for s in steps:
        viz = s.get("visualization") or {}
        if s["tool"] == "text2sql" and s["status"] == STATUS_OK and viz.get("type") not in (None, "none"):
            cands.append(s)
    return cands


def _format_candidates(cands: List[Dict[str, Any]]) -> str:
    if not cands:
        return "(none)"
    return "\n".join(
        f"Step {s['n']}: {s['visualization'].get('type')} — {s['visualization'].get('title') or 'untitled'}"
        for s in cands
    )


def _collect_sources(steps: List[Dict[str, Any]]) -> List[str]:
    seen, out = set(), []
    for s in steps:
        for url in s.get("citations") or []:
            if url not in seen:
                seen.add(url)
                out.append(url)
    return out


def _fallback_text(steps: List[Dict[str, Any]]) -> str:
    useful = [s["response"] for s in steps if s["status"] == STATUS_OK and s.get("response")]
    if not useful:
        return "I could not find enough information to answer this question."
    return "\n\n".join(useful)


# =========================================================
# CONNECT
# =========================================================
def connect(query: str, context: Dict[str, Any], steps: List[Dict[str, Any]],
            dependency_answers: Dict[str, Any] = None) -> Dict[str, Any]:
    """Return {"text", "charts", "sources", "error"}. Never raises."""
    cands = _chart_candidates(steps)
    by_n = {s["n"]: s for s in cands}
    sources = _collect_sources(steps)

    if not steps and not dependency_answers:
        return {"text": "I could not find enough information to answer this question.",
                "charts": [], "sources": [], "error": "no steps were run"}

    prompt = (THINK_CONNECTOR_PROMPT
              .replace("{query}", query)
              .replace("{context}", format_context(context))
              .replace("{dependencies}", format_dependencies(dependency_answers))
              .replace("{steps}", "\n\n".join(format_step(s, CONNECTOR_ROWS_PER_STEP) for s in steps) or "(none)")
              .replace("{charts}", _format_candidates(cands)))

    try:
        raw = call_gemini(prompt, max_tokens=CONNECTOR_MAX_TOKENS, temperature=0.0)
        data = extract_json_object(raw)
        text = str(data.get("answer", "")).strip()
        if not text:
            raise ValueError("empty answer")

        picked = []
        for n in data.get("chart_steps") or []:
            try:
                n = int(n)
            except (TypeError, ValueError):
                continue
            if n in by_n and n not in picked:
                picked.append(n)
        charts = [{"step": n,
                   "visualization": by_n[n]["visualization"],
                   "rows": by_n[n].get("rows") or [],
                   "cols": by_n[n].get("cols") or []}
                  for n in picked[:MAX_CHARTS]]
        error = ""
    except Exception as exc:
        text, charts = _fallback_text(steps), []
        error = f"connector fallback ({type(exc).__name__}: {exc})"

    dbg("THINK_CONNECTOR", f"{text}\ncharts from steps: {[c['step'] for c in charts]}")
    return {"text": text, "charts": charts, "sources": sources, "error": error}
