# backend/main.py
# Agentic Trade Chatbot backend
# Core chat/domain/confirmation flow kept stable.
# Visualization support is added only after successful Prompt A/B/C execution.
# Paste your final Prompt A, Prompt B, Prompt C, Trade Agent, Followup Agent prompts into the placeholders below.

import os
import re
import json
import uuid
import time
import datetime
from pathlib import Path
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple



import requests
import snowflake.connector
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from text2sql.text2sql_prompts.text2sql_prompts_all import PROMPT_A_TEMPLATE, PROMPT_B_TEMPLATE, PROMPT_C_TEMPLATE, TRADE_AGENT_PROMPT_TEMPLATE, FOLLOWUP_AGENT_PROMPT_TEMPLATE, NORMALIZE_PROMPT_TEMPLATE, PROMPT_A_SKELETON_BIBLE
# =========================================================
# PATHS / ENV
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
ENV_PATH = PROJECT_ROOT / ".env"
load_dotenv(ENV_PATH)

# =========================================================
# APP
# =========================================================
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================================================
# CONSTANTS / CONFIG
# =========================================================
HISTORY_TURNS = 10
EXECUTED_TRADE_HISTORY_LIMIT = 5
GENERAL_TRADE_QA_HISTORY_LIMIT = 5
RESULT_ROWS_MEMORY_LIMIT = 10
RESULT_ROWS_TRACE_LIMIT = 100

STATE_SETUP = "SETUP"
STATE_IDLE = "IDLE"
STATE_AWAITING_CLARIFICATION = "AWAITING_CLARIFICATION"
STATE_AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
STATE_READY = "READY_FOR_NEW_QUERY"

OPENROUTER_API_KEY = (os.getenv("OPENROUTER_API_KEY") or "").strip()
GEMINI_MODEL = (os.getenv("GEMINI_MODEL") or "google/gemini-2.5-flash").strip()
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DEBUG = (os.getenv("DEBUG_TRADE", "1") or "").strip() not in {"0", "false", "False", ""}


NORMALIZE_MAX_TOKENS = int(os.getenv("NORMALIZE_MAX_TOKENS", "1000"))
TRADE_AGENT_MAX_TOKENS = int(os.getenv("TRADE_AGENT_MAX_TOKENS", "5000"))
FOLLOWUP_AGENT_MAX_TOKENS = int(os.getenv("FOLLOWUP_AGENT_MAX_TOKENS", "1200"))
PROMPT_A_MAX_TOKENS = int(os.getenv("PROMPT_A_MAX_TOKENS", "22000"))
PROMPT_B_MAX_TOKENS = int(os.getenv("PROMPT_B_MAX_TOKENS", "5000"))
PROMPT_C_MAX_TOKENS = int(os.getenv("PROMPT_C_MAX_TOKENS", "3500"))

MAX_SQL_FIX_ATTEMPTS = int(os.getenv("MAX_SQL_FIX_ATTEMPTS", "3"))
PORT = int(os.getenv("PORT", "8001"))

SNOWFLAKE_ACCOUNT = os.getenv("SNOWFLAKE_ACCOUNT", "")
SNOWFLAKE_USER = os.getenv("SNOWFLAKE_USER", "")
SNOWFLAKE_PASSWORD = os.getenv("SNOWFLAKE_PASSWORD", "")
SNOWFLAKE_WAREHOUSE = os.getenv("SNOWFLAKE_WAREHOUSE", "")
SNOWFLAKE_DATABASE = os.getenv("SNOWFLAKE_DATABASE", "")
SNOWFLAKE_SCHEMA = os.getenv("SNOWFLAKE_SCHEMA", "PUBLIC")
SNOWFLAKE_ROLE = os.getenv("SNOWFLAKE_ROLE", "")

TRADE_TABLE_NAME = os.getenv("TRADE_TABLE_NAME", "imports_exports")
HS_PROBE_MONTHS = int(os.getenv("HS_PROBE_MONTHS", "3"))

HS2_DOMAIN_MAP = {
    "01": "Live Animals; Animal Products", "02": "Meat and Edible Meat Offal", "03": "Live Animals; Animal Products",
    "04": "Live Animals; Animal Products", "05": "Live Animals; Animal Products", "06": "Vegetable Products",
    "07": "Vegetable Products", "08": "Edible Fruits and Nuts", "09": "Coffee, Tea, Mate and Spices",
    "10": "Cereals", "11": "Vegetable Products", "12": "Vegetable Products", "13": "Vegetable Products",
    "14": "Vegetable Products", "15": "Fats & Oils / Waxes", "16": "Preparations of Meat",
    "17": "Sugars / Sugar Confectionery", "18": "Cocoa & Cocoa Preparations", "19": "Prepared Foodstuff",
    "20": "Prepared Foodstuff", "21": "Prepared Foodstuff", "22": "Beverages / Spirits / Vinegar",
    "23": "Residue from food industry / animal fodder", "24": "Tobacco and Tobacco products",
    "25": "Mineral Products", "26": "Mineral Products", "27": "Mineral Products", "28": "Inorganic Chemicals",
    "29": "Organic Chemicals", "30": "Pharmaceutical products", "31": "Fertilisers",
    "32": "Pigments, Dyes and other colouring matter", "33": "Essential Oils", "34": "Soaps / Detergents",
    "35": "Prepared Foodstuff", "37": "Photographic Goods", "38": "Chemicals", "39": "Plastics",
    "40": "Rubber", "41": "Leather", "42": "Leather", "43": "Fur and articles of fur",
    "44": "Wood / Articles of Wood", "45": "Cork / Articles of cork", "46": "Wood / Articles of Wood",
    "47": "Paper / Articles of paper", "48": "Paper / Articles of paper", "49": "Paper / Articles of paper",
    "50": "Textiles / Made ups / Articles of textiles", "51": "Textiles / Made ups / Articles of textiles",
    "52": "Textiles / Made ups / Articles of textiles", "53": "Textiles / Made ups / Articles of textiles",
    "54": "Textiles / Made ups / Articles of textiles", "55": "Textiles / Made ups / Articles of textiles",
    "56": "Textiles / Made ups / Articles of textiles", "57": "Textiles / Made ups / Articles of textiles",
    "58": "Textiles / Made ups / Articles of textiles", "59": "Textiles / Made ups / Articles of textiles",
    "60": "Textiles / Made ups / Articles of textiles", "61": "Textiles / Made ups / Articles of textiles",
    "62": "Textiles / Made ups / Articles of textiles", "63": "Textiles / Made ups / Articles of textiles",
    "64": "Footware / Articles of footware", "65": "Headgare / Articles of Headgare",
    "66": "Textiles / Made ups / Articles of textiles", "67": "Textiles / Made ups / Articles of textiles",
    "68": "Articles of Stone, Plaster, Cement, Asbestos, mica or similar", "69": "Ceramics",
    "70": "Glass and glassware", "71": "Gems & jewellery", "72": "Iron and Steel", "73": "Iron and Steel",
    "74": "Copper / Articles of copper", "75": "Nickel and Articles of nickel", "76": "Aluminium and Articles of aluminium",
    "77": "Metals", "78": "Lead and articles of lead", "79": "Zinc and Articles of zinc", "80": "Tin and articles of tin",
    "81": "Metals", "82": "Metals", "83": "Metals", "84": "Machinery and Mechanical Appliances",
    "85": "Machinery and Mechanical Appliances", "86": "Railway", "87": "Vehicles", "88": "Aircraft / Spacecraft",
    "89": "Ships / Boats", "90": "Engineering", "91": "Clocks, Watches & parts", "92": "Musical instruments",
    "94": "Furniture", "95": "Sports Goods", "96": "Miscellaneous", "97": "Arts and antiques",
    "98": "Miscellaneous", "99": "Miscellaneous",
}

NON_PRODUCT_TERMS = {
    "india", "united states", "usa", "us", "u.s.", "u.s.a.", "united kingdom", "uk", "china", "vietnam",
    "sri lanka", "new zealand", "global", "world", "worldwide", "buyer", "buyers", "seller", "sellers",
    "supplier", "suppliers", "exporter", "exporters", "importer", "importers", "trend", "demand", "competition",
    "price", "seasonality", "market", "markets", "port", "ports", "country", "countries", "state", "city",
    "this product", "product", "hs", "hs code", "last month", "last year",
}

# =========================================================
# PROMPTS / CONTEXT PLACEHOLDERS
# =========================================================
SCHEMA_CONTEXT = f"""SCHEMA_CONTEXT (for grounding only)
Table: {TRADE_TABLE_NAME}
- SHIPPING_DATE (date): used for all time filters, comparisons, rolling windows
- SHIPMENT_VALUE (numeric): monetary metric (default metric)
- QUANTITY (numeric): volume metric (unit-sensitive)
- UNIT (text): required only when aggregating quantity or computing price
- PRODUCT_DESCRIPTION (text): primary product filter when user mentions a product name
- HS_CODE (text): hierarchical product identifier
- BUYER: BUYER_COMPANY, BUYER_COUNTRY, BUYER_STATE, BUYER_CITY, BUYER_ADDRESS
- SELLER: SELLER_COMPANY, SELLER_COUNTRY, SELLER_STATE, SELLER_CITY, SELLER_ADDRESS
- PORTS: PORT_OF_ORIGIN, PORT_OF_DESTINATION
- DECL_NO (text): unique row identifier; never use as grouping/logical dimension
"""

SCHEMA_DESCRIPTION = f"""Table Name: {TRADE_TABLE_NAME}
Columns:
- SHIPPING_DATE (date)
- QUANTITY (numeric)
- SHIPMENT_VALUE (numeric)
- PRODUCT_DESCRIPTION (text)
- BUYER_COUNTRY (text)
- BUYER_STATE (text)
- BUYER_CITY (text)
- BUYER_ADDRESS (text)
- BUYER_COMPANY (text)
- PORT_OF_DESTINATION (text)
- SELLER_COUNTRY (text)
- SELLER_STATE (text)
- SELLER_CITY (text)
- SELLER_ADDRESS (text)
- SELLER_COMPANY (text)
- PORT_OF_ORIGIN (text)
- UNIT (text)
- DECL_NO (text)
- HS_CODE (text)
"""





SESSIONS: Dict[str, Dict[str, Any]] = {}

# =========================================================
# REQUEST MODELS
# =========================================================
class ChatRequest(BaseModel):
    session_id: str
    message: str
    source: Optional[str] = None

class ConfirmRequest(BaseModel):
    session_id: str
    decision: str

# =========================================================
# UTIL
# =========================================================
def now_ms() -> int:
    return int(time.time() * 1000)

def current_date_iso() -> str:
    return datetime.date.today().isoformat()

def current_date_pretty() -> str:
    return datetime.date.today().strftime("%B %d, %Y")

def has_real_prompt(template: str) -> bool:
    return bool(template) and "<PASTE_" not in template

def strip_markdown_fences(text: str) -> str:
    if not text:
        return ""
    return (
        text.replace("```sql", "")
        .replace("```SQL", "")
        .replace("```json", "")
        .replace("```JSON", "")
        .replace("```", "")
        .strip()
    )

def strip_sql_end(text: str) -> str:
    cleaned = strip_markdown_fences(text or "")
    if "SQL_END" in cleaned:
        cleaned = cleaned.split("SQL_END", 1)[0].strip()
    return cleaned

def convert_decimals(obj):
    if isinstance(obj, list):
        return [convert_decimals(x) for x in obj]
    if isinstance(obj, dict):
        return {k: convert_decimals(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    return obj

def extract_json_object(text: str) -> Dict[str, Any]:
    if not text:
        return {}
    cleaned = strip_markdown_fences(text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise

def clean_narrative_answer(text: str) -> str:
    s = (text or "").strip()
    s = re.sub(r"(?is)^\s*Narrative Answer\s*:?\s*", "", s).strip()
    return s
def sanitize_visualization_spec(raw_visualization: Any, safe_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    allowed_types = {"none", "structured_table", "line_chart", "bar_chart", "pie_chart", "scatter_chart"}
    available_columns = list(safe_rows[0].keys()) if safe_rows else []

    if not isinstance(raw_visualization, dict):
        raw_visualization = {}

    visual_type = raw_visualization.get("type", "none")
    if visual_type not in allowed_types:
        visual_type = "none"

    if not safe_rows:
        return {
            "type": "none",
            "title": None,
            "x_column": None,
            "y_column": None,
            "series_column": None,
            "trendline_column": None,
            "columns": [],
            "reason": "No rows returned.",
        }

    def valid_col(value):
        if not value:
            return None
        if value in available_columns:
            return value
        value_upper = str(value).upper()
        for col in available_columns:
            if col.upper() == value_upper:
                return col
        return None

    raw_columns = raw_visualization.get("columns", [])
    if not isinstance(raw_columns, list):
        raw_columns = []

    clean_columns = []
    for col in raw_columns:
        matched_col = valid_col(col)
        if matched_col and matched_col not in clean_columns:
            clean_columns.append(matched_col)

    if visual_type == "structured_table" and not clean_columns:
        clean_columns = available_columns

    x_column = valid_col(raw_visualization.get("x_column"))
    y_column = valid_col(raw_visualization.get("y_column"))
    series_column = valid_col(raw_visualization.get("series_column"))
    trendline_column = valid_col(raw_visualization.get("trendline_column"))

    if visual_type in {"line_chart", "bar_chart", "pie_chart", "scatter_chart"}:
        if not x_column or not y_column:
            visual_type = "structured_table"
            clean_columns = clean_columns or available_columns
            x_column = None
            y_column = None
            series_column = None
            trendline_column = None

    if visual_type != "line_chart":
        trendline_column = None

    return {
        "type": visual_type,
        "title": raw_visualization.get("title"),
        "x_column": x_column,
        "y_column": y_column,
        "series_column": series_column,
        "trendline_column": trendline_column,
        "columns": clean_columns,
        "reason": raw_visualization.get("reason"),
    }

def dedupe_preserve(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for q in items or []:
        if not isinstance(q, str):
            continue
        s = re.sub(r"\s+", " ", q.strip())
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out

def normalize_product_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().upper())

def is_non_product_term(term: str) -> bool:
    clean = re.sub(r"\s+", " ", (term or "").strip().lower())
    return not clean or clean in NON_PRODUCT_TERMS

def sanitize_sql_literal(value: str) -> str:
    return (value or "").replace("'", "''").strip()

def trim_json_for_memory(value: Any, max_list_items: int = RESULT_ROWS_MEMORY_LIMIT, max_str_len: int = 5000) -> Any:
    if isinstance(value, str):
        return value[:max_str_len]
    if isinstance(value, list):
        return [trim_json_for_memory(x, max_list_items, max_str_len) for x in value[:max_list_items]]
    if isinstance(value, dict):
        return {k: trim_json_for_memory(v, max_list_items, max_str_len) for k, v in value.items()}
    return value

# =========================================================
# UI MESSAGE HELPERS
# =========================================================
def msg_answer(content: str, data: Optional[dict] = None) -> Dict[str, Any]:
    return {"type": "answer", "content": content, "data": data or {}}

def msg_clarification(content: str, data: Optional[dict] = None) -> Dict[str, Any]:
    return {"type": "clarification", "content": content, "data": data or {}}

def msg_confirm(resolved_query: str) -> Dict[str, Any]:
    return {"type": "confirm", "content": "Confirm:", "data": {"resolved_query": resolved_query}}

def msg_followup(items: List[str], title: str = "Suggested questions:", limit: Optional[int] = 3) -> Dict[str, Any]:
    safe_items = items or []
    if limit is not None:
        safe_items = safe_items[:limit]
    return {"type": "followup", "content": title, "data": {"items": safe_items}}

def msg_domain_selector(product_term: str, options: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "type": "domain_selector",
        "content": f"Select one or more domains / HS codes for {product_term}.",
        "data": {"product_term": product_term, "options": options, "multi_select": True},
    }

# =========================================================
# SESSION MEMORY
# =========================================================
def new_session_state() -> Dict[str, Any]:
    return {
        "state": STATE_SETUP,
        "conversation_last_10": [],
        "active_focus": {
            "product_name": None,
            "hs_code": None,
            "pending_query_text": None,
            "pending_clarification": None,
            "pending_product_term": None,
            "pending_product_terms": [],
            "pending_hs_code": None,
            "pending_domain_disambiguation": None,
            "product_hs_memory": {},
            "selected_hs_codes": [],
            "executed_trade_history": [],
            "general_trade_qa_history": [],
            "last_executed_query_text": None,
            "last_executed_answer": None,
            "user_memory": {},
        },
        "last_trace": None,
        "last_followups_sets": [],
    }

def append_turn(session: Dict[str, Any], role: str, content: str):
    session["conversation_last_10"].append({"role": role, "content": content, "ts": now_ms()})
    if len(session["conversation_last_10"]) > HISTORY_TURNS:
        session["conversation_last_10"] = session["conversation_last_10"][-HISTORY_TURNS:]

def recent_turns(session: Dict[str, Any]) -> List[Dict[str, str]]:
    return [{"role": t["role"], "content": t["content"]} for t in session["conversation_last_10"][-HISTORY_TURNS:]]

def get_last_6_followups_shown(session: Dict[str, Any]) -> List[str]:
    flat = []
    for group in session.get("last_followups_sets", [])[-2:]:
        for q in group or []:
            if isinstance(q, str) and q.strip():
                flat.append(re.sub(r"\s+", " ", q.strip()))
    return flat[-6:]

def normalize_question_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower().rstrip("?"))

def safe_fallback_followups(session: Dict[str, Any], exclude_questions: Optional[List[str]] = None) -> List[str]:
    p = session["active_focus"].get("product_name") or session["active_focus"].get("hs_code") or "this product"
    if session["active_focus"].get("hs_code") and not str(p).lower().startswith("hs"):
        p = f"HS {p}"
    candidates = [
        f"Who are the top buyers of {p} in the last 24 months?",
        f"Which countries are showing increasing demand for {p} in the last 24 months?",
        f"Which markets have low competition for {p} in the last 12 months?",
        f"Which buyers pay premium prices for {p} in the last 24 months?",
        f"Which month does demand for {p} usually peak over the last 36 months?",
        f"What is the average price of {p} in the last 24 months?",
        f"Which ports are most active for {p} in the last 24 months?",
        f"Who are the new buyers of {p} in the last 12 months?",
    ]
    banned = {normalize_question_key(q) for q in get_last_6_followups_shown(session)}
    banned |= {normalize_question_key(q) for q in (exclude_questions or [])}
    out = []
    out_keys = set()
    for q in candidates:
        key = normalize_question_key(q)
        if key in banned or key in out_keys:
            continue
        out.append(q)
        out_keys.add(key)
        if len(out) == 3:
            return out
    for q in candidates:
        key = normalize_question_key(q)
        if key in out_keys:
            continue
        out.append(q)
        out_keys.add(key)
        if len(out) == 3:
            return out
    return out[:3]

def finalize_followups(session: Dict[str, Any], followups: List[str]) -> List[str]:
    recent_keys = {normalize_question_key(q) for q in get_last_6_followups_shown(session)}
    out = []
    out_keys = set()
    for q in dedupe_preserve(followups or []):
        key = normalize_question_key(q)
        if not key or key in out_keys or key in recent_keys:
            continue
        out.append(q.strip())
        out_keys.add(key)
        if len(out) == 3:
            return out
    for q in safe_fallback_followups(session, exclude_questions=out):
        key = normalize_question_key(q)
        if not key or key in out_keys:
            continue
        out.append(q.strip())
        out_keys.add(key)
        if len(out) == 3:
            return out
    return out[:3]

def store_followups(session: Dict[str, Any], followups: List[str]):
    final = finalize_followups(session, followups)
    if not final:
        return
    session["last_followups_sets"].append(final[:3])
    if len(session["last_followups_sets"]) > 2:
        session["last_followups_sets"] = session["last_followups_sets"][-2:]

def store_executed_trade_history(session: Dict[str, Any], user_query: str, answer: str, result_json: Dict[str, Any]):
    record = {"user_query": user_query, "answer": answer, "result_json": trim_json_for_memory(result_json), "ts": now_ms()}
    history = session["active_focus"].setdefault("executed_trade_history", [])
    history.append(record)
    session["active_focus"]["executed_trade_history"] = history[-EXECUTED_TRADE_HISTORY_LIMIT:]

def store_general_trade_qa_history(session: Dict[str, Any], question: str, answer: str):
    record = {"question": question, "answer": answer, "ts": now_ms()}
    history = session["active_focus"].setdefault("general_trade_qa_history", [])
    history.append(record)
    session["active_focus"]["general_trade_qa_history"] = history[-GENERAL_TRADE_QA_HISTORY_LIMIT:]

# =========================================================
# GEMINI CALLS
# =========================================================
def call_gemini(prompt: str, max_tokens: int, temperature: float = 0.0) -> str:
    """Name kept for compatibility. Now routed through OpenRouter."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    resp = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": GEMINI_MODEL,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": 0.95,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )

    try:
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"OpenRouter returned non-JSON ({resp.status_code}): {resp.text[:500]}") from exc

    if "error" in data:
        raise RuntimeError(data["error"].get("message", "OpenRouter API error"))

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected OpenRouter response: {data}") from exc
def fallback_normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())

def normalize_user_message(raw_message: str) -> str:
    raw_message = (raw_message or "").strip()
    if not raw_message:
        return raw_message
    if not has_real_prompt(NORMALIZE_PROMPT_TEMPLATE):
        return fallback_normalize(raw_message)
    prompt = NORMALIZE_PROMPT_TEMPLATE.replace("{current_date}", current_date_pretty()).replace("{raw_message}", raw_message)
    try:
        out = call_gemini(prompt, max_tokens=NORMALIZE_MAX_TOKENS, temperature=0.0).strip()
        out = strip_markdown_fences(out).strip().strip('"').strip("'")
        return fallback_normalize(out or raw_message)
    except Exception:
        return fallback_normalize(raw_message)

def parse_json_strict(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"(?s)\{.*\}", raw)
        if m:
            return json.loads(m.group(0))
    raise RuntimeError("Model did not return valid JSON")

# =========================================================
# PRODUCT / HS SETUP
# =========================================================
def extract_product_or_hs(text: str) -> Dict[str, Optional[str]]:
    hs_match = re.search(r"(?i)\b(?:hs|hs code)\b\D*(\d{2,10})\b", text or "")
    if hs_match:
        return {"hs_code": hs_match.group(1), "product": None}
    only_digits = (text or "").strip()
    if re.fullmatch(r"\d{2,10}", only_digits):
        return {"hs_code": only_digits, "product": None}
    return {"hs_code": None, "product": (text or "").strip()}

def normalize_hs_token(code: str) -> Optional[str]:
    raw = re.sub(r"[.\-\s]", "", (code or "").strip())
    if not re.fullmatch(r"\d{1,10}", raw):
        return None
    if len(raw) <= 2:
        return raw.zfill(2)
    return raw

def extract_explicit_hs_code(text: str) -> Optional[str]:
    return extract_product_or_hs(text).get("hs_code")

def run_hs2_probe_sql(product_term: str) -> List[Dict[str, Any]]:
    safe_term = sanitize_sql_literal(product_term)
    if not safe_term:
        return []
    sql = f"""
SELECT
    LEFT(TRIM(td.HS_CODE), 2) AS HS2,
    COUNT(*) AS RECORD_COUNT
FROM {TRADE_TABLE_NAME} AS td
WHERE td.HS_CODE IS NOT NULL
  AND TRIM(td.HS_CODE) <> ''
  AND td.PRODUCT_DESCRIPTION IS NOT NULL
  AND td.SHIPPING_DATE IS NOT NULL
  AND td.SHIPPING_DATE >= DATEADD(MONTH, -{HS_PROBE_MONTHS}, DATE_TRUNC('MONTH', CURRENT_DATE()))
  AND td.SHIPPING_DATE < DATE_TRUNC('MONTH', CURRENT_DATE())
  AND (
      UPPER(td.PRODUCT_DESCRIPTION) LIKE '%' || UPPER('{safe_term}') || '%'
      OR JAROWINKLER_SIMILARITY(UPPER(td.PRODUCT_DESCRIPTION), UPPER('{safe_term}')) > 80
  )
GROUP BY LEFT(TRIM(td.HS_CODE), 2)
ORDER BY RECORD_COUNT DESC, HS2
""".strip()
    ok, rows, cols, err = run_snowflake(sql)
    print("HS PROBE PRODUCT:", product_term)
    print("HS PROBE SQL:", sql)
    print("HS PROBE OK:", ok)
    print("HS PROBE ROWS:", rows)
    print("HS PROBE ERROR:", err)
    if not ok:
        return []
    rows = convert_decimals(rows)
    out = []
    for row in rows:
        hs2 = str(row.get("HS2") or "").strip()
        if not re.fullmatch(r"\d{1,2}", hs2):
            continue
        hs2 = hs2.zfill(2)
        out.append({"hs2": hs2, "record_count": int(row.get("RECORD_COUNT") or 0), "domain": HS2_DOMAIN_MAP.get(hs2, "Unknown / Other")})
    return out

def build_domain_options(hs2_rows: List[Dict[str, Any]], max_options: int = 8) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in hs2_rows:
        domain = row["domain"]
        if domain not in grouped:
            grouped[domain] = {"domain": domain, "hs_codes": [], "record_count": 0}
        grouped[domain]["hs_codes"].append(row["hs2"])
        grouped[domain]["record_count"] += int(row.get("record_count") or 0)
    options = list(grouped.values())
    for opt in options:
        opt["hs_codes"] = sorted(set(opt["hs_codes"]))
        opt["label"] = f"Use {opt['domain']} (HS {', '.join(opt['hs_codes'])})"
    options.sort(key=lambda x: (-x["record_count"], x["domain"]))
    for idx, opt in enumerate(options, start=1):
        opt["option_index"] = idx
    return options[:max_options]

def scopes_from_hs_codes(options: List[Dict[str, Any]], hs_codes: List[str]) -> List[Dict[str, Any]]:
    selected = set([c for c in [normalize_hs_token(x) for x in hs_codes] if c])
    scopes = []
    for opt in options or []:
        matched = [c for c in opt.get("hs_codes", []) if c in selected]
        if matched:
            scopes.append({"domain": opt.get("domain"), "selected_hs_codes": sorted(set(matched))})
    existing = {c for s in scopes for c in s.get("selected_hs_codes", [])}
    extra = sorted(selected - existing)
    if extra:
        scopes.append({"domain": "User-selected HS", "selected_hs_codes": extra})
    return scopes

def flatten_scope_hs_codes(scopes: List[Dict[str, Any]]) -> List[str]:
    codes = []
    for scope in scopes or []:
        for c in scope.get("selected_hs_codes", []) or scope.get("hs_codes", []):
            norm = normalize_hs_token(c)
            if norm:
                codes.append(norm)
    return sorted(set(codes))

def parse_domain_selection_from_text(text: str, pending: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    raw = (text or "").strip()
    if not raw:
        return None
    options = pending.get("options", []) or []

    for opt in options:
        if raw.lower() == str(opt.get("label", "")).lower():
            return [{"domain": opt.get("domain"), "selected_hs_codes": opt.get("hs_codes", [])}]

    if re.fullmatch(r"\s*\d+(\s*,\s*\d+)*\s*", raw):
        scopes = []
        for n in [int(x) for x in re.findall(r"\d+", raw)]:
            if 1 <= n <= len(options):
                opt = options[n - 1]
                scopes.append({"domain": opt.get("domain"), "selected_hs_codes": opt.get("hs_codes", [])})
        if scopes:
            return scopes

    hs_text = None
    m = re.search(r"\(HS\s+([0-9,\s]+)\)", raw, flags=re.I)
    if m:
        hs_text = m.group(1)
    else:
        m = re.fullmatch(r"(?i)\s*HS\s+([0-9,\s]+)\s*", raw)
        if m:
            hs_text = m.group(1)
        elif re.fullmatch(r"\s*\d{1,10}(\s*,\s*\d{1,10})+\s*", raw):
            hs_text = raw
    if hs_text:
        codes = [normalize_hs_token(x) for x in re.findall(r"\d{1,10}", hs_text)]
        codes = [c for c in codes if c]
        if codes:
            return scopes_from_hs_codes(options, codes)
    return None

def get_product_memory(session: Dict[str, Any], product_term: str) -> Optional[Dict[str, Any]]:
    return session["active_focus"].get("product_hs_memory", {}).get(normalize_product_key(product_term))

def save_product_scope(session: Dict[str, Any], product_term: str, scopes: List[Dict[str, Any]]):
    selected_hs = flatten_scope_hs_codes(scopes)
    key = normalize_product_key(product_term)
    session["active_focus"]["product_hs_memory"][key] = {
        "product_term": product_term,
        "selected_scopes": scopes,
        "selected_hs_codes": selected_hs,
    }
    session["active_focus"]["product_name"] = product_term
    session["active_focus"]["hs_code"] = None
    session["active_focus"]["selected_hs_codes"] = selected_hs

def get_unknown_product_terms(session: Dict[str, Any], product_terms: List[str]) -> List[str]:
    memory = session["active_focus"].get("product_hs_memory", {})
    unknown_terms = []
    for term in product_terms or []:
        if not isinstance(term, str):
            continue
        clean = re.sub(r"\s+", " ", term.strip())
        if not clean or is_non_product_term(clean):
            continue
        if normalize_product_key(clean) not in memory:
            unknown_terms.append(clean)
    return dedupe_preserve(unknown_terms)

def build_product_scopes_context(original_query: str, session: Dict[str, Any], product_terms: Optional[List[str]] = None) -> str:
    memory = session["active_focus"].get("product_hs_memory", {}) or {}
    cleaned_terms = []
    for term in product_terms or []:
        if isinstance(term, str) and term.strip() and not is_non_product_term(term):
            cleaned_terms.append(re.sub(r"\s+", " ", term.strip()))
    if not cleaned_terms:
        q_norm = normalize_product_key(original_query)
        for item in memory.values():
            term = item.get("product_term")
            if term and normalize_product_key(term) in q_norm:
                cleaned_terms.append(term)
    cleaned_terms = dedupe_preserve(cleaned_terms)

    lines = []
    for term in cleaned_terms:
        item = memory.get(normalize_product_key(term))
        if not item:
            continue
        scopes = item.get("selected_scopes") or []
        scope_texts = []
        for scope in scopes:
            domain = scope.get("domain") or "Selected domain"
            codes = [c for c in [normalize_hs_token(x) for x in scope.get("selected_hs_codes", [])] if c]
            if codes:
                scope_texts.append(f"{domain}: HS {', '.join(sorted(set(codes)))}")
        if scope_texts:
            lines.append(f"- {term}: " + "; ".join(scope_texts) + ".")
        else:
            hs_codes = [c for c in [normalize_hs_token(x) for x in item.get("selected_hs_codes", [])] if c]
            if hs_codes:
                lines.append(f"- {term}: restrict PRODUCT_DESCRIPTION matching for this product to HS_CODE prefixes/codes {', '.join(sorted(set(hs_codes)))}.")
    if not lines:
        return original_query
    return f"{original_query}\n\nSystem user-confirmed product HS scopes:\n" + "\n".join(lines)

def is_common_question_while_pending(text: str) -> bool:
    q = (text or "").strip().lower()
    if not q:
        return False
    common_starts = ("what is", "what are", "what does", "what do", "why", "how", "explain", "meaning of", "define")
    return q.startswith(common_starts) or "meant by" in q or "meaning" in q or "definition" in q

def is_stale_domain_button(text: str) -> bool:
    return bool(re.match(r"(?i)^use .+\(HS [0-9,\s]+\)$", (text or "").strip()))

def build_domain_clarification_text(product_term: str, options: List[Dict[str, Any]]) -> str:
    lines = [
        f"“{product_term}” appears under multiple HS industries in the last {HS_PROBE_MONTHS} completed months.",
        "Please select one or more domains, or type specific HS prefixes like `HS 17, 18`.",
        "If you select only a domain, all HS prefixes under that domain will be used.",
        "",
    ]
    for idx, opt in enumerate(options, start=1):
        lines.append(f"{idx}. {opt['domain']} — HS {', '.join(opt['hs_codes'])} ({opt['record_count']} matching rows)")
    return "\n".join(lines)

def build_domain_clarification_response(session: Dict[str, Any], session_id: str, product_term: str, original_query: str, options: List[Dict[str, Any]], setup_only: bool = False, product_terms: Optional[List[str]] = None):
    session["active_focus"]["pending_domain_disambiguation"] = {
        "original_query": original_query,
        "resume_query": original_query,
        "product_term": product_term,
        "options": options,
        "setup_only": setup_only,
        "product_terms": product_terms or [product_term],
    }
    session["state"] = STATE_AWAITING_CLARIFICATION
    clarification = build_domain_clarification_text(product_term, options)
    option_labels = [o["label"] for o in options]
    append_turn(session, "assistant", clarification)
    return {
        "session_id": session_id,
        "state": session["state"],
        "messages": [
            msg_clarification(clarification, data={"product_term": product_term, "options": options}),
            msg_domain_selector(product_term, options),
            msg_followup(option_labels, title="Quick select one domain, or type multiple HS codes like `HS 17, 18`:", limit=None),
        ],
    }

def start_next_unknown_product_scope(session: Dict[str, Any], session_id: str, original_query: str, product_terms: List[str], setup_only: bool = False):
    unknown_terms = get_unknown_product_terms(session, product_terms)
    if not unknown_terms:
        return None
    term_to_probe = unknown_terms[0]
    hs2_rows = run_hs2_probe_sql(term_to_probe)
    options = build_domain_options(hs2_rows)
    if len(options) > 1:
        return build_domain_clarification_response(session, session_id, term_to_probe, original_query, options, setup_only, product_terms)
    if len(options) == 1:
        save_product_scope(session, term_to_probe, [{"domain": options[0]["domain"], "selected_hs_codes": options[0]["hs_codes"]}])
        return start_next_unknown_product_scope(session, session_id, original_query, product_terms, setup_only)
    return None

# =========================================================
# TRADE AGENT
# =========================================================
def build_trade_agent_prompt(session: Dict[str, Any], normalized_message: str) -> str:
    payload = {
        "current_date": current_date_iso(),
        "state": session["state"],
        "normalized_message": normalized_message,
        "conversation_last_10": recent_turns(session),
        "product_hs_memory": session["active_focus"].get("product_hs_memory", {}),
        "executed_trade_history_last_5": session["active_focus"].get("executed_trade_history", [])[-EXECUTED_TRADE_HISTORY_LIMIT:],
        "general_trade_qa_history_last_5": session["active_focus"].get("general_trade_qa_history", [])[-GENERAL_TRADE_QA_HISTORY_LIMIT:],
        "pending_state": {
            "pending_domain_disambiguation": session["active_focus"].get("pending_domain_disambiguation"),
            "pending_query_text": session["active_focus"].get("pending_query_text"),
            "pending_clarification": session["active_focus"].get("pending_clarification"),
            "pending_product_term": session["active_focus"].get("pending_product_term"),
            "pending_product_terms": session["active_focus"].get("pending_product_terms", []),
        },
        "last_execution": {
            "last_executed_query_text": session["active_focus"].get("last_executed_query_text"),
            "last_executed_answer": session["active_focus"].get("last_executed_answer"),
            "last_trace": session.get("last_trace"),
        },
        "active_focus": session["active_focus"],
        "last_followups_sets": session.get("last_followups_sets", []),
        "prompt_a_skeleton": PROMPT_A_SKELETON_BIBLE,
        "schema": SCHEMA_CONTEXT,
        "output_actions": ["ANSWER_DIRECT", "ASK_CLARIFICATION", "CONFIRM_QUERY"],
    }
    return TRADE_AGENT_PROMPT_TEMPLATE.replace("{TRADE_AGENT_INPUT_JSON}", json.dumps(payload, ensure_ascii=False, default=str))

def fallback_trade_agent(session: Dict[str, Any], normalized_message: str) -> Dict[str, Any]:
    parsed = extract_product_or_hs(normalized_message)
    product_terms = [] if parsed.get("hs_code") else ([parsed.get("product")] if parsed.get("product") and not is_non_product_term(parsed.get("product")) else [])
    return {
        "action": "ANSWER_DIRECT",
        "assistant_text": "I can help with trade questions such as buyers, sellers, demand, trend, competition, price, seasonality, ports, and new entrants.",
        "clarification_question": None,
        "final_query_text": None,
        "product_term": product_terms[0] if product_terms else None,
        "product_terms": product_terms,
        "hs_code": parsed.get("hs_code"),
        "followups": safe_fallback_followups(session),
    }

def repair_trade_agent_json(raw_text: str, session: Dict[str, Any], normalized_message: str) -> Dict[str, Any]:
    repair_prompt = f"""
Repair the previous Trade Agent response into valid JSON only.

User message:
{normalized_message}

Invalid response:
{raw_text}

Return ONLY valid JSON matching exactly:
{{
  "action": "ANSWER_DIRECT|ASK_CLARIFICATION|CONFIRM_QUERY",
  "assistant_text": "string",
  "clarification_question": "string or null",
  "final_query_text": "string or null",
  "product_terms": ["string"],
  "product_term": "string or null",
  "hs_code": "string or null",
  "followups": ["Q1", "Q2", "Q3"]
}}
""".strip()
    fixed = call_gemini(repair_prompt, max_tokens=1200, temperature=0.0)
    return parse_json_strict(fixed)

def call_trade_agent(session: Dict[str, Any], normalized_message: str) -> Dict[str, Any]:
    if not has_real_prompt(TRADE_AGENT_PROMPT_TEMPLATE):
        return fallback_trade_agent(session, normalized_message)
    raw = call_gemini(build_trade_agent_prompt(session, normalized_message), max_tokens=TRADE_AGENT_MAX_TOKENS, temperature=0.0)
    try:
        return parse_json_strict(raw)
    except Exception:
        return repair_trade_agent_json(raw, session, normalized_message)

def safe_call_trade_agent(session: Dict[str, Any], normalized_message: str) -> Dict[str, Any]:
    try:
        return call_trade_agent(session, normalized_message)
    except Exception:
        return fallback_trade_agent(session, normalized_message)

# =========================================================
# FOLLOWUP AGENT
# =========================================================
def build_followup_agent_prompt(session: Dict[str, Any]) -> str:
    executed_history = []
    for item in session["active_focus"].get("executed_trade_history", [])[-EXECUTED_TRADE_HISTORY_LIMIT:]:
        executed_history.append({"user_query": item.get("user_query"), "answer": item.get("answer")})
    payload = {
        "executed_trade_history_last_5": executed_history,
        "last_6_followups_shown": get_last_6_followups_shown(session),
        "product_hs_memory": session["active_focus"].get("product_hs_memory", {}),
        "prompt_a_skeleton": PROMPT_A_SKELETON_BIBLE,
        "user_memory": session["active_focus"].get("user_memory", {}),
    }
    return FOLLOWUP_AGENT_PROMPT_TEMPLATE.replace("{FOLLOWUP_AGENT_INPUT_JSON}", json.dumps(payload, ensure_ascii=False, default=str))

def call_followup_agent(session: Dict[str, Any]) -> List[str]:
    if not has_real_prompt(FOLLOWUP_AGENT_PROMPT_TEMPLATE):
        return []
    out = call_gemini(build_followup_agent_prompt(session), max_tokens=FOLLOWUP_AGENT_MAX_TOKENS, temperature=0.0)
    data = parse_json_strict(out)
    followups = data.get("followups")
    if not isinstance(followups, list):
        return []
    return [q.strip() for q in followups if isinstance(q, str) and q.strip()]

def generate_post_execution_followups(session: Dict[str, Any]) -> List[str]:
    try:
        agent_followups = call_followup_agent(session)
    except Exception:
        agent_followups = []
    followups = finalize_followups(session, agent_followups)
    if followups:
        store_followups(session, followups)
    return followups

# =========================================================
# PROMPT A/B/C PIPELINE
# =========================================================
def build_prompt_a(user_query: str) -> str:
    if not has_real_prompt(PROMPT_A_TEMPLATE):
        raise RuntimeError("PROMPT_A_TEMPLATE not configured")
    return PROMPT_A_TEMPLATE.replace("{current_date}", current_date_pretty()).replace("{user_query}", user_query)

def build_prompt_b(user_query: str, interpretation: str, plan: str, attempt_history: str, latest_sql: str, latest_error: str, attempt_index: int) -> str:
    if not has_real_prompt(PROMPT_B_TEMPLATE):
        raise RuntimeError("PROMPT_B_TEMPLATE not configured")
    return (
        PROMPT_B_TEMPLATE
        .replace("{current_date}", current_date_pretty())
        .replace("{user_query}", user_query)
        .replace("{interpretation_summary}", interpretation)
        .replace("{sql_implementation_plan}", plan)
        .replace("{schema_description}", SCHEMA_DESCRIPTION)
        .replace("{attempt_history}", attempt_history)
        .replace("{latest_sql}", latest_sql)
        .replace("{latest_error}", latest_error)
        .replace("{latest_attempt_index}", str(attempt_index))
    )

def build_prompt_c(user_query: str, interpretation: str, plan: str, executed_sql: str, rows: List[Dict[str, Any]], cols: List[str]) -> str:
    if not has_real_prompt(PROMPT_C_TEMPLATE):
        raise RuntimeError("PROMPT_C_TEMPLATE not configured")
    return (
        PROMPT_C_TEMPLATE
        .replace("{current_date}", current_date_pretty())
        .replace("{user_query}", user_query)
        .replace("{interpretation_summary}", interpretation)
        .replace("{sql_implementation_plan}", plan)
        .replace("{executed_sql}", executed_sql)
        .replace("{row_count}", str(len(rows)))
        .replace("{column_names}", ", ".join(cols))
        .replace("{result_rows}", json.dumps(rows[:RESULT_ROWS_TRACE_LIMIT], ensure_ascii=False, default=str))
    )

def parse_prompt_a_output(text: str) -> Dict[str, str]:
    raw = text or ""
    interp = ""
    plan = ""
    sql = ""

    m = re.search(r"(?is)Interpretation Summary\s*(.*?)(?:SQL Implementation Plan)", raw)
    if m:
        interp = m.group(1).strip()
    m = re.search(r"(?is)SQL Implementation Plan\s*(.*?)(?:Generated SQL|Generated\s+SQL|SQL\s*:)", raw)
    if m:
        plan = m.group(1).strip()

    m = re.search(r"(?is)Generated SQL\s*:?\s*([\s\S]*?)\n\s*SQL_END\s*$", raw)
    if m:
        sql = strip_markdown_fences(m.group(1).strip())
    else:
        m = re.search(r"(?is)Generated SQL\s*:?\s*([\s\S]*)$", raw)
        if m:
            sql = strip_markdown_fences(m.group(1).strip())

    sql = strip_sql_end(sql)
    if not sql:
        m = re.search(r"(?is)\b(WITH\b[\s\S]*?;|\bSELECT\b[\s\S]*?;)", raw)
        sql = m.group(1).strip() if m else ""
    sql = strip_sql_end(sql)
    return {"interpretation_summary": interp, "sql_plan": plan, "sql": sql, "raw": raw}

def parse_prompt_b_output(text: str) -> str:
    raw = strip_sql_end(text or "")
    m = re.search(r"(?is)\b(WITH\b[\s\S]*?;|\bSELECT\b[\s\S]*?;)", raw)
    return m.group(1).strip() if m else raw.strip()

def get_snowflake_conn():
    return snowflake.connector.connect(
        user=SNOWFLAKE_USER,
        password=SNOWFLAKE_PASSWORD,
        account=SNOWFLAKE_ACCOUNT,
        warehouse=SNOWFLAKE_WAREHOUSE,
        database=SNOWFLAKE_DATABASE,
        schema=SNOWFLAKE_SCHEMA,
        role=SNOWFLAKE_ROLE if SNOWFLAKE_ROLE else None,
    )


def run_snowflake(sql: str) -> Tuple[bool, List[Dict[str, Any]], List[str], str]:
    conn = None
    cur = None
    try:
        conn = get_snowflake_conn()
        cur = conn.cursor()
        cur.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall() if cur.description else []
        dict_rows = [dict(zip(cols, r)) for r in rows]
        return True, dict_rows, cols, ""
    except Exception as e:
        return False, [], [], str(e)
    finally:
        try:
            if cur:
                cur.close()
        except Exception:
            pass
        try:
            if conn:
                conn.close()
        except Exception:
            pass

def format_attempt_history(attempts: List[Dict[str, str]]) -> str:
    lines = []
    for i, a in enumerate(attempts, start=1):
        lines.append(f"Attempt {i}:\nSQL:\n{a['sql']}\nERROR:\n{a['error']}\n")
    return "\n".join(lines).strip()

def default_visualization(rows: List[Dict[str, Any]], cols: List[str], reason: str = "Fallback visualization.") -> Dict[str, Any]:
    return {
        "type": "structured_table" if rows else "none",
        "title": "Result Table" if rows else None,
        "x_column": None,
        "y_column": None,
        "series_column": None,
        "trendline_column": None,
        "columns": cols if rows else [],
        "reason": reason,
    }

def dbg(label: str, value: Any):
    if not DEBUG:
        return
    print(f"\n{'='*20} {label} {'='*20}")
    if isinstance(value, (dict, list)):
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str)[:4000])
    else:
        print(str(value)[:4000])

def execute_prompt_pipeline(final_query_text: str) -> Dict[str, Any]:
    a_raw = ""
    a_parts = {"interpretation_summary": "", "sql_plan": "", "sql": "", "raw": ""}
    try:
        dbg("QUERY", final_query_text)
        a_prompt = build_prompt_a(final_query_text)
        a_raw = call_gemini(a_prompt, max_tokens=PROMPT_A_MAX_TOKENS, temperature=0.0)
        a_parts = parse_prompt_a_output(a_raw)
        sql_to_run = (a_parts.get("sql") or "").strip()
        dbg("SQL FROM PROMPT A", sql_to_run)
        if not sql_to_run:
            return {
                "stage": "prompt_a_no_sql",
                "narrative": "No Prompt A interpretation / plan available.",
                "visualization": default_visualization([], []),
                "sql": "",
                "rows": [],
                "cols": [],
                "prompt_a_raw": a_raw,
                "prompt_a_parts": a_parts,
                "prompt_c_raw": "",
                "error": "Prompt A did not produce SQL",
            }

        attempts = []
        for attempt_idx in range(1, MAX_SQL_FIX_ATTEMPTS + 1):
            ok, rows, cols, err = run_snowflake(sql_to_run)
            if ok:
                rows = convert_decimals(rows)
                c_prompt = build_prompt_c(
                    user_query=final_query_text,
                    interpretation=a_parts.get("interpretation_summary", ""),
                    plan=a_parts.get("sql_plan", ""),
                    executed_sql=sql_to_run,
                    rows=rows,
                    cols=cols,
                )
                prompt_c_raw = call_gemini(c_prompt, max_tokens=PROMPT_C_MAX_TOKENS, temperature=0.0).strip()
                dbg("ROW COUNT", f"{len(rows)} rows, cols = {cols}")
                dbg("PROMPT C RAW", prompt_c_raw)

                fallback_visualization = default_visualization(rows, cols, "Fallback visualization because Prompt C did not return valid visualization JSON.")
                try:
                    prompt_c_json = extract_json_object(prompt_c_raw)
                    narrative = clean_narrative_answer(str(prompt_c_json.get("narrative_answer", "")).strip())
                    visualization = sanitize_visualization_spec(prompt_c_json.get("visualization"), rows)
                except Exception:
                    narrative = clean_narrative_answer(prompt_c_raw)
                    visualization = fallback_visualization

                if not narrative:
                    if not rows:
                        narrative = "There are no recorded shipments matching those criteria in the selected period, so the requested metric cannot be computed."
                    else:
                        narrative = f"The query returned {len(rows)} row{'s' if len(rows) != 1 else ''}."
                dbg("FINAL JSON", {"narrative": narrative, "visualization": visualization})
                dbg("ANSWER", narrative)
                return {
                    "stage": "success",
                    "narrative": narrative,
                    "visualization": visualization,
                    "sql": sql_to_run,
                    "rows": rows,
                    "cols": cols,
                    "prompt_a_raw": a_raw,
                    "prompt_a_parts": a_parts,
                    "prompt_c_raw": prompt_c_raw,
                    "error": None,
                }

            attempts.append({"sql": sql_to_run, "error": err})
            dbg(f"SQL FAILED (attempt {attempt_idx})", err)
            if attempt_idx == MAX_SQL_FIX_ATTEMPTS:
                return {
                    "stage": "sql_execution_failure",
                    "narrative": "SQL execution failed.",
                    "visualization": default_visualization([], []),
                    "sql": sql_to_run,
                    "rows": [],
                    "cols": [],
                    "prompt_a_raw": a_raw,
                    "prompt_a_parts": a_parts,
                    "prompt_c_raw": "",
                    "error": err,
                }

            b_prompt = build_prompt_b(
                user_query=final_query_text,
                interpretation=a_parts.get("interpretation_summary", ""),
                plan=a_parts.get("sql_plan", ""),
                attempt_history=format_attempt_history(attempts[-3:]),
                latest_sql=sql_to_run,
                latest_error=err,
                attempt_index=attempt_idx,
            )
            b_raw = call_gemini(b_prompt, max_tokens=PROMPT_B_MAX_TOKENS, temperature=0.0)
            sql_to_run = parse_prompt_b_output(b_raw)

        return {
            "stage": "unknown_failure",
            "narrative": "Execution failed.",
            "visualization": default_visualization([], []),
            "sql": sql_to_run,
            "rows": [],
            "cols": [],
            "prompt_a_raw": a_raw,
            "prompt_a_parts": a_parts,
            "prompt_c_raw": "",
            "error": "Unknown failure",
        }
    except Exception as e:
        return {
            "stage": "pipeline_exception",
            "narrative": "Pipeline exception occurred.",
            "visualization": default_visualization([], []),
            "sql": "",
            "rows": [],
            "cols": [],
            "prompt_a_raw": a_raw,
            "prompt_a_parts": a_parts,
            "prompt_c_raw": "",
            "error": str(e),
        }

# =========================================================
# DOMAIN SELECTION FLOW
# =========================================================
def handle_pending_domain_selection(req: ChatRequest, session: Dict[str, Any], pending_domain: Dict[str, Any], raw_message: str):
    selected_scopes = parse_domain_selection_from_text(raw_message, pending_domain)
    if selected_scopes:
        product_term = pending_domain["product_term"]
        original_query = pending_domain.get("resume_query") or pending_domain.get("original_query") or ""
        product_terms = pending_domain.get("product_terms") or [product_term]
        save_product_scope(session, product_term, selected_scopes)
        session["active_focus"]["pending_domain_disambiguation"] = None
        session["active_focus"]["pending_product_term"] = None
        session["active_focus"]["pending_hs_code"] = None
        append_turn(session, "user", raw_message)

        selected_hs = flatten_scope_hs_codes(selected_scopes)
        assistant_text = f"Got it. I’ll use HS {', '.join(selected_hs)} for “{product_term}”."
        append_turn(session, "assistant", assistant_text)

        next_response = start_next_unknown_product_scope(session, req.session_id, original_query, product_terms, setup_only=bool(pending_domain.get("setup_only")))
        if next_response:
            next_response["messages"].insert(0, msg_answer(assistant_text))
            return next_response

        if pending_domain.get("setup_only") or not original_query or normalize_product_key(original_query) == normalize_product_key(product_term):
            session["state"] = STATE_IDLE
            followups = [
                f"Who are the top buyers of {product_term} in the last 24 months?",
                f"Which countries are showing increasing demand for {product_term} in the last 24 months?",
                f"Which month does demand for {product_term} usually peak over the last 36 months?",
            ]
            store_followups(session, followups)
            return {"session_id": req.session_id, "state": session["state"], "messages": [msg_answer(assistant_text), msg_followup(followups, title="Here are 3 questions you can run:")]}

        scoped_query = build_product_scopes_context(original_query, session, product_terms)
        session["active_focus"]["pending_query_text"] = scoped_query
        session["active_focus"]["pending_product_terms"] = product_terms
        session["state"] = STATE_AWAITING_CONFIRMATION
        return {"session_id": req.session_id, "state": session["state"], "messages": [msg_answer(assistant_text), msg_confirm(scoped_query)]}

    if is_common_question_while_pending(raw_message):
        normalized = normalize_user_message(raw_message)
        append_turn(session, "user", normalized)
        agent_out = safe_call_trade_agent(session, normalized)
        assistant_text = (agent_out.get("assistant_text") or "").strip() or "I can answer that, and your product domain selection will remain pending."
        append_turn(session, "assistant", assistant_text)
        store_general_trade_qa_history(session, normalized, assistant_text)
        option_labels = [o["label"] for o in pending_domain.get("options", [])]
        return {
            "session_id": req.session_id,
            "state": session["state"],
            "messages": [
                msg_answer(assistant_text),
                msg_followup(option_labels, title="Product scope is still pending. Quick select one domain, or type multiple HS codes:", limit=None),
            ],
        }

    if extract_explicit_hs_code(raw_message):
        return None

    product_term = pending_domain["product_term"]
    pending_domain["resume_query"] = raw_message
    assistant_text = f"Please select the intended domain/HS scope for “{product_term}” first. After selection, I’ll run: “{raw_message}”."
    append_turn(session, "user", raw_message)
    append_turn(session, "assistant", assistant_text)
    option_labels = [o["label"] for o in pending_domain.get("options", [])]
    return {
        "session_id": req.session_id,
        "state": session["state"],
        "messages": [
            msg_answer(assistant_text),
            msg_followup(option_labels, title="Quick select one domain, or type multiple HS codes like `HS 17, 18`:", limit=None),
        ],
    }

# =========================================================
# ROUTES
# =========================================================
@app.get("/")
def home():
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        return JSONResponse(status_code=404, content={"error": f"index.html not found at: {index_path}"})
    return FileResponse(index_path)

@app.get("/health")
def health():
    return {"ok": True, "model": GEMINI_MODEL, "table": TRADE_TABLE_NAME}

@app.post("/start_session")
def start_session():
    sid = str(uuid.uuid4())
    SESSIONS[sid] = new_session_state()
    return {"session_id": sid, "state": SESSIONS[sid]["state"]}

@app.post("/chat")
def chat(req: ChatRequest):
    if req.session_id not in SESSIONS:
        return JSONResponse(status_code=400, content={"error": "Invalid session_id"})
    session = SESSIONS[req.session_id]
    raw_message = (req.message or "").strip()
    if not raw_message:
        return JSONResponse(status_code=400, content={"error": "Empty message"})

    pending_domain = session["active_focus"].get("pending_domain_disambiguation")
    if pending_domain:
        response = handle_pending_domain_selection(req, session, pending_domain, raw_message)
        if response is not None:
            return response

    if req.source == "followup":
        if is_stale_domain_button(raw_message):
            assistant_text = "That HS/domain selection is no longer active. Please ask a trade question, or type a new product name to choose a different HS scope."
            append_turn(session, "user", raw_message)
            append_turn(session, "assistant", assistant_text)
            followups = safe_fallback_followups(session)
            store_followups(session, followups)
            return {"session_id": req.session_id, "state": session["state"], "messages": [msg_answer(assistant_text), msg_followup(followups, title="Suggested questions:")]}
        q = raw_message.strip()
        append_turn(session, "user", q)
        session["active_focus"]["pending_query_text"] = build_product_scopes_context(q, session)
        session["active_focus"]["pending_product_term"] = session["active_focus"].get("product_name")
        session["active_focus"]["pending_product_terms"] = [session["active_focus"].get("product_name")] if session["active_focus"].get("product_name") else []
        session["active_focus"]["pending_hs_code"] = session["active_focus"].get("hs_code")
        session["state"] = STATE_AWAITING_CONFIRMATION
        return {"session_id": req.session_id, "state": session["state"], "messages": [msg_confirm(session["active_focus"]["pending_query_text"])]}

    normalized = normalize_user_message(raw_message)
    append_turn(session, "user", normalized)

    if session["state"] == STATE_SETUP:
        parsed = extract_product_or_hs(normalized)
        if parsed["hs_code"]:
            label = parsed["hs_code"]
            session["active_focus"]["hs_code"] = label
            session["active_focus"]["product_name"] = None
            session["state"] = STATE_IDLE
            assistant_text = f"Got it. I’ll focus on HS: {label}"
            append_turn(session, "assistant", assistant_text)
            followups = [
                f"Who are the top buyers of HS {label} in the last 24 months?",
                f"Which countries are showing increasing demand for HS {label} in the last 24 months?",
                f"Which month does demand for HS {label} usually peak over the last 36 months?",
            ]
            store_followups(session, followups)
            return {"session_id": req.session_id, "state": session["state"], "messages": [msg_answer(assistant_text), msg_followup(followups, title="Here are 3 questions you can run:")]}

        product_term = parsed["product"]
        if product_term and not is_non_product_term(product_term):
            session["active_focus"]["product_name"] = product_term
            session["active_focus"]["hs_code"] = None
            response = start_next_unknown_product_scope(session, req.session_id, product_term, [product_term], setup_only=True)
            if response:
                return response
            session["state"] = STATE_IDLE
            assistant_text = f"Got it. I’ll focus on: {product_term}"
            append_turn(session, "assistant", assistant_text)
            followups = [
                f"Who are the top buyers of {product_term} in the last 24 months?",
                f"Which countries are showing increasing demand for {product_term} in the last 24 months?",
                f"Which month does demand for {product_term} usually peak over the last 36 months?",
            ]
            store_followups(session, followups)
            return {"session_id": req.session_id, "state": session["state"], "messages": [msg_answer(assistant_text), msg_followup(followups, title="Here are 3 questions you can run:")]}

    if session["state"] == STATE_AWAITING_CONFIRMATION:
        low = normalized.lower().strip()
        if low in {"yes", "y", "ok", "okay", "proceed", "run"}:
            return _confirm_internal(req.session_id, "proceed")
        if low in {"no", "n", "cancel", "stop"}:
            return _confirm_internal(req.session_id, "cancel")

    agent_out = safe_call_trade_agent(session, normalized)
    action = (agent_out.get("action") or "").upper().strip()
    assistant_text = (agent_out.get("assistant_text") or "").strip() or "Okay."
    clarification_q = agent_out.get("clarification_question")
    final_query_text = agent_out.get("final_query_text")
    product_term = agent_out.get("product_term")
    product_terms = agent_out.get("product_terms") if isinstance(agent_out.get("product_terms"), list) else []
    hs_code = agent_out.get("hs_code")
    followups = agent_out.get("followups") if isinstance(agent_out.get("followups"), list) else []

    product_terms = [p for p in product_terms if isinstance(p, str) and p.strip() and not is_non_product_term(p)]
    if product_term and not is_non_product_term(product_term) and product_term not in product_terms:
        product_terms.append(product_term)
    product_terms = dedupe_preserve(product_terms)

    messages: List[Dict[str, Any]] = [msg_answer(assistant_text)]
    append_turn(session, "assistant", assistant_text)

    followups = dedupe_preserve(followups)
    if len(followups) < 3:
        followups = safe_fallback_followups(session)
    store_followups(session, followups)

    if not hs_code and product_terms:
        response = start_next_unknown_product_scope(session, req.session_id, final_query_text or normalized, product_terms, setup_only=False)
        if response:
            return response

    if action == "ASK_CLARIFICATION":
        session["state"] = STATE_AWAITING_CLARIFICATION
        if not clarification_q:
            clarification_q = "What do you want to achieve—buyers/sellers, demand, competition, price, trend, peak month, onboarding, or ports?"
        session["active_focus"]["pending_clarification"] = clarification_q
        messages.append(msg_clarification(clarification_q))
        messages.append(msg_followup(followups, title="Suggested next steps:"))
        append_turn(session, "assistant", f"Clarification: {clarification_q}")
        return {"session_id": req.session_id, "state": session["state"], "messages": messages}

    if action == "CONFIRM_QUERY":
        if not final_query_text:
            session["state"] = STATE_AWAITING_CLARIFICATION
            clarification_q = "Please rephrase as a concrete trade question you want to run."
            session["active_focus"]["pending_clarification"] = clarification_q
            messages.append(msg_clarification(clarification_q))
            messages.append(msg_followup(followups, title="Suggested questions:"))
            append_turn(session, "assistant", f"Clarification: {clarification_q}")
            return {"session_id": req.session_id, "state": session["state"], "messages": messages}

        scoped_query = build_product_scopes_context(final_query_text, session, product_terms)
        session["active_focus"]["pending_query_text"] = scoped_query
        session["active_focus"]["pending_product_term"] = product_terms[0] if product_terms else product_term
        session["active_focus"]["pending_product_terms"] = product_terms
        session["active_focus"]["pending_hs_code"] = hs_code
        session["state"] = STATE_AWAITING_CONFIRMATION
        messages.append(msg_confirm(scoped_query))
        messages.append(msg_followup(followups, title="Next best questions:"))
        return {"session_id": req.session_id, "state": session["state"], "messages": messages}

    session["state"] = STATE_READY
    store_general_trade_qa_history(session, normalized, assistant_text)
    messages.append(msg_followup(followups, title="Suggested questions:"))
    return {"session_id": req.session_id, "state": session["state"], "messages": messages}

@app.post("/confirm")
def confirm(req: ConfirmRequest):
    return _confirm_internal(req.session_id, (req.decision or "").strip().lower())

def _confirm_internal(session_id: str, decision: str):
    if session_id not in SESSIONS:
        return JSONResponse(status_code=400, content={"error": "Invalid session_id"})
    session = SESSIONS[session_id]
    if session["state"] != STATE_AWAITING_CONFIRMATION:
        return JSONResponse(status_code=400, content={"error": "No pending query to confirm"})
    if decision not in {"proceed", "cancel"}:
        return JSONResponse(status_code=400, content={"error": "decision must be proceed|cancel"})

    if decision == "cancel":
        session["active_focus"]["pending_query_text"] = None
        session["active_focus"]["pending_product_term"] = None
        session["active_focus"]["pending_product_terms"] = []
        session["active_focus"]["pending_hs_code"] = None
        session["state"] = STATE_IDLE
        assistant_text = "Cancelled. Ask another trade question anytime."
        append_turn(session, "assistant", assistant_text)
        followups = safe_fallback_followups(session)
        store_followups(session, followups)
        return {"session_id": session_id, "state": session["state"], "messages": [msg_answer(assistant_text), msg_followup(followups, title="Suggested questions:")]}

    final_query_text = session["active_focus"].get("pending_query_text") or ""
    pending_hs_code = session["active_focus"].get("pending_hs_code")
    pending_product_terms = session["active_focus"].get("pending_product_terms") or []

    session["active_focus"]["pending_query_text"] = None
    session["active_focus"]["pending_product_term"] = None
    session["active_focus"]["pending_product_terms"] = []
    session["active_focus"]["pending_hs_code"] = None

    if pending_hs_code:
        session["active_focus"]["hs_code"] = pending_hs_code
        session["active_focus"]["product_name"] = None
        session["active_focus"]["selected_hs_codes"] = []
    elif pending_product_terms:
        final_query_text = build_product_scopes_context(final_query_text, session, pending_product_terms)

    result = execute_prompt_pipeline(final_query_text)

    trace = {
        "prompt_a_parts": result.get("prompt_a_parts", {}) or {},
        "prompt_a_raw": result.get("prompt_a_raw", "") or "",
        "prompt_c_raw": result.get("prompt_c_raw", "") or "",
        "sql": result.get("sql", "") or "",
        "rows_preview": (result.get("rows") or [])[:RESULT_ROWS_TRACE_LIMIT],
        "rows": (result.get("rows") or [])[:RESULT_ROWS_TRACE_LIMIT],
        "visualization": result.get("visualization") or default_visualization(result.get("rows") or [], result.get("cols") or []),
        "meta": {"stage": result.get("stage")},
    }
    if result.get("error"):
        trace["error"] = result["error"]

    session["last_trace"] = trace
    session["active_focus"]["last_executed_query_text"] = final_query_text
    narrative = (result.get("narrative") or "").strip() or "Execution completed."
    session["active_focus"]["last_executed_answer"] = narrative

    session["state"] = STATE_READY
    append_turn(session, "assistant", narrative)

    if result.get("stage") != "success":
        return {"session_id": session_id, "state": session["state"], "messages": [msg_answer(narrative, data=trace)]}

    store_executed_trade_history(
        session=session,
        user_query=final_query_text,
        answer=narrative,
        result_json={
            "stage": result.get("stage"),
            "cols": result.get("cols"),
            "rows": result.get("rows"),
            "visualization": result.get("visualization"),
            "error": result.get("error"),
        },
    )

    followups = generate_post_execution_followups(session)
    messages = [msg_answer(narrative, data=trace)]
    if followups:
        messages.append(msg_followup(followups, title="Follow-up questions:"))
    return {"session_id": session_id, "state": session["state"], "messages": messages}

@app.get("/favicon.ico")
def favicon():
    return JSONResponse(status_code=204, content=None)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=True)
