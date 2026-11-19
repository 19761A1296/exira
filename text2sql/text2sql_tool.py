# text2sql/text2sql_tool.py
# Agentic Trade Chatbot engine. Called in-process through engine_adapter.py
# (start_session / chat / confirm); there is no HTTP server here.
# Core chat/domain/confirmation flow kept stable.
# Visualization support is added only after successful Prompt A/B/C execution.
# Paste your final Prompt A, Prompt B, Prompt C, Trade Agent, Followup Agent prompts into the placeholders below.
#
# DATABASE: VeloDB (Apache Doris engine), reached over the MySQL protocol via PyMySQL.
#   pip install pymysql
#   .env:
#     VELODB_HOST, VELODB_PORT (MySQL/query port, usually 9030), VELODB_USER,
#     VELODB_PASSWORD, VELODB_DATABASE, VELODB_TRADE_TABLE_NAME

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
import pymysql
from dotenv import load_dotenv
from dataclasses import dataclass
from text2sql.text2sql_prompts.text2sql_prompts_all import PROMPT_A_TEMPLATE, PROMPT_B_TEMPLATE, PROMPT_C_TEMPLATE, TRADE_AGENT_PROMPT_TEMPLATE, FOLLOWUP_AGENT_PROMPT_TEMPLATE, NORMALIZE_PROMPT_TEMPLATE, PRODUCT_SCOPE_RESOLVER_PROMPT_TEMPLATE, PROMPT_A_SKELETON_BIBLE
# =========================================================
# PATHS / ENV
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
ENV_PATH = PROJECT_ROOT / ".env"
load_dotenv(ENV_PATH)

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
PRODUCT_SCOPE_RESOLVER_MODEL = (os.getenv("PRODUCT_SCOPE_RESOLVER_MODEL") or "google/gemma-4-31b-it").strip()
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DEBUG = (os.getenv("DEBUG_TRADE", "1") or "").strip() not in {"0", "false", "False", ""}


NORMALIZE_MAX_TOKENS = int(os.getenv("NORMALIZE_MAX_TOKENS", "1000"))
TRADE_AGENT_MAX_TOKENS = int(os.getenv("TRADE_AGENT_MAX_TOKENS", "5000"))
FOLLOWUP_AGENT_MAX_TOKENS = int(os.getenv("FOLLOWUP_AGENT_MAX_TOKENS", "1200"))
PRODUCT_SCOPE_RESOLVER_MAX_TOKENS = int(os.getenv("PRODUCT_SCOPE_RESOLVER_MAX_TOKENS", "2500"))
PROMPT_A_MAX_TOKENS = int(os.getenv("PROMPT_A_MAX_TOKENS", "22000"))
PROMPT_B_MAX_TOKENS = int(os.getenv("PROMPT_B_MAX_TOKENS", "5000"))
PROMPT_C_MAX_TOKENS = int(os.getenv("PROMPT_C_MAX_TOKENS", "3500"))

MAX_SQL_FIX_ATTEMPTS = int(os.getenv("MAX_SQL_FIX_ATTEMPTS", "3"))

# ---------------- VeloDB (Apache Doris, MySQL protocol) ----------------
VELODB_HOST = (os.getenv("VELODB_HOST") or "").strip()
VELODB_PORT = int((os.getenv("VELODB_PORT") or "9030").strip())
VELODB_USER = (os.getenv("VELODB_USER") or "").strip()
VELODB_PASSWORD = os.getenv("VELODB_PASSWORD") or ""
VELODB_DATABASE = (os.getenv("VELODB_DATABASE") or "").strip()
TRADE_TABLE_NAME = (os.getenv("VELODB_TRADE_TABLE_NAME"))

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
Engine: VeloDB (Apache Doris) - MySQL-compatible SQL dialect
Table: {TRADE_TABLE_NAME}
- SHIPPING_DATE (date): used for all time filters, comparisons, rolling windows
- SHIPMENT_VALUE (numeric): monetary metric (default metric)
- QUANTITY (numeric): volume metric (unit-sensitive)
- UNIT (text): required only when aggregating quantity or computing price
- PRODUCT_DESCRIPTION_NORM (text): ONLY product-description field used by Text2SQL; already uppercase, whitespace-clean, and special-character-normalized
- HS_CODE (text): hierarchical product identifier
- BUYER: BUYER_COMPANY, BUYER_COUNTRY, BUYER_STATE, BUYER_CITY, BUYER_ADDRESS
- SELLER: SELLER_COMPANY, SELLER_COUNTRY, SELLER_STATE, SELLER_CITY, SELLER_ADDRESS
- PORTS: PORT_OF_ORIGIN, PORT_OF_DESTINATION
- DECL_NO (text): nullable shipment reference; not a logical unique key and never use as grouping/logical dimension
- Raw PRODUCT_DESCRIPTION may exist physically in VeloDB but MUST NOT be used by Text2SQL
"""

SCHEMA_DESCRIPTION = f"""Engine: VeloDB (Apache Doris) - MySQL-compatible SQL dialect
Table Name: {TRADE_TABLE_NAME}
Columns:
- SHIPPING_DATE (date)
- QUANTITY (numeric)
- SHIPMENT_VALUE (numeric)
- PRODUCT_DESCRIPTION_NORM (text; canonical product field used by Text2SQL)
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
Note: raw PRODUCT_DESCRIPTION may exist physically, but it is intentionally excluded from this Text2SQL schema and MUST NOT be used.
"""

# Injected into Prompt A and Prompt B so the LLM writes Doris SQL instead of Snowflake SQL.
# If a prompt template contains the placeholder {sql_dialect_notes}, it is inserted there;
# otherwise it is prepended to the prompt.
VELODB_SQL_DIALECT_NOTES = """TARGET SQL ENGINE: VeloDB Cloud 26.0.5.1 (Apache Doris-compatible SQL via MySQL protocol). NOT Snowflake.
This runtime block is authoritative and MUST agree with Prompt A/B. Preserve business semantics while using only this VeloDB contract.

- Output exactly ONE read-only SELECT/WITH statement.
- Physical trade columns are lowercase.
- Month shifts: MONTHS_ADD(date_expr, signed_months).
- Day differences: DATEDIFF(end_date, start_date). For calendar month-boundary differences use (YEAR(end_date)*12 + MONTH(end_date)) - (YEAR(start_date)*12 + MONTH(start_date)).
- Date truncation: DATE_TRUNC(date_expr, 'month'|'quarter'|'year').
- Exact percentile: PERCENTILE_CONT(CAST(value_expr AS DOUBLE), percentile). Never use WITHIN GROUP or a percentile window.
- REGR_SLOPE / REGR_INTERCEPT are supported; REGR_R2 is not. Derive r2 with guarded CORR/VAR_POP when required.
- COUNT_IF is unavailable; use SUM(CASE WHEN condition THEN 1 ELSE 0 END).
- Use CONCAT(...) for concatenation and GROUP_CONCAT for string aggregation. Do not use || or LISTAGG.
- HS_CODE is a clean digit-only VARCHAR in production. Preserve leading zeros and filter the physical hs_code directly; never wrap hs_code in REGEXP_REPLACE/REPLACE, SUBSTR, LEFT, or casts for filtering. Sanitize only user-provided HS text before SQL. Use LIKE for one hierarchical prefix, a parenthesized OR of direct hs_code LIKE '<prefix>%' predicates for multiple hierarchical prefixes, and =/IN only for exact full HS codes.
- PRODUCT_DESCRIPTION_NORM is the ONLY product-description column allowed for Text2SQL. It is already uppercase, whitespace-clean, and special-character-normalized. Raw product_description may exist physically but MUST NOT be referenced by generated SQL.
- Product matching uses exact substring plus a token-level NGRAM_SEARCH fallback for ASCII product tokens of length >= 4. The fuzzy branch MUST use tokens directly from SPLIT_BY_STRING(td.product_description_norm, ' '), never REGEXP_REPLACE/UPPER/TRIM around the stored column and never whole-description NGRAM_SEARCH. The canonical threshold is 0.80 with a token-length difference of at most 2 characters. REGEXP_EXTRACT_ALL must NOT be used as the ARRAY source on this production cluster.
- ARRAY_COUNT, SPLIT_BY_STRING, CHAR_LENGTH, ABS, and NGRAM_SEARCH(text, pattern, 2) are permitted for that canonical product predicate.
- All stored text dimensions used by Text2SQL are already uppercase and whitespace-clean. Keep database columns bare in equality, LIKE, joins, DISTINCT, GROUP BY, COUNT(DISTINCT), and window partitions; never wrap them in UPPER(), LOWER(), or TRIM(). Normalize only user-provided literals before SQL generation.
- When both PRODUCT_DESCRIPTION_NORM and HS scope apply, they are ALWAYS combined as (PRODUCT_MATCH) AND (HS_SCOPE). Internal OR is allowed only inside PRODUCT_MATCH (exact OR fuzzy) or inside a multi-code HS set. Never use (PRODUCT_MATCH) OR (HS_SCOPE) unless the user explicitly asks for either/union semantics.
- HS scope precedence: CURRENT explicit HS code(s) in the user/sub-question override any older remembered/resolved product HS scope. Reuse saved scope only when the current request supplies no explicit HS scope.
- JAROWINKLER_SIMILARITY, LEVENSHTEIN, Damerau-Levenshtein, TIMESTAMPDIFF, ILIKE, and Snowflake DATEADD syntax are forbidden.
- Ports and countries are standardized in production. Countries resolve to canonical uppercase literals and then use direct equality. Ports resolve to one standardized DISTINCT stored value (exact first, contains second, NGRAM_SEARCH >= 0.90 only as typo fallback), then fact rows use direct equality. Never fuzzy-match every fact row. Company matching keeps its existing candidate-resolution rules, but final fact-row comparisons use bare canonical columns.
- Use single quotes for literals; unquoted identifiers when possible, backticks only if required; never double-quoted identifiers.
- The imports_exports schema is flat: never generate GET(...), FLATTEN, ::VARIANT, or ARRAY/VARIANT access patterns. ARRAY values created transiently by SPLIT_BY_STRING for canonical product-token matching are allowed.
- No QUALIFY: materialize window results in a CTE/subquery and filter at the next query level.
"""

def apply_sql_dialect(prompt: str) -> str:
    if "{sql_dialect_notes}" in prompt:
        return prompt.replace("{sql_dialect_notes}", VELODB_SQL_DIALECT_NOTES)
    return VELODB_SQL_DIALECT_NOTES + "\n\n" + prompt





# Engine session state lives in a shared store (Redis in production),
# not in this process. See text2sql/session_store.py.
from text2sql.session_store import STORE as SESSION_STORE, SessionBusy

# =========================================================
# REQUEST MODELS
# =========================================================
@dataclass
class ChatRequest:
    session_id: str
    message: str
    source: Optional[str] = None

@dataclass
class ConfirmRequest:
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

def completed_month_window(months: int) -> Tuple[datetime.date, datetime.date]:
    """[start, end) covering the last `months` completed calendar months."""
    end = datetime.date.today().replace(day=1)
    y, m = divmod(end.year * 12 + (end.month - 1) - months, 12)
    start = datetime.date(y, m + 1, 1)
    return start, end

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
    # MySQL-protocol engines treat backslash as an escape character too.
    return (value or "").replace("\\", "\\\\").replace("'", "''").strip()

# Canonical product/HS filter contract. Keep this in sync with Prompt A/B and the thinking layer.
PRODUCT_FUZZY_NGRAM_THRESHOLD = 0.80
PRODUCT_SCOPE_CONTRACT_HEADER = "SYSTEM_RESOLVED_PRODUCT_FILTER_CONTRACT"
PRODUCT_MATCH_STOPWORDS = {"A", "AN", "THE", "OF", "AND", "OR", "FOR", "WITH", "IN", "ON", "TO", "BY"}

def _sql_string_literal(value: str) -> str:
    return "'" + sanitize_sql_literal(value) + "'"

def product_match_tokens(product_term: str) -> List[str]:
    norm = normalize_product_key(product_term)
    tokens = re.findall(r"[A-Z0-9]+", norm)
    meaningful = [t for t in tokens if t not in PRODUCT_MATCH_STOPWORDS]
    return meaningful or tokens

def build_canonical_product_match_sql(product_term: str, alias: str = "td") -> str:
    """Return the authoritative VeloDB PRODUCT_DESCRIPTION_NORM predicate.

    product_description_norm is already uppercase, whitespace-clean and special-character
    normalized in storage. Exact substring matching is retained. For ASCII tokens length
    >=4, token-level NGRAM_SEARCH provides the fuzzy fallback without any per-row UPPER,
    TRIM or REGEXP_REPLACE work.
    """
    norm = normalize_product_key(product_term)
    if not norm:
        return "1 = 1"
    phrase_lit = _sql_string_literal(norm)
    exact_phrase = f"{alias}.product_description_norm LIKE CONCAT('%', {phrase_lit}, '%')"
    tokens = product_match_tokens(norm)
    if not tokens:
        return exact_phrase

    token_predicates = []
    for token in tokens:
        token_lit = _sql_string_literal(token)
        exact_token = f"{alias}.product_description_norm LIKE CONCAT('%', {token_lit}, '%')"
        if token.isascii() and token.isalnum() and len(token) >= 4:
            min_len = max(2, len(token) - 2)
            max_len = len(token) + 2
            fuzzy_token = (
                "(ARRAY_COUNT(tok -> ("
                f"CHAR_LENGTH(tok) BETWEEN {min_len} AND {max_len} "
                f"AND NGRAM_SEARCH(tok, {token_lit}, 2) >= {PRODUCT_FUZZY_NGRAM_THRESHOLD:.2f}"
                "), "
                f"SPLIT_BY_STRING({alias}.product_description_norm, ' ')) > 0)"
            )
            token_predicates.append(f"({exact_token} OR {fuzzy_token})")
        else:
            token_predicates.append(exact_token)

    if len(tokens) == 1:
        return token_predicates[0]

    # Multi-word products: exact phrase OR all meaningful tokens, each with its own
    # exact/fuzzy token match. This keeps required product words conjunctive.
    return f"({exact_phrase} OR ({' AND '.join(token_predicates)}))"

def build_canonical_hs_scope_sql(hs_codes: List[str], alias: str = "td") -> str:
    """Build the authoritative HS predicate for clean digit-only VARCHAR hs_code.

    Production invariant: the physical column already contains only digits and preserves
    leading zeros. Sanitize only user input; never normalize hs_code inside SQL.
    2/4/6-digit values are hierarchical prefixes; 8/10-digit values are full codes.
    """
    codes = [c for c in [normalize_hs_token(x) for x in (hs_codes or [])] if c]
    codes = sorted(set(codes))
    if not codes:
        return "1 = 1"

    # Keep the physical hs_code column bare for prefix pruning/index use.
    # Hierarchical prefixes are always direct LIKE predicates, even when several
    # prefixes have the same length. Exact full codes may use equality/IN.
    if len(codes) > 1 and all(len(c) in {8, 10} for c in codes):
        literals = ", ".join(_sql_string_literal(c) for c in codes)
        return f"{alias}.hs_code IN ({literals})"

    parts = []
    for code in codes:
        lit = _sql_string_literal(code)
        if len(code) in {2, 4, 6}:
            parts.append(f"{alias}.hs_code LIKE CONCAT({lit}, '%')")
        else:
            parts.append(f"{alias}.hs_code = {lit}")
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"

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
            "pending_explicit_hs_codes": [],
            "pending_profile_product_hs_map": {},
            "pending_scope_resolver_trace": None,
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
def call_openrouter_model(prompt: str, max_tokens: int, model: str, temperature: float = 0.0) -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    resp = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
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


def call_gemini(prompt: str, max_tokens: int, temperature: float = 0.0) -> str:
    """Name kept for compatibility. Main agents are routed through the configured GEMINI_MODEL."""
    return call_openrouter_model(prompt, max_tokens=max_tokens, model=GEMINI_MODEL, temperature=temperature)
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
# PRODUCT SCOPE RESOLVER (PRE-TEXT2SQL LAYER)
# =========================================================
def get_company_product_profile(session: Dict[str, Any]) -> Any:
    """Return only the authoritative company product card used as resolver evidence."""
    user_memory = session.get("active_focus", {}).get("user_memory", {}) or {}
    return user_memory.get("user_product_info")


def _resolver_profile_blob(profile: Any) -> str:
    try:
        return json.dumps(profile, ensure_ascii=False, default=str)
    except Exception:
        return str(profile or "")


def _resolver_evidence_norm(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()


def _profile_contains_hs(profile_blob: str, hs_code: str) -> bool:
    return bool(re.search(r"(?<!\d)" + re.escape(hs_code) + r"(?!\d)", profile_blob or ""))


def _profile_contains_product(profile_blob: str, product_text: str) -> bool:
    p = _resolver_evidence_norm(product_text)
    if not p:
        return False
    blob = _resolver_evidence_norm(profile_blob)
    return p in blob


def _build_safe_profile_enriched_query(candidate_query: str, resolved_products: List[Dict[str, Any]]) -> str:
    """Render resolver evidence without allowing the model to rewrite analytical intent."""
    base = (candidate_query or "").strip()
    if not resolved_products:
        return base
    labels = []
    for item in resolved_products:
        product = str(item.get("user_product") or item.get("profile_product") or "").strip()
        codes = [c for c in item.get("hs_codes") or [] if c]
        if product and codes:
            labels.append(f"{product} (HS {', '.join(codes)})")
    if not labels:
        return base
    return base + "\n\nResolved company product scopes for this query: " + "; ".join(labels)


def _sanitize_resolver_output(data: Dict[str, Any], profile: Any, candidate_query: str) -> Dict[str, Any]:
    """Fail closed: only profile-verifiable HIGH-confidence product->HS pairs survive."""
    profile_blob = _resolver_profile_blob(profile)
    scope_type = str(data.get("scope_type") or "GLOBAL").upper().strip()
    if scope_type not in {"GLOBAL", "USER_COMPANY", "OTHER_COMPANY"}:
        scope_type = "GLOBAL"

    valid_products: List[Dict[str, Any]] = []
    merged: Dict[str, Dict[str, Any]] = {}
    for item in data.get("resolved_products") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("confidence") or "").upper().strip() != "HIGH":
            continue
        user_product = re.sub(r"\s+", " ", str(item.get("user_product") or "").strip())
        profile_product = re.sub(r"\s+", " ", str(item.get("profile_product") or "").strip())
        if not user_product or not profile_product or not _profile_contains_product(profile_blob, profile_product):
            continue
        codes = []
        for raw_code in item.get("hs_codes") or []:
            code = normalize_hs_token(str(raw_code))
            if code and _profile_contains_hs(profile_blob, code) and code not in codes:
                codes.append(code)
        if not codes:
            continue
        key = normalize_product_key(user_product)
        if key not in merged:
            merged[key] = {
                "user_product": user_product,
                "profile_product": profile_product,
                "hs_codes": [],
                "confidence": "HIGH",
            }
        for code in codes:
            if code not in merged[key]["hs_codes"]:
                merged[key]["hs_codes"].append(code)
    valid_products = list(merged.values())

    unresolved = [
        re.sub(r"\s+", " ", str(x).strip())
        for x in (data.get("unresolved_products") or [])
        if isinstance(x, str) and x.strip()
    ]
    unresolved = dedupe_preserve(unresolved)

    action = str(data.get("action") or "PASS_THROUGH").upper().strip()
    # Fail closed for partial/ambiguous mapping: if the resolver says any requested
    # product is unresolved, leave the whole query untouched and let Text2SQL use its
    # existing HS-selection flow. This prevents silently analyzing only a subset.
    if scope_type != "USER_COMPANY" or action != "REWRITE" or not valid_products or unresolved:
        action = "PASS_THROUGH"
        valid_products = []

    # The LLM decides scope and semantic profile mapping, but it is not allowed to rewrite
    # analytics wording directly. Render the enrichment deterministically from the original
    # candidate query so dates/geography/metrics/engine intent cannot drift.
    rewritten = _build_safe_profile_enriched_query(candidate_query, valid_products) if action == "REWRITE" else candidate_query
    return {
        "scope_type": scope_type,
        "action": action,
        "resolved_products": valid_products,
        "unresolved_products": unresolved,
        "rewritten_query": rewritten,
        "reason": str(data.get("reason") or "").strip(),
    }


def build_product_scope_resolver_prompt(
    session: Dict[str, Any],
    original_user_query: str,
    candidate_query: str,
    current_explicit_hs_codes: List[str],
) -> Optional[str]:
    profile = get_company_product_profile(session)
    if profile in (None, "", {}, []):
        return None
    payload = {
        "original_user_query": original_user_query,
        "candidate_query": candidate_query,
        "company_profile": profile,
        "current_explicit_hs_codes": current_explicit_hs_codes or [],
    }
    return PRODUCT_SCOPE_RESOLVER_PROMPT_TEMPLATE.replace(
        "{PRODUCT_SCOPE_RESOLVER_INPUT_JSON}",
        json.dumps(payload, ensure_ascii=False, default=str),
    )


def resolve_company_product_scope(
    session: Dict[str, Any],
    original_user_query: str,
    candidate_query: str,
    current_explicit_hs_codes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Use Gemma only for company-profile semantic mapping; failure always passes through."""
    candidate_query = (candidate_query or original_user_query or "").strip()
    explicit = [c for c in [normalize_hs_token(x) for x in (current_explicit_hs_codes or [])] if c]
    profile = get_company_product_profile(session)
    default = {
        "scope_type": "GLOBAL",
        "action": "PASS_THROUGH",
        "resolved_products": [],
        "unresolved_products": [],
        "rewritten_query": candidate_query,
        "reason": "Resolver not applied.",
    }
    if not candidate_query or profile in (None, "", {}, []):
        return default
    if explicit:
        default["reason"] = "Current user turn already contains explicit HS code(s)."
        return default
    prompt = build_product_scope_resolver_prompt(session, original_user_query, candidate_query, explicit)
    if not prompt:
        return default
    try:
        raw = call_openrouter_model(
            prompt,
            max_tokens=PRODUCT_SCOPE_RESOLVER_MAX_TOKENS,
            model=PRODUCT_SCOPE_RESOLVER_MODEL,
            temperature=0.0,
        )
        parsed = parse_json_strict(raw)
        result = _sanitize_resolver_output(parsed, profile, candidate_query)
        dbg("PRODUCT SCOPE RESOLVER", {"model": PRODUCT_SCOPE_RESOLVER_MODEL, "result": result})
        return result
    except Exception as exc:
        default["reason"] = f"Resolver failed closed: {exc}"
        dbg("PRODUCT SCOPE RESOLVER FAILED", str(exc))
        return default


def resolver_product_hs_override_map(resolver_result: Dict[str, Any]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for item in resolver_result.get("resolved_products") or []:
        product = re.sub(r"\s+", " ", str(item.get("user_product") or "").strip())
        codes = [c for c in [normalize_hs_token(x) for x in item.get("hs_codes") or []] if c]
        if product and codes:
            out[normalize_product_key(product)] = sorted(set(codes))
    return out


def resolver_product_terms(resolver_result: Dict[str, Any]) -> List[str]:
    return dedupe_preserve([
        str(item.get("user_product") or "").strip()
        for item in resolver_result.get("resolved_products") or []
        if isinstance(item, dict) and str(item.get("user_product") or "").strip()
    ])

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
    raw = re.sub(r"[^0-9]", "", (code or "").strip())
    if not re.fullmatch(r"\d{1,10}", raw):
        return None
    if len(raw) <= 2:
        return raw.zfill(2)
    return raw

def extract_explicit_hs_codes(text: str) -> List[str]:
    """Extract HS code/prefixes explicitly supplied in the CURRENT user turn.

    Examples supported:
      HS09
      HS-09
      HS 5201
      HS codes 19, 20, 21 and 35
      chilli-HS19,20,21,35

    This parser is intentionally conservative: ordinary numbers such as dates,
    top-N counts, and time windows are ignored unless introduced by an HS marker.
    A message consisting only of HS digits is also accepted for the HS-only setup flow.
    """
    raw = (text or "").strip()
    if not raw:
        return []

    found: List[str] = []
    # Capture one HS list immediately following an explicit HS marker:
    # HS / HTS, optionally followed by CODE(S) / CHAPTER(S) / HEADING(S), or a bare
    # CHAPTER(S) / HEADING(S) marker (HS chapters/headings in trade context).
    # Separators may be comma, slash, ampersand, or the word "and".
        # HS marker, optionally followed by codes / chapters / headings / prefixes,
    # then a list of codes. Ranges like "50-63" or "50 to 63" are allowed.
    pattern = re.compile(
        r"(?i)\bhs(?:\s*(?:codes?|chapters?|headings?|prefix(?:es)?))?\s*[-:]?\s*"
        r"((?:\d{1,10})(?:\s*(?:-|–|to)\s*\d{1,10})?"
        r"(?:\s*(?:,|/|&|\band\b|,\s*and\b)\s*\d{1,10}(?:\s*(?:-|–|to)\s*\d{1,10})?)*)"
    )
    for match in pattern.finditer(raw):
        for start, end in re.findall(r"(\d{1,10})(?:\s*(?:-|–|to)\s*(\d{1,10}))?", match.group(1)):
            tokens = [start]
            if end and len(start) == len(end) <= 2 and int(start) < int(end) <= 99:
                # chapter range, e.g. 50-63 -> 50, 51, ..., 63
                tokens = [str(n) for n in range(int(start), int(end) + 1)]
            elif end:
                tokens = [start, end]
            for token in tokens:
                norm = normalize_hs_token(token)
                if norm and norm not in found:
                    found.append(norm)

    if not found and re.fullmatch(r"\d{2,10}", raw):
        norm = normalize_hs_token(raw)
        if norm:
            found.append(norm)
    return found


def extract_explicit_hs_code(text: str) -> Optional[str]:
    codes = extract_explicit_hs_codes(text)
    return codes[0] if codes else None

def _build_hs2_probe_sql(product_term: str, use_fuzzy: bool) -> Tuple[str, List[Any]]:
    # %s placeholders are filled by PyMySQL (safe quoting/escaping).
    tokens = product_match_tokens(product_term)
    if not tokens:
        tokens = [normalize_product_key(product_term)] if normalize_product_key(product_term) else []

    product_clauses: List[str] = []
    product_params: List[Any] = []
    for token in tokens:
        token = token.upper()
        like_clause = "td.product_description_norm LIKE %s"
        product_params.append(f"%{token}%")
        if use_fuzzy and token.isascii() and token.isalnum() and len(token) >= 4:
            min_len = max(2, len(token) - 2)
            max_len = len(token) + 2
            product_clauses.append(
                "(" + like_clause + " OR (ARRAY_COUNT(tok -> ("
                f"CHAR_LENGTH(tok) BETWEEN {min_len} AND {max_len} "
                "AND NGRAM_SEARCH(tok, %s, 2) >= 0.80), "
                "SPLIT_BY_STRING(td.product_description_norm, ' ')) > 0))"
            )
            product_params.append(token)
        else:
            product_clauses.append(like_clause)

    product_sql = " AND ".join(product_clauses) if product_clauses else "1 = 0"
    sql = f"""
SELECT
    SUBSTR(td.hs_code, 1, 2) AS HS2,
    COUNT(*) AS RECORD_COUNT
FROM {TRADE_TABLE_NAME} AS td
WHERE td.hs_code IS NOT NULL
  AND td.hs_code <> ''
  AND td.product_description_norm IS NOT NULL
  AND td.shipping_date IS NOT NULL
  AND td.shipping_date >= CAST(%s AS DATE)
  AND td.shipping_date < CAST(%s AS DATE)
  AND ({product_sql})
GROUP BY SUBSTR(td.hs_code, 1, 2)
ORDER BY RECORD_COUNT DESC, HS2
""".strip()
    return sql, product_params


def run_hs2_probe_sql(product_term: str) -> List[Dict[str, Any]]:
    term = (product_term or "").strip()
    if not term:
        return []
    start, end = completed_month_window(HS_PROBE_MONTHS)

    # First try the same canonical token-level fuzzy product semantics used by final SQL.
    sql, product_params = _build_hs2_probe_sql(term, use_fuzzy=True)
    params = (start.isoformat(), end.isoformat(), *product_params)
    ok, rows, cols, err = run_sql(sql, params)

    # Conservative compatibility fallback if token-array functions are unavailable.
    if not ok:
        print("HS PROBE token fuzzy match failed, retrying with LIKE-only product matching:", err)
        sql, product_params = _build_hs2_probe_sql(term, use_fuzzy=False)
        params = (start.isoformat(), end.isoformat(), *product_params)
        ok, rows, cols, err = run_sql(sql, params)

    print("HS PROBE PRODUCT:", product_term)
    print("HS PROBE SQL:", sql)
    print("HS PROBE PARAMS:", params)
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


def get_mentioned_memory_products(session: Dict[str, Any], text: str) -> List[str]:
    """Return remembered product names explicitly mentioned in the current text.

    This deterministic backstop protects session-scope precedence even if the Trade
    Agent forgets to return product_terms for a message such as chilli-HS19,20,21,35.
    """
    q_norm = normalize_product_key(text)
    out: List[str] = []
    for item in (session["active_focus"].get("product_hs_memory", {}) or {}).values():
        term = (item or {}).get("product_term")
        key = normalize_product_key(term or "")
        if not key:
            continue
        if re.search(r"(?<![A-Z0-9])" + re.escape(key) + r"(?![A-Z0-9])", q_norm):
            out.append(term)
    return dedupe_preserve(out)

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

def build_product_scopes_context(
    original_query: str,
    session: Dict[str, Any],
    product_terms: Optional[List[str]] = None,
    explicit_hs_overrides: Optional[Dict[str, List[str]]] = None,
    profile_hs_overrides: Optional[Dict[str, List[str]]] = None,
) -> str:
    """Append one authoritative, idempotent product/HS filter contract.

    Precedence is deterministic:
      1) HS code(s) explicitly supplied in the CURRENT user turn,
      2) current-query company-profile resolver scope,
      3) the latest saved session scope for that product,
      4) no contract (the normal product-scope resolution flow handles it).

    Current explicit HS scope never gets overwritten by older product_hs_memory.
    The LLM receives the already-resolved SQL predicate instead of reconstructing the
    AND/OR relationship on every call. Repeated calls replace the old contract rather
    than appending duplicates.
    """
    memory = session["active_focus"].get("product_hs_memory", {}) or {}
    base_query = (original_query or "").split(f"\n\n{PRODUCT_SCOPE_CONTRACT_HEADER}", 1)[0].rstrip()

    cleaned_terms = []
    for term in product_terms or []:
        if isinstance(term, str) and term.strip() and not is_non_product_term(term):
            cleaned_terms.append(re.sub(r"\s+", " ", term.strip()))
    if not cleaned_terms:
        q_norm = normalize_product_key(base_query)
        for item in memory.values():
            term = item.get("product_term")
            if term and normalize_product_key(term) in q_norm:
                cleaned_terms.append(term)
    cleaned_terms = dedupe_preserve(cleaned_terms)

    override_map = explicit_hs_overrides or {}
    profile_map = profile_hs_overrides or {}
    blocks = []
    pair_predicates = []
    for term in cleaned_terms:
        term_key = normalize_product_key(term)
        override_codes = [
            c for c in [normalize_hs_token(x) for x in override_map.get(term_key, [])] if c
        ]
        profile_codes = [
            c for c in [normalize_hs_token(x) for x in profile_map.get(term_key, [])] if c
        ]
        if override_codes:
            # CURRENT user instruction always wins.
            hs_codes = sorted(set(override_codes))
            scope_source = "CURRENT_USER_EXPLICIT_HS"
        elif profile_codes:
            # Current-query profile resolution outranks stale session memory, but is NOT persisted as generic product memory.
            hs_codes = sorted(set(profile_codes))
            scope_source = "COMPANY_PROFILE_RESOLVER"
        else:
            item = memory.get(term_key)
            if not item:
                continue
            hs_codes = flatten_scope_hs_codes(item.get("selected_scopes") or [])
            if not hs_codes:
                hs_codes = [c for c in [normalize_hs_token(x) for x in item.get("selected_hs_codes", [])] if c]
            hs_codes = sorted(set(hs_codes))
            scope_source = "SESSION_PRODUCT_HS_MEMORY"
        if not hs_codes:
            continue

        product_sql = build_canonical_product_match_sql(term, alias="td")
        hs_sql = build_canonical_hs_scope_sql(hs_codes, alias="td")
        pair_predicate = f"(({product_sql}) AND ({hs_sql}))"
        pair_predicates.append(pair_predicate)
        blocks.append(
            f"Product: {term}\n"
            f"Scope source: {scope_source}\n"
            f"Selected HS scope: {', '.join(hs_codes)}\n"
            "Authoritative product-specific pair (preserve these semantics exactly):\n"
            f"{pair_predicate}"
        )

    if not blocks:
        return base_query

    overall_scope = pair_predicates[0] if len(pair_predicates) == 1 else "(\n  " + "\n  OR\n  ".join(pair_predicates) + "\n)"
    contract = (
        f"{PRODUCT_SCOPE_CONTRACT_HEADER} (AUTHORITATIVE; INTERNAL)\n"
        "HS precedence: CURRENT explicit user HS > current-query COMPANY_PROFILE_RESOLVER scope > latest saved session product scope.\n"
        "Company-profile resolver scopes apply ONLY to this current company-scoped query and must not be treated as generic/global product HS memory.\n"
        "The product-to-HS scopes below already reflect that precedence. Do not infer, broaden, narrow, or reconstruct them.\n"
        "For EACH product, preserve PRODUCT_MATCH AND ITS OWN HS_SCOPE. NEVER flatten HS codes from different products into one shared HS list.\n"
        "When more than one product-specific pair is listed, the allowed portfolio filter is the OR-union of the COMPLETE pairs: (PRODUCT_1 AND HS_1) OR (PRODUCT_2 AND HS_2) ... .\n"
        "If the user requests a per-product comparison/breakdown, keep pair identity when deriving product groups; do not cross-apply another product's HS codes.\n"
        "OR is permitted inside each product predicate for exact-vs-fuzzy matching and inside one product's HS predicate when that product has several HS codes.\n"
        "If SQL repeats the scope in more than one CTE, every occurrence must preserve exactly the same semantics. Prefer one scoped_data/base_scope CTE.\n\n"
        "OVERALL AUTHORITATIVE PRODUCT SCOPE PREDICATE:\n"
        f"{overall_scope}\n\n"
        + "\n\n".join(blocks)
    )
    return f"{base_query}\n\n{contract}"

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
    prompt = PROMPT_A_TEMPLATE.replace("{current_date}", current_date_pretty()).replace("{user_query}", user_query)
    return apply_sql_dialect(prompt)

def build_prompt_b(user_query: str, interpretation: str, plan: str, attempt_history: str, latest_sql: str, latest_error: str, attempt_index: int) -> str:
    if not has_real_prompt(PROMPT_B_TEMPLATE):
        raise RuntimeError("PROMPT_B_TEMPLATE not configured")
    prompt = (
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
    return apply_sql_dialect(prompt)

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
    raw = strip_sql_end(text or "").strip()
    if not raw:
        return ""
    m = re.search(r"(?is)\b(?:WITH|SELECT)\b", raw)
    if not m:
        return ""
    sql = raw[m.start():].strip()
    # Prompt B is required to emit one statement. If it adds trailing prose after
    # a semicolon, keep only the first SQL statement.
    if ";" in sql:
        sql = sql.split(";", 1)[0].rstrip()
    return sql

# Known production compatibility repair/backstop.
# The optimized production contract is:
#   * product_description_norm is the only product field used by Text2SQL,
#   * stored text dimensions are already uppercase + whitespace-clean,
#   * hierarchical HS prefix filters keep hs_code bare and use direct LIKE predicates.
_REGEXP_EXTRACT_PRODUCT_ARRAY_RE = re.compile(
    r"REGEXP_EXTRACT_ALL\(\s*(?:UPPER\(\s*)?([A-Za-z_][A-Za-z0-9_]*\.)?(?:product_description_norm|product_description)\s*\)?\s*,\s*'\(\[A-Z0-9\]\+\)'\s*\)",
    re.IGNORECASE,
)

_CANONICAL_TEXT_COLUMNS = (
    "product_description_norm",
    "buyer_country", "buyer_state", "buyer_city", "buyer_address", "buyer_company",
    "port_of_destination",
    "seller_country", "seller_state", "seller_city", "seller_address", "seller_company",
    "port_of_origin", "unit",
)
_CANONICAL_TEXT_COLUMN_ALT = "|".join(re.escape(c) for c in _CANONICAL_TEXT_COLUMNS)


def _replace_array_exists_with_scalar_count(sql: str) -> str:
    """Replace ARRAY_EXISTS(lambda, array) with (ARRAY_COUNT(lambda, array) > 0)."""
    text = sql or ""
    lower = text.lower()
    out = []
    i = 0
    token = "array_exists("
    while True:
        pos = lower.find(token, i)
        if pos < 0:
            out.append(text[i:])
            break
        out.append(text[i:pos])
        open_pos = pos + len("array_exists")
        depth = 0
        quote = None
        j = open_pos
        while j < len(text):
            ch = text[j]
            if quote:
                if ch == quote:
                    if j + 1 < len(text) and text[j + 1] == quote:
                        j += 2
                        continue
                    quote = None
                j += 1
                continue
            if ch in ("'", '"'):
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= len(text):
            out.append(text[pos:])
            break
        inner = text[open_pos + 1:j]
        out.append(f"(ARRAY_COUNT({inner}) > 0)")
        i = j + 1
    return "".join(out)


def _rewrite_hs_prefix_in_to_like(match: re.Match) -> str:
    """SUBSTR/LEFT(hs_code,1,N) IN (...) -> parenthesized OR of bare hs_code LIKE."""
    col = match.group("col")
    n = int(match.group("n"))
    items = match.group("items")
    codes = re.findall(r"'([0-9]+)'", items)
    if not codes or any(len(code) != n for code in codes):
        return match.group(0)
    return "(" + " OR ".join(f"{col} LIKE '{code}%'" for code in codes) + ")"


def enforce_static_velodb_sql_contract(sql: str) -> str:
    """Canonicalize stale model SQL to the optimized production access contract."""
    s = sql or ""

    # Raw product_description physically exists but is forbidden for Text2SQL.
    # Word-boundary replacement does not touch product_description_norm.
    s = re.sub(r"(?i)\bproduct_description\b", "product_description_norm", s)

    # product_description_norm is already normalized; remove stale row-level cleanup.
    s = re.sub(
        r"(?is)REGEXP_REPLACE\(\s*(?:UPPER\(\s*)?(?P<col>(?:[A-Za-z_][A-Za-z0-9_]*\.)?product_description_norm)\s*\)?\s*,\s*'\[\^A-Z0-9\]\+'\s*,\s*' '\s*\)",
        lambda m: m.group("col"),
        s,
    )

    # All canonical stored text dimensions are already uppercase and whitespace-clean.
    # Strip normalization wrappers only when they directly wrap one known physical column.
    col_pat = rf"(?P<col>(?:[A-Za-z_][A-Za-z0-9_]*\.)?(?:{_CANONICAL_TEXT_COLUMN_ALT}))"
    for pat in (
        rf"(?is)UPPER\(\s*TRIM\(\s*{col_pat}\s*\)\s*\)",
        rf"(?is)TRIM\(\s*UPPER\(\s*{col_pat}\s*\)\s*\)",
        rf"(?is)UPPER\(\s*{col_pat}\s*\)",
        rf"(?is)LOWER\(\s*{col_pat}\s*\)",
        rf"(?is)TRIM\(\s*{col_pat}\s*\)",
    ):
        s = re.sub(pat, lambda m: m.group("col"), s)

    # hs_code is clean digit-only VARCHAR: remove stale physical-column normalization.
    s = re.sub(
        r"(?is)REGEXP_REPLACE\(\s*TRIM\(\s*([A-Za-z_][A-Za-z0-9_]*\.)?hs_code\s*\)\s*,\s*'\[\^0-9\]'\s*,\s*''\s*\)",
        lambda m: f"{m.group(1) or ''}hs_code",
        s,
    )
    s = re.sub(
        r"(?is)REGEXP_REPLACE\(\s*([A-Za-z_][A-Za-z0-9_]*\.)?hs_code\s*,\s*'\[\^0-9\]'\s*,\s*''\s*\)",
        lambda m: f"{m.group(1) or ''}hs_code",
        s,
    )
    s = re.sub(
        r"(?is)REPLACE\(\s*REPLACE\(\s*REPLACE\(\s*([A-Za-z_][A-Za-z0-9_]*\.)?hs_code\s*,\s*'\.'\s*,\s*''\s*\)\s*,\s*'-'\s*,\s*''\s*\)\s*,\s*' '\s*,\s*''\s*\)",
        lambda m: f"{m.group(1) or ''}hs_code",
        s,
    )

    # Stale multi-prefix filters are rewritten to bare hs_code LIKE predicates.
    hs_prefix_in_re = re.compile(
        r"(?is)(?:SUBSTR\(\s*(?P<col>(?:[A-Za-z_][A-Za-z0-9_]*\.)?hs_code)\s*,\s*1\s*,\s*(?P<n>\d+)\s*\)|LEFT\(\s*(?P=col)\s*,\s*(?P=n)\s*\))\s+IN\s*\((?P<items>[^)]*)\)"
    )
    s = hs_prefix_in_re.sub(_rewrite_hs_prefix_in_to_like, s)

    return s


def apply_known_velodb_compatibility_repairs(sql: str, error: str) -> str:
    current = enforce_static_velodb_sql_contract(sql or "")
    err = (error or "").lower()
    if "lambda argument must be array" in err and "regexp_extract_all" in current.lower():
        def _replace_regexp_extract(m: re.Match) -> str:
            alias = m.group(1) or ""
            return f"SPLIT_BY_STRING({alias}product_description_norm, ' ')"
        current = _REGEXP_EXTRACT_PRODUCT_ARRAY_RE.sub(_replace_regexp_extract, current)
    if (
        "array<boolean>" in err
        or "target type=boolean" in err
        or "can not cast from origin type" in err
    ) and "array_exists" in current.lower():
        current = _replace_array_exists_with_scalar_count(current)
    return enforce_static_velodb_sql_contract(current)

# =========================================================
# VELODB EXECUTION
# =========================================================
READ_ONLY_SQL_RE = re.compile(r"(?is)^\s*(?:(?:--[^\n]*\n|/\*.*?\*/)\s*)*(SELECT|WITH)\b")

def get_velodb_conn():
    if not VELODB_HOST:
        raise RuntimeError("VELODB_HOST is not set")
    return pymysql.connect(
        host=VELODB_HOST,
        port=VELODB_PORT,
        user=VELODB_USER,
        password=VELODB_PASSWORD,
        database=VELODB_DATABASE or None,
        charset="utf8mb4",
        autocommit=True,
    )

def prepare_sql_for_execution(sql: str) -> str:
    s = (sql or "").strip()
    while s.endswith(";"):
        s = s[:-1].rstrip()
    return s

def run_sql(sql: str, params: Optional[Tuple[Any, ...]] = None) -> Tuple[bool, List[Dict[str, Any]], List[str], str]:
    """Execute one read-only query on VeloDB. Returns (ok, rows_as_dicts, column_names, error)."""
    conn = None
    cur = None
    try:
        clean_sql = prepare_sql_for_execution(sql)
        if not clean_sql:
            return False, [], [], "Empty SQL"
        if not READ_ONLY_SQL_RE.match(clean_sql):
            return False, [], [], "Only a single read-only SELECT/WITH query is allowed."
        conn = get_velodb_conn()
        cur = conn.cursor()
        # With params=None PyMySQL sends the SQL as-is, so LIKE '%...%' in LLM SQL is safe.
        cur.execute(clean_sql, params)
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

# Backward-compatible alias for any code that still imports the old name.
run_snowflake = run_sql

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
        sql_to_run = enforce_static_velodb_sql_contract((a_parts.get("sql") or "").strip())
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
            ok, rows, cols, err = run_sql(sql_to_run)
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

            local_repair = apply_known_velodb_compatibility_repairs(sql_to_run, err)
            if local_repair != sql_to_run:
                sql_to_run = local_repair
                dbg("LOCAL VELODB COMPATIBILITY REPAIR", sql_to_run)
                continue

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
            sql_to_run = enforce_static_velodb_sql_contract(parse_prompt_b_output(b_raw))

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
        session["active_focus"]["pending_explicit_hs_codes"] = []
        session["active_focus"]["pending_profile_product_hs_map"] = {}
        session["active_focus"]["pending_scope_resolver_trace"] = None
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
        return {"session_id": req.session_id, "state": session["state"], "messages": [msg_answer(assistant_text), msg_confirm(original_query)]}

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
# ENGINE ENTRY POINTS  (called by engine_adapter.py)
# Errors come back as {"error": "..."}.
# =========================================================
def start_session():
    sid = str(uuid.uuid4())
    state = new_session_state()
    SESSION_STORE.save(sid, state)
    return {"session_id": sid, "state": state["state"]}


def _error(message: str) -> Dict[str, str]:
    return {"error": message}


def _invalid_session():
    return _error("Invalid session_id")


def _session_busy():
    return _error("This session is still working on a previous request")


def chat(req: ChatRequest):
    """Lock the session, load it, run the turn, save it back."""
    try:
        with SESSION_STORE.lock(req.session_id):
            session = SESSION_STORE.load(req.session_id)
            if session is None:
                return _invalid_session()
            try:
                return _chat_impl(req, session)
            finally:
                SESSION_STORE.save(req.session_id, session)
    except SessionBusy:
        return _session_busy()


def _chat_impl(req: ChatRequest, session: Dict[str, Any]):
    raw_message = (req.message or "").strip()
    if not raw_message:
        return _error("Empty message")

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
        followup_explicit_hs = extract_explicit_hs_codes(q)
        active_product = session["active_focus"].get("product_name")
        followup_terms = [active_product] if active_product else []
        followup_overrides = (
            {normalize_product_key(active_product): followup_explicit_hs}
            if active_product and followup_explicit_hs else None
        )
        session["active_focus"]["pending_query_text"] = build_product_scopes_context(
            q, session, followup_terms, explicit_hs_overrides=followup_overrides
        )
        session["active_focus"]["pending_product_term"] = active_product
        session["active_focus"]["pending_product_terms"] = followup_terms
        session["active_focus"]["pending_explicit_hs_codes"] = followup_explicit_hs
        session["active_focus"]["pending_hs_code"] = (
            ",".join(followup_explicit_hs) if followup_explicit_hs and not active_product
            else session["active_focus"].get("hs_code")
        )
        session["state"] = STATE_AWAITING_CONFIRMATION
        return {"session_id": req.session_id, "state": session["state"], "messages": [msg_confirm(q)]}

    current_explicit_hs_codes = extract_explicit_hs_codes(raw_message)
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
            return _confirm_impl(req.session_id, session, "proceed")
        if low in {"no", "n", "cancel", "stop"}:
            return _confirm_impl(req.session_id, session, "cancel")

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
    # Deterministic backstop: if a remembered product is visibly named in this turn,
    # preserve it even if the Trade Agent omitted it (especially product+explicit-HS turns).
    for remembered_product in get_mentioned_memory_products(session, raw_message):
        if remembered_product not in product_terms:
            product_terms.append(remembered_product)
    product_terms = dedupe_preserve(product_terms)

    # Never let an agent rewrite silently drop CURRENT explicit HS codes. If its clean
    # final query does not preserve the full explicit set, execute/confirm the raw user
    # wording instead; the internal contract will still supply the canonical predicate.
    if current_explicit_hs_codes and final_query_text:
        final_hs = set(extract_explicit_hs_codes(final_query_text))
        if not set(current_explicit_hs_codes).issubset(final_hs):
            final_query_text = raw_message

    # Pre-Text2SQL company product-scope resolver. Run it only for a concrete DB query,
    # never for definitions/advisory/direct answers, so it does not add unnecessary latency.
    scope_resolver_result = {
        "scope_type": "GLOBAL",
        "action": "PASS_THROUGH",
        "resolved_products": [],
        "unresolved_products": [],
        "rewritten_query": final_query_text or normalized,
        "reason": "Resolver not needed for this action.",
    }
    profile_scope_overrides: Dict[str, List[str]] = {}
    if action == "CONFIRM_QUERY" and not current_explicit_hs_codes:
        scope_resolver_result = resolve_company_product_scope(
            session=session,
            original_user_query=raw_message,
            candidate_query=final_query_text or normalized,
            current_explicit_hs_codes=current_explicit_hs_codes,
        )
        profile_scope_overrides = resolver_product_hs_override_map(scope_resolver_result)
        if scope_resolver_result.get("action") == "REWRITE" and profile_scope_overrides:
            final_query_text = scope_resolver_result.get("rewritten_query") or final_query_text or normalized
            for resolved_term in resolver_product_terms(scope_resolver_result):
                if resolved_term not in product_terms:
                    product_terms.append(resolved_term)
            product_terms = dedupe_preserve(product_terms)

    messages: List[Dict[str, Any]] = [msg_answer(assistant_text)]
    append_turn(session, "assistant", assistant_text)

    followups = dedupe_preserve(followups)
    if len(followups) < 3:
        followups = safe_fallback_followups(session)
    store_followups(session, followups)

    if not hs_code and not current_explicit_hs_codes and product_terms:
        unresolved_for_domain = [
            term for term in product_terms
            if normalize_product_key(term) not in profile_scope_overrides
        ]
        if unresolved_for_domain:
            response = start_next_unknown_product_scope(
                session, req.session_id, final_query_text or normalized, unresolved_for_domain, setup_only=False
            )
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

        # CURRENT explicit HS scope has highest precedence over product_hs_memory.
        # Store it as pending until the user confirms; on Proceed it becomes the new
        # latest session scope for the product (so future product-only turns reuse it).
        explicit_scope_overrides = None
        if current_explicit_hs_codes and product_terms:
            explicit_scope_overrides = {
                normalize_product_key(term): current_explicit_hs_codes for term in product_terms
            }
        scoped_query = build_product_scopes_context(
            final_query_text,
            session,
            product_terms,
            explicit_hs_overrides=explicit_scope_overrides,
            profile_hs_overrides=profile_scope_overrides,
        )
        session["active_focus"]["pending_query_text"] = scoped_query
        session["active_focus"]["pending_product_term"] = product_terms[0] if product_terms else product_term
        session["active_focus"]["pending_product_terms"] = product_terms
        session["active_focus"]["pending_explicit_hs_codes"] = current_explicit_hs_codes
        session["active_focus"]["pending_profile_product_hs_map"] = profile_scope_overrides
        session["active_focus"]["pending_scope_resolver_trace"] = scope_resolver_result
        session["active_focus"]["pending_hs_code"] = hs_code
        session["state"] = STATE_AWAITING_CONFIRMATION
        messages.append(msg_confirm(final_query_text))
        messages.append(msg_followup(followups, title="Next best questions:"))
        return {"session_id": req.session_id, "state": session["state"], "messages": messages}

    session["state"] = STATE_READY
    store_general_trade_qa_history(session, normalized, assistant_text)
    messages.append(msg_followup(followups, title="Suggested questions:"))
    return {"session_id": req.session_id, "state": session["state"], "messages": messages}

def confirm(req: ConfirmRequest):
    return _confirm_internal(req.session_id, (req.decision or "").strip().lower())

def _confirm_internal(session_id: str, decision: str):
    """Lock the session, load it, run the confirm step, save it back."""
    try:
        with SESSION_STORE.lock(session_id):
            session = SESSION_STORE.load(session_id)
            if session is None:
                return _invalid_session()
            try:
                return _confirm_impl(session_id, session, decision)
            finally:
                SESSION_STORE.save(session_id, session)
    except SessionBusy:
        return _session_busy()


def _confirm_impl(session_id: str, session: Dict[str, Any], decision: str):
    if session["state"] != STATE_AWAITING_CONFIRMATION:
        return _error("No pending query to confirm")
    if decision not in {"proceed", "cancel"}:
        return _error("decision must be proceed|cancel")

    if decision == "cancel":
        session["active_focus"]["pending_query_text"] = None
        session["active_focus"]["pending_product_term"] = None
        session["active_focus"]["pending_product_terms"] = []
        session["active_focus"]["pending_hs_code"] = None
        session["active_focus"]["pending_explicit_hs_codes"] = []
        session["active_focus"]["pending_profile_product_hs_map"] = {}
        session["active_focus"]["pending_scope_resolver_trace"] = None
        session["state"] = STATE_IDLE
        assistant_text = "Cancelled. Ask another trade question anytime."
        append_turn(session, "assistant", assistant_text)
        followups = safe_fallback_followups(session)
        store_followups(session, followups)
        return {"session_id": session_id, "state": session["state"], "messages": [msg_answer(assistant_text), msg_followup(followups, title="Suggested questions:")]}

    final_query_text = session["active_focus"].get("pending_query_text") or ""
    pending_hs_code = session["active_focus"].get("pending_hs_code")
    pending_product_terms = session["active_focus"].get("pending_product_terms") or []
    pending_explicit_hs_codes = session["active_focus"].get("pending_explicit_hs_codes") or []
    pending_profile_product_hs_map = session["active_focus"].get("pending_profile_product_hs_map") or {}
    pending_scope_resolver_trace = session["active_focus"].get("pending_scope_resolver_trace")

    session["active_focus"]["pending_query_text"] = None
    session["active_focus"]["pending_product_term"] = None
    session["active_focus"]["pending_product_terms"] = []
    session["active_focus"]["pending_hs_code"] = None
    session["active_focus"]["pending_explicit_hs_codes"] = []
    session["active_focus"]["pending_profile_product_hs_map"] = {}
    session["active_focus"]["pending_scope_resolver_trace"] = None

    if pending_explicit_hs_codes and pending_product_terms:
        # The user explicitly changed the product's HS scope in this turn.
        # On Proceed this becomes the latest session scope (choice A).
        explicit_scopes = [{"domain": "User-explicit HS", "selected_hs_codes": pending_explicit_hs_codes}]
        for term in pending_product_terms:
            save_product_scope(session, term, explicit_scopes)
        final_query_text = build_product_scopes_context(final_query_text, session, pending_product_terms)
    elif pending_profile_product_hs_map and pending_product_terms:
        # Company-profile resolver scope is current-query only. Do NOT save it into generic
        # product_hs_memory, otherwise a later GLOBAL product query could be silently narrowed.
        final_query_text = build_product_scopes_context(
            final_query_text,
            session,
            pending_product_terms,
            profile_hs_overrides=pending_profile_product_hs_map,
        )
    elif pending_hs_code and not pending_product_terms:
        # HS-only query: keep it HS-only; do not invent/attach a product.
        session["active_focus"]["hs_code"] = pending_hs_code
        session["active_focus"]["product_name"] = None
        session["active_focus"]["selected_hs_codes"] = pending_explicit_hs_codes
    elif pending_product_terms:
        # No HS in the current turn: reuse the latest saved scope for each product.
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
        "meta": {
            "stage": result.get("stage"),
            "product_scope_resolver": pending_scope_resolver_trace,
        },
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
