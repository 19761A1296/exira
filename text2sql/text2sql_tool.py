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
VELODB_TRANSIENT_RETRY_DELAY_SECONDS = float(os.getenv("VELODB_TRANSIENT_RETRY_DELAY_SECONDS", "1.0"))

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
    "this product", "that product", "the product", "product", "products",
    "leading product", "leading export product", "top product", "top export product",
    "biggest product", "largest product", "highest value product", "selected product",
    "previous product", "product identified in q1", "product identified in q2",
    "hs", "hs code", "last month", "last year",
}

GENERIC_PRODUCT_REFERENCE_RE = re.compile(
    r"(?i)^(?:"
    r"(?:the\s+)?(?:leading|top|biggest|largest|highest(?:[- ]value)?)\s+(?:export\s+|import\s+)?product|"
    r"(?:the\s+)?(?:selected|previous|identified)\s+product|"
    r"(?:the\s+)?product\s+(?:identified|selected|found)\s+(?:in|from)\s+q\d+|"
    r"this\s+product|that\s+product|the\s+product"
    r")$"
)

PRODUCT_DISCOVERY_RE = re.compile(
    r"(?i)(?:"
    r"\bwhich\s+(?:of\s+[^?]{0,80}\s+)?products?\b|"
    r"\bwhat\s+(?:is|are)\s+[^?]{0,80}\bproducts?\b|"
    r"\b(?:leading|top|biggest|largest|highest(?:[- ]value)?)\s+(?:export\s+|import\s+)?products?\b|"
    r"\bproducts?\s+(?:do|does)\s+[^?]{0,40}\b(?:export|import|sell|buy|deal)\b|"
    r"\btop\s+\d+\s+(?:export\s+|import\s+)?products?\b"
    r")"
)

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
- HS_CODE is a clean digit-only VARCHAR in production. Preserve leading zeros and filter the physical hs_code directly; never wrap hs_code in REGEXP_REPLACE/REPLACE, SUBSTR, LEFT, or casts for filtering. Sanitize only user-provided HS text before SQL. Treat every supplied HS digit string as a hierarchical prefix by default, regardless of length: use direct hs_code LIKE '<code>%' predicates and OR them for multiple codes. Use equality only when the current user wording explicitly says exact HS / exact HS code. Never infer exactness from digit length.
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
    return not clean or clean in NON_PRODUCT_TERMS or bool(GENERIC_PRODUCT_REFERENCE_RE.fullmatch(clean))


def is_open_product_discovery_query(text: str, product_terms: Optional[List[str]] = None) -> bool:
    """True when the user is asking the database to discover/rank products.

    Open discovery must not be narrowed to the company-profile product card.  The card may
    identify the user's company, but it is not the candidate universe for questions such as
    "which is my leading export product?".  A genuinely named product term disables this
    guard so normal product scoping still applies.
    """
    concrete_terms = [
        t for t in (product_terms or [])
        if isinstance(t, str) and t.strip() and not is_non_product_term(t)
    ]
    if concrete_terms:
        return False
    q = re.sub(r"\s+", " ", (text or "").strip())
    return bool(q and PRODUCT_DISCOVERY_RE.search(q))

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

def build_canonical_hs_scope_sql(
    hs_codes: List[str],
    alias: str = "td",
    exact_hs_codes: Optional[List[str]] = None,
) -> str:
    """Build the authoritative HS predicate for clean digit-only VARCHAR hs_code.

    Default semantics are hierarchical prefix matching for every supplied HS digit string,
    including 8/10/12+ digit national extensions. Equality is used only when the CURRENT
    query explicitly says "exact HS" / "exact HS code" for that code.
    """
    codes = [c for c in [normalize_hs_token(x) for x in (hs_codes or [])] if c]
    codes = sorted(set(codes))
    if not codes:
        return "1 = 1"
    exact_set = {
        c for c in [normalize_hs_token(x) for x in (exact_hs_codes or [])] if c
    }
    parts = []
    for code in codes:
        lit = _sql_string_literal(code)
        if code in exact_set:
            parts.append(f"{alias}.hs_code = {lit}")
        else:
            parts.append(f"{alias}.hs_code LIKE CONCAT({lit}, '%')")
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
            "pending_explicit_product_hs_map": {},
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
    return p in _resolver_evidence_norm(profile_blob)


def _product_term_lookup(product_terms: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for term in product_terms or []:
        if isinstance(term, str) and term.strip() and not is_non_product_term(term):
            clean = re.sub(r"\s+", " ", term.strip())
            out[normalize_product_key(clean)] = clean
    return out


def _deterministic_explicit_product_hs_map(
    raw_query: str,
    product_terms: List[str],
    mentions: List[Dict[str, Any]],
) -> Dict[str, List[str]]:
    """Conservative backstop for obvious current-query product->HS links.

    The LLM resolver handles grammar/semantic linking for harder multi-product text. This
    deterministic layer only covers cases that are safe without interpretation:
      * exactly one product + one/more explicit HS mentions;
      * a parenthesized HS mention immediately after a detected product.
    It never assigns one query-wide HS list to every product.
    """
    lookup = _product_term_lookup(product_terms)
    if not lookup or not mentions:
        return {}
    all_codes = dedupe_preserve([c for m in mentions for c in (m.get("codes") or [])])
    if len(lookup) == 1 and all_codes:
        only_key = next(iter(lookup))
        return {only_key: all_codes}

    raw_upper = (raw_query or "").upper()
    occurrences: List[Tuple[int, int, str]] = []
    for key, original in lookup.items():
        term_upper = original.upper()
        for m in re.finditer(r"(?<![A-Z0-9])" + re.escape(term_upper) + r"(?![A-Z0-9])", raw_upper):
            occurrences.append((m.start(), m.end(), key))
    occurrences.sort()

    out: Dict[str, List[str]] = {}

    # Safe shared-scope fallback for wording such as "coffee and tea in HS 09".
    # Only apply when there is exactly one HS mention and the immediately preceding
    # comma-free phrase ends with in/under/within/using, so unrelated earlier products
    # are not swept into the same scope.
    if len(mentions) == 1 and len(lookup) > 1:
        mention = mentions[0]
        before = raw_upper[:int(mention.get("start") or 0)]
        mscope = re.search(r"(?is)([^,;:.!?]{0,240})\b(?:IN|UNDER|WITHIN|USING)\s*$", before)
        if mscope:
            scope_text = mscope.group(1)
            scoped_keys = [
                key for key, original in lookup.items()
                if re.search(r"(?<![A-Z0-9])" + re.escape(original.upper()) + r"(?![A-Z0-9])", scope_text)
            ]
            if len(scoped_keys) >= 2:
                for key in scoped_keys:
                    out[key] = list(mention.get("codes") or [])
                return out

    for mention in mentions:
        m_start = int(mention.get("start") or 0)
        # Parenthetical form: PRODUCT (HS ...). Find the nearest opening parenthesis
        # before HS and ensure no ')' closes it first.
        open_paren = raw_upper.rfind("(", 0, m_start)
        close_paren = raw_upper.rfind(")", 0, m_start)
        if open_paren < 0 or open_paren < close_paren:
            continue
        candidates = [o for o in occurrences if o[1] <= open_paren]
        if not candidates:
            continue
        nearest = max(candidates, key=lambda x: x[1])
        between = raw_upper[nearest[1]:open_paren]
        if len(between) > 12 or re.search(r"[;,]", between):
            continue
        key = nearest[2]
        out.setdefault(key, [])
        for code in mention.get("codes") or []:
            if code not in out[key]:
                out[key].append(code)
    return out


def _sanitize_scope_resolver_output(
    data: Dict[str, Any],
    profile: Any,
    product_terms: List[str],
    explicit_mentions: List[Dict[str, Any]],
    prelinked_explicit: Dict[str, List[str]],
    original_user_query: str = "",
) -> Dict[str, Any]:
    """Validate resolver mappings against current-query HS evidence or company profile."""
    profile_blob = _resolver_profile_blob(profile)
    scope_type = str(data.get("scope_type") or "GLOBAL").upper().strip()
    if scope_type not in {"GLOBAL", "USER_COMPANY", "OTHER_COMPANY"}:
        scope_type = "GLOBAL"

    term_lookup = _product_term_lookup(product_terms)
    query_norm = _resolver_evidence_norm(original_user_query)
    portfolio_query = bool(re.search(
        r"(?i)\b(?:all|portfolio|products?)\b.*\b(?:my|our|we|deal|sell|export|import|buy)\b|\bproducts?\s+(?:i|we)\s+deal\s+in\b",
        original_user_query or "",
    ))
    explicit_codes = {
        c for m in explicit_mentions for c in (m.get("codes") or []) if normalize_hs_token(c)
    }
    explicit_codes = {normalize_hs_token(c) for c in explicit_codes if normalize_hs_token(c)}

    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def add_mapping(user_product: str, source: str, codes: List[str], profile_product: str = ""):
        if is_non_product_term(user_product):
            return
        key = normalize_product_key(user_product)
        if source == "CURRENT_USER_EXPLICIT_HS":
            # Prefer Trade-Agent product_terms when available. For external executors
            # (Thinking) product_terms may be empty, so allow a literal product phrase
            # only when it is visibly present in the current query.
            if key in term_lookup:
                user_product = term_lookup[key]
            elif not _resolver_evidence_norm(user_product) or _resolver_evidence_norm(user_product) not in query_norm:
                return
            valid_codes = [c for c in codes if c in explicit_codes]
            if not valid_codes:
                return
            profile_product = ""
        elif source == "COMPANY_PROFILE_RESOLVER":
            if scope_type != "USER_COMPANY" or profile in (None, "", {}, []):
                return
            if not profile_product or not _profile_contains_product(profile_blob, profile_product):
                return
            valid_codes = [c for c in codes if _profile_contains_hs(profile_blob, c)]
            if not valid_codes:
                return
            # For named-product queries require the user product to be one of the detected
            # products. Portfolio/all-product queries may legitimately introduce profile
            # product names even when product_terms was empty.
            if term_lookup and key not in term_lookup:
                return
            if key in term_lookup:
                user_product = term_lookup[key]
            elif not portfolio_query:
                # External Thinking path may not provide product_terms. In that case
                # require the resolver's user_product wording to actually appear in the query.
                if not _resolver_evidence_norm(user_product) or _resolver_evidence_norm(user_product) not in query_norm:
                    return
        else:
            return

        bucket_key = (normalize_product_key(user_product), source)
        bucket = merged.setdefault(bucket_key, {
            "user_product": user_product,
            "source": source,
            "profile_product": profile_product,
            "hs_codes": [],
            "confidence": "HIGH",
        })
        if profile_product and not bucket.get("profile_product"):
            bucket["profile_product"] = profile_product
        for code in valid_codes:
            if code not in bucket["hs_codes"]:
                bucket["hs_codes"].append(code)

    # Preserve deterministic explicit links first. The model may add harder grammar links,
    # but it cannot erase a safe explicit mapping already established from the query syntax.
    for key, codes in (prelinked_explicit or {}).items():
        if key in term_lookup:
            add_mapping(term_lookup[key], "CURRENT_USER_EXPLICIT_HS", codes)

    for item in data.get("resolved_products") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("confidence") or "").upper().strip() != "HIGH":
            continue
        user_product = re.sub(r"\s+", " ", str(item.get("user_product") or "").strip())
        source = str(item.get("source") or "").upper().strip()
        profile_product = re.sub(r"\s+", " ", str(item.get("profile_product") or "").strip())
        codes = [c for c in [normalize_hs_token(str(x)) for x in (item.get("hs_codes") or [])] if c]
        if user_product and codes:
            add_mapping(user_product, source, codes, profile_product)

    resolved = list(merged.values())
    explicit_product_keys = {
        normalize_product_key(x["user_product"])
        for x in resolved
        if x.get("source") == "CURRENT_USER_EXPLICIT_HS"
    }
    resolved = [
        x for x in resolved
        if not (
            x.get("source") == "COMPANY_PROFILE_RESOLVER"
            and normalize_product_key(x["user_product"]) in explicit_product_keys
        )
    ]
    resolved_keys = {normalize_product_key(x["user_product"]) for x in resolved}
    unresolved = []
    for term in product_terms or []:
        if not isinstance(term, str) or not term.strip() or is_non_product_term(term):
            continue
        clean = re.sub(r"\s+", " ", term.strip())
        if normalize_product_key(clean) not in resolved_keys:
            unresolved.append(clean)
    unresolved = dedupe_preserve(unresolved)

    used_explicit = {
        c for x in resolved if x.get("source") == "CURRENT_USER_EXPLICIT_HS" for c in x.get("hs_codes", [])
    }
    model_unscoped = [normalize_hs_token(str(x)) for x in (data.get("unscoped_explicit_hs_codes") or [])]
    unscoped = [c for c in model_unscoped if c and c in explicit_codes and c not in used_explicit]
    for code in sorted(explicit_codes):
        if code not in used_explicit and code not in unscoped:
            unscoped.append(code)

    return {
        "scope_type": scope_type,
        "resolved_products": resolved,
        "unresolved_products": unresolved,
        "unscoped_explicit_hs_codes": unscoped,
        "reason": str(data.get("reason") or "").strip(),
    }


def build_product_scope_resolver_prompt(
    session: Dict[str, Any],
    original_user_query: str,
    candidate_query: str,
    product_terms: List[str],
    explicit_mentions: List[Dict[str, Any]],
    prelinked_explicit: Dict[str, List[str]],
) -> str:
    payload = {
        "original_user_query": original_user_query,
        "candidate_query": candidate_query,
        "product_terms": product_terms or [],
        "company_profile": get_company_product_profile(session),
        "explicit_hs_mentions": [
            {
                "text": m.get("text", ""),
                "codes": m.get("codes", []),
                "exact_codes": m.get("exact_codes", []),
            }
            for m in explicit_mentions
        ],
        "prelinked_explicit_product_hs": {
            _product_term_lookup(product_terms).get(k, k): v
            for k, v in (prelinked_explicit or {}).items()
        },
    }
    return PRODUCT_SCOPE_RESOLVER_PROMPT_TEMPLATE.replace(
        "{PRODUCT_SCOPE_RESOLVER_INPUT_JSON}",
        json.dumps(payload, ensure_ascii=False, default=str),
    )


def resolve_product_scope(
    session: Dict[str, Any],
    original_user_query: str,
    candidate_query: str,
    product_terms: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Resolve current explicit product->HS links plus company-profile fallback.

    The resolver is deliberately per-product. One explicit HS elsewhere in the message does
    not disable profile/session/picker resolution for other products.
    """
    candidate_query = (candidate_query or original_user_query or "").strip()
    product_terms = dedupe_preserve([
        re.sub(r"\s+", " ", x.strip()) for x in (product_terms or [])
        if isinstance(x, str) and x.strip() and not is_non_product_term(x)
    ])
    mentions = extract_explicit_hs_mentions(original_user_query)
    prelinked = _deterministic_explicit_product_hs_map(original_user_query, product_terms, mentions)
    profile = get_company_product_profile(session)

    default = {
        "scope_type": "GLOBAL",
        "resolved_products": [],
        "unresolved_products": list(product_terms),
        "unscoped_explicit_hs_codes": extract_explicit_hs_codes(original_user_query),
        "reason": "Resolver not applied.",
    }
    if not candidate_query:
        return default

    # An explicit HS with no concrete product term is already sufficient scope.  Do not
    # ask the model to manufacture a pseudo-product such as "leading export product" and
    # turn that reference into PRODUCT_DESCRIPTION matching.
    if mentions and not product_terms:
        default["reason"] = "Explicit HS scope with no concrete product term: keep the query HS-only."
        dbg("PRODUCT SCOPE RESOLVER BYPASS", default)
        return default

    # Product-discovery/ranking queries deliberately have no product filter yet.  The
    # company profile is useful for identifying the user's company, but must never become
    # an implicit candidate universe for "my leading/top product".  Let Prompt A discover
    # the product from shipment records instead.
    if is_open_product_discovery_query(original_user_query, product_terms) or \
            is_open_product_discovery_query(candidate_query, product_terms):
        default["reason"] = "Open product-discovery query: resolver bypassed so the database discovers/ranks products."
        dbg("PRODUCT SCOPE RESOLVER BYPASS", default)
        return default

    # Fast path: one product with explicit HS can be linked deterministically without an LLM.
    if len(product_terms) == 1 and mentions:
        return _sanitize_scope_resolver_output(
            {"scope_type": "GLOBAL", "resolved_products": [], "reason": "Deterministic single-product explicit HS mapping."},
            profile, product_terms, mentions, prelinked, original_user_query,
        )

    # No explicit association work and no company profile means there is nothing for this
    # resolver to add; normal session memory / HS picker remains authoritative downstream.
    if not mentions and profile in (None, "", {}, []):
        return default

    prompt = build_product_scope_resolver_prompt(
        session, original_user_query, candidate_query, product_terms, mentions, prelinked
    )
    try:
        raw = call_openrouter_model(
            prompt,
            max_tokens=PRODUCT_SCOPE_RESOLVER_MAX_TOKENS,
            model=PRODUCT_SCOPE_RESOLVER_MODEL,
            temperature=0.0,
        )
        parsed = parse_json_strict(raw)
        result = _sanitize_scope_resolver_output(parsed, profile, product_terms, mentions, prelinked, original_user_query)
        dbg("PRODUCT SCOPE RESOLVER", {"model": PRODUCT_SCOPE_RESOLVER_MODEL, "result": result})
        return result
    except Exception as exc:
        default = _sanitize_scope_resolver_output(
            {"scope_type": "GLOBAL", "resolved_products": [], "reason": f"Resolver failed closed: {exc}"},
            profile, product_terms, mentions, prelinked, original_user_query,
        )
        dbg("PRODUCT SCOPE RESOLVER FAILED", str(exc))
        return default


def prepare_external_query_product_scope(session_id: str, query: str) -> Dict[str, Any]:
    """Prepare the same product/HS contract for executors that bypass normal chat().

    Used by the Thinking route. It never mutates session memory and never launches the HS
    picker. Safe explicit/profile/session mappings are attached; unresolved product wording
    remains in the query for the downstream executor rather than being cross-mapped.
    """
    raw_query = (query or "").strip()
    out = {"query": raw_query, "resolver": None, "explicit_map": {}, "profile_map": {}, "product_terms": []}
    if not raw_query or not session_id:
        return out
    try:
        session = SESSION_STORE.load(session_id)
    except Exception:
        session = None
    if not isinstance(session, dict):
        return out

    # Let the resolver identify products directly from the current query when this path has
    # no Trade Agent product_terms. Also preserve any session product visibly mentioned.
    result = resolve_product_scope(session, raw_query, raw_query, product_terms=[])
    explicit_map, profile_map = resolver_product_hs_maps(result)
    terms = resolver_product_terms(result)
    for remembered in get_mentioned_memory_products(session, raw_query):
        if remembered not in terms:
            terms.append(remembered)
    terms = dedupe_preserve(terms)
    scoped = build_product_scopes_context(
        raw_query,
        session,
        terms,
        explicit_hs_overrides=explicit_map,
        profile_hs_overrides=profile_map,
    )
    out.update({
        "query": scoped,
        "resolver": result,
        "explicit_map": explicit_map,
        "profile_map": profile_map,
        "product_terms": terms,
    })
    return out


def resolver_product_hs_maps(resolver_result: Dict[str, Any]) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    explicit_map: Dict[str, List[str]] = {}
    profile_map: Dict[str, List[str]] = {}
    for item in resolver_result.get("resolved_products") or []:
        if not isinstance(item, dict):
            continue
        product = re.sub(r"\s+", " ", str(item.get("user_product") or "").strip())
        codes = [c for c in [normalize_hs_token(x) for x in item.get("hs_codes") or []] if c]
        if not product or not codes:
            continue
        target = explicit_map if item.get("source") == "CURRENT_USER_EXPLICIT_HS" else profile_map
        key = normalize_product_key(product)
        target.setdefault(key, [])
        for code in codes:
            if code not in target[key]:
                target[key].append(code)
    return explicit_map, profile_map


def resolver_product_terms(resolver_result: Dict[str, Any]) -> List[str]:
    return dedupe_preserve([
        str(item.get("user_product") or "").strip()
        for item in resolver_result.get("resolved_products") or []
        if isinstance(item, dict) and str(item.get("user_product") or "").strip()
    ])

# =========================================================
# PRODUCT / HS SETUP
# =========================================================
def normalize_hs_token(code: str) -> Optional[str]:
    """Normalize user/profile HS text to a digit string without truncation.

    EX-IRA sees national tariff extensions longer than 10 digits, so support up to 18 digits.
    Leading zeros are preserved. One/two-digit chapter inputs are zero-padded to two digits.
    """
    raw = re.sub(r"[^0-9]", "", (code or "").strip())
    if not re.fullmatch(r"\d{1,18}", raw):
        return None
    if len(raw) <= 2:
        return raw.zfill(2)
    return raw


def _expand_hs_fragment(fragment: str) -> List[str]:
    """Parse the numeric fragment after an explicit HS marker.

    Supports lists, 2-digit chapter ranges, dotted codes, and spaced code groups.
    Examples:
      50, 51 and 67 -> 50,51,67
      50-63 -> 50..63
      0803.90.11 -> 08039011
      6306 22 00 -> 63062200
    """
    frag = (fragment or "").strip().strip(",;/& ")
    if not frag:
        return []
    frag = re.sub(r"(?i)\band\b", ",", frag)
    # Slash and ampersand are list separators. Commas are also list separators.
    parts = [x.strip() for x in re.split(r"\s*(?:,|/|&)\s*", frag) if x.strip()]
    out: List[str] = []
    for part in parts:
        # Expand only safe HS chapter ranges. Longer hyphenated forms are treated as
        # formatting and normalized by stripping punctuation rather than expanded.
        m = re.fullmatch(r"\s*(\d{1,2})\s*(?:-|–|—|(?i:to))\s*(\d{1,2})\s*", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if 0 <= a <= 99 and 0 <= b <= 99 and a <= b:
                for n in range(a, b + 1):
                    code = normalize_hs_token(str(n))
                    if code and code not in out:
                        out.append(code)
                continue
        code = normalize_hs_token(part)
        if code and code not in out:
            out.append(code)
    return out


def extract_explicit_hs_mentions(text: str) -> List[Dict[str, Any]]:
    """Return explicit HS mentions with spans/codes; ignore ordinary unmarked numbers.

    A6 remains conservative: numbers inside a general sentence are not treated as HS unless
    introduced by HS/HS code/chapter/heading/prefix wording. A message made only of digits
    still works for the existing HS-only setup flow.
    """
    raw = text or ""
    if not raw.strip():
        return []

    marker_re = re.compile(
        r"(?i)\b(?P<exact>exact\s+)?hs(?:\s*(?:codes?|chapters?|headings?|prefix(?:es)?))?\s*[-:]?\s*"
    )
    mentions: List[Dict[str, Any]] = []
    for marker in marker_re.finditer(raw):
        tail = raw[marker.end():]
        # Consume only the numeric/list/range formatting immediately following the marker.
        # Stop as soon as non-HS prose begins (except connector words 'and'/'to').
        seg_match = re.match(
            r"(?i)([0-9][0-9\s\.,/&\-–—]*(?:(?:\band\b|\bto\b)[0-9\s\.,/&\-–—]*)*)",
            tail,
        )
        if not seg_match:
            continue
        fragment = seg_match.group(1).rstrip()
        codes = _expand_hs_fragment(fragment)
        if not codes:
            continue
        end = marker.end() + seg_match.end(1)
        exact_codes = list(codes) if marker.group("exact") else []
        mentions.append({
            "start": marker.start(),
            "end": end,
            "text": raw[marker.start():end],
            "codes": codes,
            "exact_codes": exact_codes,
        })

    if not mentions and re.fullmatch(r"\s*\d{2,18}\s*", raw):
        code = normalize_hs_token(raw)
        if code:
            mentions.append({
                "start": 0,
                "end": len(raw),
                "text": raw.strip(),
                "codes": [code],
                "exact_codes": [],
            })
    return mentions


def extract_explicit_hs_codes(text: str) -> List[str]:
    found: List[str] = []
    for mention in extract_explicit_hs_mentions(text):
        for code in mention.get("codes") or []:
            if code not in found:
                found.append(code)
    return found


def extract_exact_hs_codes(text: str) -> List[str]:
    found: List[str] = []
    for mention in extract_explicit_hs_mentions(text):
        for code in mention.get("exact_codes") or []:
            if code not in found:
                found.append(code)
    return found


def extract_explicit_hs_code(text: str) -> Optional[str]:
    codes = extract_explicit_hs_codes(text)
    return codes[0] if codes else None


def extract_product_or_hs(text: str) -> Dict[str, Optional[str]]:
    codes = extract_explicit_hs_codes(text)
    if codes:
        return {"hs_code": codes[0], "product": None}
    return {"hs_code": None, "product": (text or "").strip()}

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

def full_domain_hs_codes(domain: str) -> List[str]:
    """Return the full authoritative HS2 chapter set for a named domain."""
    return sorted([hs2 for hs2, name in HS2_DOMAIN_MAP.items() if name == domain])

def format_hs_chapters_compact(codes: List[str]) -> str:
    vals = sorted({int(c) for c in codes or [] if re.fullmatch(r"\d{2}", str(c))})
    if not vals:
        return ", ".join(codes or [])
    chunks = []
    i = 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[j + 1] == vals[j] + 1:
            j += 1
        if j - i >= 2:
            chunks.append(f"{vals[i]:02d}-{vals[j]:02d}")
        else:
            chunks.extend(f"{vals[k]:02d}" for k in range(i, j + 1))
        i = j + 1
    return ", ".join(chunks)

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
        opt["full_domain_hs_codes"] = full_domain_hs_codes(opt["domain"]) or list(opt["hs_codes"])
        opt["label"] = f"Use {opt['domain']} (full domain HS {format_hs_chapters_compact(opt['full_domain_hs_codes'])})"
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
            return [{"domain": opt.get("domain"), "selected_hs_codes": opt.get("full_domain_hs_codes") or full_domain_hs_codes(opt.get("domain")) or opt.get("hs_codes", [])}]

    if re.fullmatch(r"\s*\d+(\s*,\s*\d+)*\s*", raw):
        scopes = []
        for n in [int(x) for x in re.findall(r"\d+", raw)]:
            if 1 <= n <= len(options):
                opt = options[n - 1]
                scopes.append({"domain": opt.get("domain"), "selected_hs_codes": opt.get("full_domain_hs_codes") or full_domain_hs_codes(opt.get("domain")) or opt.get("hs_codes", [])})
        if scopes:
            return scopes

    # Explicit HS text entered while the picker is open uses the same production parser
    # as normal queries, so ranges/dotted/spaced/12+ digit codes behave consistently.
    codes = extract_explicit_hs_codes(raw)
    if not codes and re.fullmatch(r"\s*\d{1,18}(\s*,\s*\d{1,18})+\s*", raw):
        codes = [normalize_hs_token(x) for x in re.findall(r"\d{1,18}", raw)]
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
    current_exact_hs_codes = set(extract_exact_hs_codes(base_query))
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
        hs_sql = build_canonical_hs_scope_sql(
            hs_codes, alias="td", exact_hs_codes=[c for c in hs_codes if c in current_exact_hs_codes]
        )
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
    return bool(re.match(r"(?i)^use .+\([^)]*HS [0-9,\s\-–—]+\)$", (text or "").strip()))

def build_domain_clarification_text(product_term: str, options: List[Dict[str, Any]]) -> str:
    lines = [
        f"“{product_term}” appears under multiple HS industries in the last {HS_PROBE_MONTHS} completed months.",
        "Please select one or more domains, or type specific HS prefixes like `HS 17, 18`.",
        "If you select only a domain, all HS prefixes under that domain will be used.",
        "",
    ]
    for idx, opt in enumerate(options, start=1):
        detected = format_hs_chapters_compact(opt.get("hs_codes") or [])
        full_scope = format_hs_chapters_compact(opt.get("full_domain_hs_codes") or opt.get("hs_codes") or [])
        lines.append(
            f"{idx}. {opt['domain']} — detected HS {detected}; selecting this uses full domain HS {full_scope} "
            f"({opt['record_count']} matching rows in the probe window)"
        )
    return "\n".join(lines)

def build_domain_clarification_response(
    session: Dict[str, Any],
    session_id: str,
    product_term: str,
    original_query: str,
    options: List[Dict[str, Any]],
    setup_only: bool = False,
    product_terms: Optional[List[str]] = None,
    explicit_hs_overrides: Optional[Dict[str, List[str]]] = None,
    profile_hs_overrides: Optional[Dict[str, List[str]]] = None,
    scope_resolver_trace: Optional[Dict[str, Any]] = None,
):
    session["active_focus"]["pending_domain_disambiguation"] = {
        "original_query": original_query,
        "resume_query": original_query,
        "product_term": product_term,
        "options": options,
        "setup_only": setup_only,
        "product_terms": product_terms or [product_term],
        "explicit_hs_overrides": explicit_hs_overrides or {},
        "profile_hs_overrides": profile_hs_overrides or {},
        "scope_resolver_trace": scope_resolver_trace,
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


def start_next_unknown_product_scope(
    session: Dict[str, Any],
    session_id: str,
    original_query: str,
    product_terms: List[str],
    setup_only: bool = False,
    explicit_hs_overrides: Optional[Dict[str, List[str]]] = None,
    profile_hs_overrides: Optional[Dict[str, List[str]]] = None,
    scope_resolver_trace: Optional[Dict[str, Any]] = None,
):
    explicit_map = explicit_hs_overrides or {}
    profile_map = profile_hs_overrides or {}
    candidates = [
        term for term in product_terms or []
        if normalize_product_key(term) not in explicit_map
        and normalize_product_key(term) not in profile_map
    ]
    unknown_terms = get_unknown_product_terms(session, candidates)
    if not unknown_terms:
        return None
    term_to_probe = unknown_terms[0]
    hs2_rows = run_hs2_probe_sql(term_to_probe)
    options = build_domain_options(hs2_rows)
    if len(options) > 1:
        return build_domain_clarification_response(
            session, session_id, term_to_probe, original_query, options, setup_only,
            product_terms, explicit_map, profile_map, scope_resolver_trace,
        )
    if len(options) == 1:
        save_product_scope(
            session,
            term_to_probe,
            [{
                "domain": options[0]["domain"],
                "selected_hs_codes": options[0].get("full_domain_hs_codes")
                    or full_domain_hs_codes(options[0]["domain"])
                    or options[0]["hs_codes"],
            }],
        )
        return start_next_unknown_product_scope(
            session, session_id, original_query, product_terms, setup_only,
            explicit_map, profile_map, scope_resolver_trace,
        )
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



def _rewrite_literal_hs_exactness(sql: str, exact_codes: Optional[List[str]] = None) -> str:
    """Enforce prefix-by-default semantics for literal hs_code equality/IN filters."""
    exact_set = {c for c in [normalize_hs_token(x) for x in (exact_codes or [])] if c}
    s = sql or ""

    eq_re = re.compile(r"(?i)(?P<col>(?:[A-Za-z_][A-Za-z0-9_]*\.)?hs_code)\s*=\s*'(?P<code>\d{1,18})'")
    def repl_eq(m: re.Match) -> str:
        code = normalize_hs_token(m.group("code"))
        if not code or code in exact_set:
            return m.group(0)
        return f"{m.group('col')} LIKE CONCAT('{code}', '%')"
    s = eq_re.sub(repl_eq, s)

    in_re = re.compile(r"(?is)(?P<col>(?:[A-Za-z_][A-Za-z0-9_]*\.)?hs_code)\s+IN\s*\((?P<items>[^)]*)\)")
    def repl_in(m: re.Match) -> str:
        quoted = re.findall(r"'([0-9]{1,18})'", m.group("items"))
        # Only rewrite when the IN list is a pure quoted-digit HS list.
        residue = re.sub(r"'([0-9]{1,18})'", "", m.group("items"))
        if not quoted or re.sub(r"[\s,]", "", residue):
            return m.group(0)
        parts = []
        for raw in quoted:
            code = normalize_hs_token(raw)
            if not code:
                return m.group(0)
            if code in exact_set:
                parts.append(f"{m.group('col')} = '{code}'")
            else:
                parts.append(f"{m.group('col')} LIKE CONCAT('{code}', '%')")
        return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"
    return in_re.sub(repl_in, s)

def enforce_static_velodb_sql_contract(sql: str, user_query: str = "") -> str:
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

    # Prefix is the default even for long national tariff codes; equality survives only
    # when the CURRENT user explicitly requested exact HS semantics.
    s = _rewrite_literal_hs_exactness(s, extract_exact_hs_codes(user_query))

    return s


def apply_known_velodb_compatibility_repairs(sql: str, error: str, user_query: str = "") -> str:
    current = enforce_static_velodb_sql_contract(sql or "", user_query)
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
    return enforce_static_velodb_sql_contract(current, user_query)


_TRANSIENT_VELODB_ERROR_MARKERS = (
    "compute_groups_no_alive_be",
    "no alive be",
    "backend nodes",
    "backend node",
    "compute group",
    "connection reset",
    "connection refused",
    "server has gone away",
    "lost connection",
    "read timed out",
    "connect timeout",
    "timed out",
    "temporarily unavailable",
    "too many connections",
)


def is_transient_velodb_error(error: str) -> bool:
    """Return True for infrastructure/availability failures that SQL rewriting cannot fix."""
    low = (error or "").lower()
    return bool(low and any(marker in low for marker in _TRANSIENT_VELODB_ERROR_MARKERS))

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
        sql_to_run = enforce_static_velodb_sql_contract((a_parts.get("sql") or "").strip(), final_query_text)
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

            # Availability/network failures are not SQL problems.  Prompt B must never
            # rewrite a valid query because the compute group happened to be unavailable.
            # Retry the exact same SQL; if all attempts fail, surface a service-availability
            # failure so callers can stop dependent TRADE nodes rather than changing source.
            if is_transient_velodb_error(err):
                if attempt_idx == MAX_SQL_FIX_ATTEMPTS:
                    return {
                        "stage": "sql_infrastructure_failure",
                        "narrative": "The trade-data service is temporarily unavailable, so this analysis could not be completed from trade records.",
                        "visualization": default_visualization([], []),
                        "sql": sql_to_run,
                        "rows": [],
                        "cols": [],
                        "prompt_a_raw": a_raw,
                        "prompt_a_parts": a_parts,
                        "prompt_c_raw": "",
                        "error": err,
                    }
                if VELODB_TRANSIENT_RETRY_DELAY_SECONDS > 0:
                    # Back off between retries so a briefly unhealthy compute group has
                    # time to recover.  The SQL text itself is never changed for an
                    # infrastructure failure.
                    delay = VELODB_TRANSIENT_RETRY_DELAY_SECONDS * (2 ** (attempt_idx - 1))
                    time.sleep(delay)
                continue

            local_repair = apply_known_velodb_compatibility_repairs(sql_to_run, err, final_query_text)
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
            sql_to_run = enforce_static_velodb_sql_contract(parse_prompt_b_output(b_raw), final_query_text)

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
        explicit_map = pending_domain.get("explicit_hs_overrides") or {}
        profile_map = pending_domain.get("profile_hs_overrides") or {}
        resolver_trace = pending_domain.get("scope_resolver_trace")

        save_product_scope(session, product_term, selected_scopes)
        session["active_focus"]["pending_domain_disambiguation"] = None
        append_turn(session, "user", raw_message)

        selected_hs = flatten_scope_hs_codes(selected_scopes)
        assistant_text = f"Got it. I’ll use HS {', '.join(selected_hs)} for “{product_term}”."
        append_turn(session, "assistant", assistant_text)

        next_response = start_next_unknown_product_scope(
            session,
            req.session_id,
            original_query,
            product_terms,
            setup_only=bool(pending_domain.get("setup_only")),
            explicit_hs_overrides=explicit_map,
            profile_hs_overrides=profile_map,
            scope_resolver_trace=resolver_trace,
        )
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
            return {
                "session_id": req.session_id,
                "state": session["state"],
                "messages": [msg_answer(assistant_text), msg_followup(followups, title="Here are 3 questions you can run:")],
            }

        scoped_query = build_product_scopes_context(
            original_query,
            session,
            product_terms,
            explicit_hs_overrides=explicit_map,
            profile_hs_overrides=profile_map,
        )
        session["active_focus"]["pending_query_text"] = scoped_query
        session["active_focus"]["pending_product_terms"] = product_terms
        session["active_focus"]["pending_explicit_hs_codes"] = dedupe_preserve([
            c for codes in explicit_map.values() for c in codes
        ])
        session["active_focus"]["pending_explicit_product_hs_map"] = explicit_map
        session["active_focus"]["pending_profile_product_hs_map"] = profile_map
        session["active_focus"]["pending_scope_resolver_trace"] = resolver_trace
        session["state"] = STATE_AWAITING_CONFIRMATION
        return {
            "session_id": req.session_id,
            "state": session["state"],
            "messages": [msg_answer(assistant_text), msg_confirm(original_query)],
        }

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
        session["active_focus"]["pending_explicit_product_hs_map"] = followup_overrides or {}
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

    # Product Scope Resolver: resolve CURRENT explicit product->HS links per product,
    # then (USER_COMPANY only) fill still-unresolved products from company-profile evidence.
    # One explicit HS elsewhere in a long query must never disable resolution for other products.
    scope_resolver_result = {
        "scope_type": "GLOBAL",
        "resolved_products": [],
        "unresolved_products": list(product_terms),
        "unscoped_explicit_hs_codes": list(current_explicit_hs_codes),
        "reason": "Resolver not needed for this action.",
    }
    explicit_scope_overrides: Dict[str, List[str]] = {}
    profile_scope_overrides: Dict[str, List[str]] = {}
    if action == "CONFIRM_QUERY":
        scope_resolver_result = resolve_product_scope(
            session=session,
            original_user_query=raw_message,
            candidate_query=final_query_text or normalized,
            product_terms=product_terms,
        )
        explicit_scope_overrides, profile_scope_overrides = resolver_product_hs_maps(scope_resolver_result)
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

    if product_terms:
        unresolved_for_domain = [
            term for term in product_terms
            if normalize_product_key(term) not in explicit_scope_overrides
            and normalize_product_key(term) not in profile_scope_overrides
        ]
        if unresolved_for_domain:
            response = start_next_unknown_product_scope(
                session,
                req.session_id,
                final_query_text or normalized,
                unresolved_for_domain,
                setup_only=False,
                explicit_hs_overrides=explicit_scope_overrides,
                profile_hs_overrides=profile_scope_overrides,
                scope_resolver_trace=scope_resolver_result,
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

        # Preserve product-specific current explicit mappings. Unscoped explicit HS is kept
        # separately for HS-only queries; it is never copied onto unrelated products.
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
        session["active_focus"]["pending_explicit_product_hs_map"] = explicit_scope_overrides
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
        session["active_focus"]["pending_explicit_product_hs_map"] = {}
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
    pending_explicit_product_hs_map = session["active_focus"].get("pending_explicit_product_hs_map") or {}
    pending_profile_product_hs_map = session["active_focus"].get("pending_profile_product_hs_map") or {}
    pending_scope_resolver_trace = session["active_focus"].get("pending_scope_resolver_trace")

    session["active_focus"]["pending_query_text"] = None
    session["active_focus"]["pending_product_term"] = None
    session["active_focus"]["pending_product_terms"] = []
    session["active_focus"]["pending_hs_code"] = None
    session["active_focus"]["pending_explicit_hs_codes"] = []
    session["active_focus"]["pending_explicit_product_hs_map"] = {}
    session["active_focus"]["pending_profile_product_hs_map"] = {}
    session["active_focus"]["pending_scope_resolver_trace"] = None

    if pending_explicit_product_hs_map and pending_product_terms:
        # Persist ONLY each product's own explicit HS scope. This fixes the historical bug
        # where the merged query-wide HS list was saved against every product.
        term_lookup = {normalize_product_key(t): t for t in pending_product_terms}
        for key, codes in pending_explicit_product_hs_map.items():
            term = term_lookup.get(key)
            clean_codes = [c for c in [normalize_hs_token(x) for x in codes] if c]
            if term and clean_codes:
                save_product_scope(
                    session,
                    term,
                    [{"domain": "User-explicit HS", "selected_hs_codes": sorted(set(clean_codes))}],
                )
        final_query_text = build_product_scopes_context(
            final_query_text,
            session,
            pending_product_terms,
            explicit_hs_overrides=pending_explicit_product_hs_map,
            profile_hs_overrides=pending_profile_product_hs_map,
        )
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
        # No current explicit/profile HS for these products: reuse saved per-product scope.
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
