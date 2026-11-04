# thinking_layer/src/think_tagger.py
"""
think_tagger: decides where ONE sub-question goes.

    sub-question  ──►  LLM (one call)  ──►  "text2sql"  (shipment database, text2sql_thinking)
                                       └─►  "web"       (thinking_webtool)

    from thinking_layer.src.think_tagger import tag
    tag("Current EU import duty on green coffee, HS 0901 11")
    # {"tool": "web", "reason": "...", "fallback": False}

Never raises. If the LLM call fails or returns something unreadable, it falls
back to "text2sql" and sets fallback=True so the trace shows it.
"""

import os
from typing import Any, Dict

from thinking_layer.text2sql_thinking.text2sql_thinking_tool import call_gemini, dbg, extract_json_object
from thinking_layer.text2sql_thinking import PROMPT_A_SKELETON_BIBLE_THINKING

TOOL_TEXT2SQL = "text2sql"
TOOL_WEB = "web"
DEFAULT_TOOL = TOOL_TEXT2SQL

TAGGER_MAX_TOKENS = int(os.getenv("THINK_TAGGER_MAX_TOKENS", "400"))


THINK_TAGGER_PROMPT = """You route one sub-question of a trade analysis to exactly one tool.

TOOLS

text2sql
Answers from Exira's own trade shipment database (import/export shipment records).
Use it for anything that can be counted, summed, ranked or computed from shipment records:
trade value, quantity, shipment count, buyers, sellers/suppliers, ports, countries, cities,
products, HS codes, observed unit prices, growth, new or stopped buyers/suppliers, and
formulas built from these (shares, concentration, HHI, ratios, rates, averages).
Its capability map:
<<<SKELETON
{skeleton}
SKELETON>>>

web
Answers from the internet. Use it for facts that are NOT in shipment records: tariffs, duties,
trade agreements, sanctions, regulations, certifications, market size, production,
consumption, GDP, population, commodity or freight prices, exchange rates, company
background (what a company does, ownership, size), news and events, HS classification help.

RULES
- If the sub-question asks for numbers about shipments, buyers, suppliers, ports or observed
  trade values/prices, choose text2sql, even if it mentions a company or country by name.
- If it needs information that shipment records cannot contain, choose web.
- If it needs both, choose the tool for the main thing being asked.
- Choose exactly one tool.

SUB-QUESTION
{sub_question}

OUTPUT
Return only JSON, no other text:
{"tool": "text2sql" | "web", "reason": "<one short sentence>"}"""


def build_prompt(sub_question: str) -> str:
    return (THINK_TAGGER_PROMPT
            .replace("{skeleton}", PROMPT_A_SKELETON_BIBLE_THINKING)
            .replace("{sub_question}", sub_question))


def tag(sub_question: str) -> Dict[str, Any]:
    """Return {"tool": "text2sql"|"web", "reason": str, "fallback": bool}."""
    sub_question = (sub_question or "").strip()
    if not sub_question:
        return {"tool": DEFAULT_TOOL, "reason": "empty sub-question", "fallback": True}

    try:
        raw = call_gemini(build_prompt(sub_question), max_tokens=TAGGER_MAX_TOKENS, temperature=0.0)
        data = extract_json_object(raw)
        tool = str(data.get("tool", "")).strip().lower()
        if tool in {"sql", "text2sql_thinking", "database", "db"}:
            tool = TOOL_TEXT2SQL
        if tool not in {TOOL_TEXT2SQL, TOOL_WEB}:
            raise ValueError(f"unknown tool {tool!r}")
        out = {"tool": tool, "reason": str(data.get("reason", "")).strip(), "fallback": False}
    except Exception as exc:
        out = {"tool": DEFAULT_TOOL, "reason": f"tagger fallback ({type(exc).__name__}: {exc})",
               "fallback": True}

    dbg("THINK_TAGGER", f"{out['tool']}  ← {sub_question}\n{out['reason']}")
    return out