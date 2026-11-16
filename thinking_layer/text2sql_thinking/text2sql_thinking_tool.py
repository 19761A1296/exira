# text2sql_thinking/text2sql_thinking_tool.py
"""
text2sql_thinking: engine-free text2sql.

Same pipeline as text2sql's execute_prompt_pipeline, but with its own prompts
that have no analytical engines and no data coverage:

    question
       │
       ▼
    Normalize            →  spelling / whitespace / country-abbreviation cleanup
       │
       ▼
    Prompt A (thinking)  →  Interpretation Summary, SQL Implementation Plan, SQL
       │
       ▼
    VeloDB     ──error──►  Prompt B (thinking) rewrites the SQL,
       │                   up to MAX_SQL_FIX_ATTEMPTS attempts in total
       ▼
    Prompt C (thinking)  →  narrative + visualization spec
       │
       ▼
    result dict

Database: VeloDB (Apache Doris engine), reached over the MySQL protocol via PyMySQL
(pip install pymysql). Prompts A and B get a VeloDB/Doris SQL dialect block injected
so the model writes Doris SQL, not Snowflake SQL.

Fully standalone: imports nothing from text2sql/. It has no sessions, no
Trade Agent, no HS picker, no confirmation, no follow-ups.
The question must already be complete (product / HS code, geography, period
written into it).

PROMPT_A_SKELETON_BIBLE_THINKING (exported from this package) is the engine-free
capability map. run() does not use it: in text2sql it is only read by the Trade
Agent and Follow-up Agent. It is here for the thinking layer, to know what this
text2sql can answer.

    from thinking_layer.text2sql_thinking import run
    out = run("Share of the top 5 buyers in total trade value for HS 0901 "
              "imports into Germany in the last 12 months")

Result dict (same keys as text2sql's execute_prompt_pipeline, plus "attempts"
and "seconds"):

    stage           success | prompt_a_no_sql | sql_execution_failure | pipeline_exception
    question        the question as given
    normalized_query  the question after Normalize (what Prompts A/B/C received)
    narrative       Prompt C answer text
    visualization   chart / table spec for the UI
    sql             the SQL that ran (or the last one tried)
    rows, cols      result set (full)
    attempts        [{"sql", "error"}] for every failed execution
    prompt_a_raw, prompt_a_parts, prompt_c_raw, error, seconds

.env — same keys as text2sql, so one .env serves both:
    OPENROUTER_API_KEY, GEMINI_MODEL
    VELODB_HOST, VELODB_PORT (MySQL/query port, usually 9030), VELODB_USER,
    VELODB_PASSWORD, VELODB_DATABASE, VELODB_TRADE_TABLE_NAME
    NORMALIZE_MAX_TOKENS, PROMPT_A_MAX_TOKENS, PROMPT_B_MAX_TOKENS, PROMPT_C_MAX_TOKENS,
    MAX_SQL_FIX_ATTEMPTS
    DEBUG_TRADE
  Optional:
    VELODB_CONNECT_TIMEOUT (default 10), VELODB_READ_TIMEOUT (default 300)
  Optional overrides that apply to this module only:
    THINKING_NORMALIZE_MAX_TOKENS, THINKING_PROMPT_A_MAX_TOKENS, THINKING_PROMPT_B_MAX_TOKENS,
    THINKING_PROMPT_C_MAX_TOKENS, THINKING_MAX_SQL_FIX_ATTEMPTS, DEBUG_TRADE_THINKING
"""

import datetime
import json
import os
import re
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pymysql
import requests
from dotenv import load_dotenv

from thinking_layer.text2sql_thinking.text2sql_thinking_prompts.text2sql_thinking_prompts_all import (
    NORMALIZE_PROMPT_THINKING_TEMPLATE,
    PROMPT_A_SKELETON_BIBLE_THINKING,
    PROMPT_A_THINKING_TEMPLATE,
    PROMPT_B_THINKING_TEMPLATE,
    PROMPT_C_THINKING_TEMPLATE,
)

# =========================================================
# PATHS / ENV
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parents[1]          # thinking_layer/text2sql_thinking -> project root
load_dotenv(PROJECT_ROOT / ".env")


def _env_int(own: str, shared: str, default: int) -> int:
    return int(os.getenv(own) or os.getenv(shared) or default)


OPENROUTER_API_KEY = (os.getenv("OPENROUTER_API_KEY") or "").strip()
GEMINI_MODEL = (os.getenv("GEMINI_MODEL") or "google/gemini-3.8-flash").strip()
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DEBUG = (os.getenv("DEBUG_TRADE_THINKING") or os.getenv("DEBUG_TRADE", "1") or "").strip() \
    not in {"0", "false", "False", ""}

NORMALIZE_MAX_TOKENS = _env_int("THINKING_NORMALIZE_MAX_TOKENS", "NORMALIZE_MAX_TOKENS", 1000)
PROMPT_A_MAX_TOKENS = _env_int("THINKING_PROMPT_A_MAX_TOKENS", "PROMPT_A_MAX_TOKENS", 35000)
PROMPT_B_MAX_TOKENS = _env_int("THINKING_PROMPT_B_MAX_TOKENS", "PROMPT_B_MAX_TOKENS", 5000)
PROMPT_C_MAX_TOKENS = _env_int("THINKING_PROMPT_C_MAX_TOKENS", "PROMPT_C_MAX_TOKENS", 6000)
MAX_SQL_FIX_ATTEMPTS = _env_int("THINKING_MAX_SQL_FIX_ATTEMPTS", "MAX_SQL_FIX_ATTEMPTS", 3)

RESULT_ROWS_TRACE_LIMIT = 100          # rows passed to Prompt C, same as text2sql

# ---------------- VeloDB (Apache Doris, MySQL protocol) ----------------
VELODB_HOST = (os.getenv("VELODB_HOST") or "").strip()
VELODB_PORT = int((os.getenv("VELODB_PORT") or "9030").strip())
VELODB_USER = (os.getenv("VELODB_USER") or "").strip()
VELODB_PASSWORD = os.getenv("VELODB_PASSWORD") or ""
VELODB_DATABASE = (os.getenv("VELODB_DATABASE") or "").strip()

TRADE_TABLE_NAME = (os.getenv("VELODB_TRADE_TABLE_NAME"))

# =========================================================
# SCHEMA (used by Prompt B)
# =========================================================
SCHEMA_DESCRIPTION = f"""Engine: VeloDB Cloud 26.0.5.1 (Apache Doris-compatible SQL via MySQL protocol)
Table Name: {TRADE_TABLE_NAME}
Physical columns:
- shipping_date (DATE)
- quantity (DOUBLE)
- shipment_value (DECIMAL(18,4))
- product_description (text)
- buyer_country, buyer_state, buyer_city, buyer_address, buyer_company (varchar)
- port_of_destination (varchar)
- seller_country, seller_state, seller_city, seller_address, seller_company (varchar)
- port_of_origin (varchar)
- unit (varchar)
- decl_no (varchar, nullable shipment reference; not a logical unique key)
- hs_code (varchar)
"""

# =========================================================
# SQL DIALECT (injected into Prompt A and Prompt B)
# =========================================================
# If a thinking prompt template contains the placeholder {sql_dialect_notes}, the block
# is inserted there; otherwise it is prepended to the prompt.
VELODB_SQL_DIALECT_NOTES = """TARGET SQL ENGINE: VeloDB Cloud 26.0.5.1 (Apache Doris-compatible, MySQL protocol). NOT Snowflake.
This hard runtime guard MUST agree with the native VeloDB rules inside Prompt A/B.

- Output exactly ONE read-only SELECT/WITH statement.
- Physical trade columns are lowercase.
- Month shifts: MONTHS_ADD(date_expr, signed_months). Day differences: DATEDIFF(end_date, start_date). Calendar month difference: (YEAR(end_date)*12 + MONTH(end_date)) - (YEAR(start_date)*12 + MONTH(start_date)).
- Date truncation: DATE_TRUNC(date_expr, 'month'|'quarter'|'year').
- Exact percentile: PERCENTILE_CONT(CAST(value_expr AS DOUBLE), percentile); never WITHIN GROUP or percentile windows.
- REGR_SLOPE / REGR_INTERCEPT are available; REGR_R2 is not. Derive r2 with guarded CORR/VAR_POP when needed.
- COUNT_IF is unavailable; use SUM(CASE WHEN condition THEN 1 ELSE 0 END).
- Use CONCAT(...) and GROUP_CONCAT; do not use || or LISTAGG.
- HS_CODE is a clean digit-only VARCHAR in production. Preserve leading zeros and filter hs_code directly; never normalize the physical column with REGEXP_REPLACE/REPLACE. Sanitize only user-provided HS text before SQL. Use LIKE for one prefix, SUBSTR(hs_code, 1, N) IN (...) for multiple equal-length prefixes, and = for an exact full code.
- PRODUCT_DESCRIPTION uses exact LIKE plus a token-level NGRAM_SEARCH fallback for ASCII product tokens length >=4. Build a real token ARRAY with SPLIT_BY_STRING(REGEXP_REPLACE(UPPER(td.product_description), '[^A-Z0-9]+', ' '), ' '), require token-length difference <=2, and use NGRAM_SEARCH(token, pattern, 2) >= 0.80. Never run NGRAM_SEARCH against the entire long product_description. REGEXP_EXTRACT_ALL must NOT be used as the ARRAY source on this production cluster.
- ARRAY_COUNT, SPLIT_BY_STRING, REGEXP_REPLACE, CHAR_LENGTH, ABS and NGRAM_SEARCH are allowed for that canonical product predicate.
- When both product-description matching and HS scope are active, combine them as (PRODUCT_MATCH) AND (HS_SCOPE), never OR unless the user explicitly asks for either/union semantics.
- HS scope precedence: CURRENT explicit HS code(s) in the user/sub-question override any older remembered/resolved product HS scope. Reuse saved scope only when the current request supplies no explicit HS scope.
- Ports and countries are standardized in production. Countries use exact normalized equality after canonical resolution. Ports resolve against DISTINCT standardized candidates (exact first, contains second, NGRAM_SEARCH >= 0.90 only as typo fallback), then fact rows use exact normalized equality. Never fuzzy-match every fact row. Company matching keeps its existing rules.
- JAROWINKLER_SIMILARITY, LEVENSHTEIN, Damerau-Levenshtein, TIMESTAMPDIFF, ILIKE and Snowflake DATEADD are forbidden.
- Use single quotes for literals; unquoted identifiers when possible, backticks only if required; never double-quoted identifiers.
- The imports_exports schema is flat: no GET(...), FLATTEN, ::VARIANT, or stored ARRAY/VARIANT workarounds. Transient arrays from SPLIT_BY_STRING for canonical product-token matching are allowed.
- No QUALIFY: materialize window results then filter in the next query level.
"""


def apply_sql_dialect(prompt: str) -> str:
    if "{sql_dialect_notes}" in prompt:
        return prompt.replace("{sql_dialect_notes}", VELODB_SQL_DIALECT_NOTES)
    return VELODB_SQL_DIALECT_NOTES + "\n\n" + prompt


# =========================================================
# UTIL
# =========================================================
def current_date_pretty() -> str:
    return datetime.date.today().strftime("%B %d, %Y")


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


def default_visualization(rows: List[Dict[str, Any]], cols: List[str],
                          reason: str = "Fallback visualization.") -> Dict[str, Any]:
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
    print(f"\n{'=' * 20} THINKING {label} {'=' * 20}")
    if isinstance(value, (dict, list)):
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str)[:4000])
    else:
        print(str(value)[:4000])


# =========================================================
# LLM (OpenRouter)
# =========================================================
def call_gemini(prompt: str, max_tokens: int, temperature: float = 0.0) -> str:
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
        raise RuntimeError(f"Unexpected OpenRouter response: {str(data)[:500]}") from exc


# =========================================================
# NORMALIZE
# =========================================================
def fallback_normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def normalize_message(raw_message: str) -> str:
    """LLM cleanup of spelling / whitespace / country abbreviations. Falls back to
    whitespace cleanup if the call fails, so it never blocks the pipeline."""
    raw_message = (raw_message or "").strip()
    if not raw_message:
        return raw_message
    prompt = (NORMALIZE_PROMPT_THINKING_TEMPLATE
              .replace("{current_date}", current_date_pretty())
              .replace("{raw_message}", raw_message))
    try:
        out = call_gemini(prompt, max_tokens=NORMALIZE_MAX_TOKENS, temperature=0.0).strip()
        out = strip_markdown_fences(out).strip().strip('"').strip("'")
        return fallback_normalize(out or raw_message)
    except Exception:
        return fallback_normalize(raw_message)


# =========================================================
# PROMPTS A / B / C
# =========================================================
def build_prompt_a(user_query: str) -> str:
    prompt = (PROMPT_A_THINKING_TEMPLATE
              .replace("{current_date}", current_date_pretty())
              .replace("{user_query}", user_query))
    return apply_sql_dialect(prompt)


def build_prompt_b(user_query: str, interpretation: str, plan: str, attempt_history: str,
                   latest_sql: str, latest_error: str, attempt_index: int) -> str:
    prompt = (PROMPT_B_THINKING_TEMPLATE
              .replace("{current_date}", current_date_pretty())
              .replace("{user_query}", user_query)
              .replace("{interpretation_summary}", interpretation)
              .replace("{sql_implementation_plan}", plan)
              .replace("{schema_description}", SCHEMA_DESCRIPTION)
              .replace("{attempt_history}", attempt_history)
              .replace("{latest_sql}", latest_sql)
              .replace("{latest_error}", latest_error)
              .replace("{latest_attempt_index}", str(attempt_index)))
    return apply_sql_dialect(prompt)


def build_prompt_c(user_query: str, interpretation: str, plan: str, executed_sql: str,
                   rows: List[Dict[str, Any]], cols: List[str]) -> str:
    return (PROMPT_C_THINKING_TEMPLATE
            .replace("{current_date}", current_date_pretty())
            .replace("{user_query}", user_query)
            .replace("{interpretation_summary}", interpretation)
            .replace("{sql_implementation_plan}", plan)
            .replace("{executed_sql}", executed_sql)
            .replace("{row_count}", str(len(rows)))
            .replace("{column_names}", ", ".join(cols))
            .replace("{result_rows}", json.dumps(rows[:RESULT_ROWS_TRACE_LIMIT], ensure_ascii=False, default=str)))


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


def format_attempt_history(attempts: List[Dict[str, str]]) -> str:
    lines = []
    for i, a in enumerate(attempts, start=1):
        lines.append(f"Attempt {i}:\nSQL:\n{a['sql']}\nERROR:\n{a['error']}\n")
    return "\n".join(lines).strip()


# Known production compatibility repair: on this VeloDB cluster,
# REGEXP_EXTRACT_ALL is not an ARRAY value suitable for ARRAY_COUNT.  Convert the
# canonical product-token source to SPLIT_BY_STRING(REGEXP_REPLACE(...)), which is
# array-typed and preserves the same token-level NGRAM semantics.
_REGEXP_EXTRACT_PRODUCT_ARRAY_RE = re.compile(
    r"REGEXP_EXTRACT_ALL\(\s*UPPER\(\s*([A-Za-z_][A-Za-z0-9_]*\.product_description)\s*\)\s*,\s*'\(\[A-Z0-9\]\+\)'\s*\)",
    re.IGNORECASE,
)

def _replace_array_exists_with_scalar_count(sql: str) -> str:
    """Replace ARRAY_EXISTS(lambda, array) with (ARRAY_COUNT(lambda, array) > 0).

    Doris/VeloDB ARRAY_EXISTS returns ARRAY<BOOLEAN>, so it cannot be used directly
    as a WHERE predicate. This balanced-parenthesis rewrite is deterministic and only
    touches ARRAY_EXISTS calls.
    """
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


# Production invariant: hs_code is clean digit-only VARCHAR. Canonicalize stale
# model output before execution so prompt drift cannot re-introduce row-level
# regex/replacement normalization or LEFT-vs-SUBSTR variation.
def enforce_static_velodb_sql_contract(sql: str) -> str:
    s = sql or ""

    # REGEXP_REPLACE(TRIM(alias.hs_code), '[^0-9]', '') -> alias.hs_code
    s = re.sub(
        r"(?is)REGEXP_REPLACE\(\s*TRIM\(\s*([A-Za-z_][A-Za-z0-9_]*\.)?hs_code\s*\)\s*,\s*'\[\^0-9\]'\s*,\s*''\s*\)",
        lambda m: f"{m.group(1) or ''}hs_code",
        s,
    )
    # REGEXP_REPLACE(alias.hs_code, '[^0-9]', '') -> alias.hs_code
    s = re.sub(
        r"(?is)REGEXP_REPLACE\(\s*([A-Za-z_][A-Za-z0-9_]*\.)?hs_code\s*,\s*'\[\^0-9\]'\s*,\s*''\s*\)",
        lambda m: f"{m.group(1) or ''}hs_code",
        s,
    )
    # Common legacy triple-REPLACE normalization -> direct hs_code.
    s = re.sub(
        r"(?is)REPLACE\(\s*REPLACE\(\s*REPLACE\(\s*([A-Za-z_][A-Za-z0-9_]*\.)?hs_code\s*,\s*'\.'\s*,\s*''\s*\)\s*,\s*'-'\s*,\s*''\s*\)\s*,\s*' '\s*,\s*''\s*\)",
        lambda m: f"{m.group(1) or ''}hs_code",
        s,
    )
    # Canonical prefix extraction uses SUBSTR for hs_code.
    s = re.sub(
        r"(?is)LEFT\(\s*(([A-Za-z_][A-Za-z0-9_]*\.)?hs_code)\s*,\s*(\d+)\s*\)",
        lambda m: f"SUBSTR({m.group(1)}, 1, {m.group(3)})",
        s,
    )
    return s

def apply_known_velodb_compatibility_repairs(sql: str, error: str) -> str:
    current = sql or ""
    err = (error or "").lower()
    if "lambda argument must be array" in err and "regexp_extract_all" in current.lower():
        current = _REGEXP_EXTRACT_PRODUCT_ARRAY_RE.sub(
            r"SPLIT_BY_STRING(REGEXP_REPLACE(UPPER(\1), '[^A-Z0-9]+', ' '), ' ')",
            current,
        )
    if (
        "array<boolean>" in err
        or "target type=boolean" in err
        or "can not cast from origin type" in err
    ) and "array_exists" in current.lower():
        current = _replace_array_exists_with_scalar_count(current)
    return current

# =========================================================
# VELODB
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


# =========================================================
# PIPELINE
# =========================================================
def _result(stage: str, **kw) -> Dict[str, Any]:
    out = {
        "stage": stage,
        "question": "",
        "normalized_query": "",
        "narrative": "",
        "visualization": default_visualization([], []),
        "sql": "",
        "rows": [],
        "cols": [],
        "attempts": [],
        "prompt_a_raw": "",
        "prompt_a_parts": {"interpretation_summary": "", "sql_plan": "", "sql": "", "raw": ""},
        "prompt_c_raw": "",
        "error": None,
        "seconds": 0.0,
    }
    out.update(kw)
    return out


def _narrate(question: str, a_parts: Dict[str, str], sql: str,
             rows: List[Dict[str, Any]], cols: List[str]) -> Tuple[str, Dict[str, Any], str]:
    """Prompt C → (narrative, visualization, raw)."""
    c_raw = call_gemini(
        build_prompt_c(
            user_query=question,
            interpretation=a_parts.get("interpretation_summary", ""),
            plan=a_parts.get("sql_plan", ""),
            executed_sql=sql,
            rows=rows,
            cols=cols,
        ),
        max_tokens=PROMPT_C_MAX_TOKENS,
        temperature=0.0,
    ).strip()
    # dbg("PROMPT C RAW", c_raw)

    try:
        c_json = extract_json_object(c_raw)
        narrative = clean_narrative_answer(str(c_json.get("narrative_answer", "")).strip())
        visualization = sanitize_visualization_spec(c_json.get("visualization"), rows)
    except Exception:
        narrative = clean_narrative_answer(c_raw)
        visualization = default_visualization(
            rows, cols, "Fallback visualization because Prompt C did not return valid visualization JSON.")

    if not narrative:
        if not rows:
            narrative = ("There are no recorded shipments matching those criteria in the selected "
                         "period, so the requested metric cannot be computed.")
        else:
            narrative = f"The query returned {len(rows)} row{'s' if len(rows) != 1 else ''}."
    return narrative, visualization, c_raw


def run(question: str) -> Dict[str, Any]:
    """Run one complete question: Normalize → Prompt A → VeloDB (+ Prompt B fixes) → Prompt C.

    Never raises; failures come back as a non-"success" stage with "error" set.
    """
    started = time.time()
    question = (question or "").strip()
    raw_question = question
    a_raw = ""
    a_parts = {"interpretation_summary": "", "sql_plan": "", "sql": "", "raw": ""}
    attempts: List[Dict[str, str]] = []
    sql_to_run = ""

    def done(stage: str, **kw) -> Dict[str, Any]:
        return _result(stage, question=raw_question, normalized_query=question, prompt_a_raw=a_raw, prompt_a_parts=a_parts, attempts=attempts,
                       seconds=round(time.time() - started, 2), **kw)

    if not question:
        return done("pipeline_exception", narrative="No question given.", error="Empty question")

    try:
        question = normalize_message(raw_question)
        # dbg("QUERY", raw_question)
        # dbg("NORMALIZED", question)
        a_raw = call_gemini(build_prompt_a(question), max_tokens=PROMPT_A_MAX_TOKENS, temperature=0.0)
        a_parts = parse_prompt_a_output(a_raw)
        sql_to_run = enforce_static_velodb_sql_contract((a_parts.get("sql") or "").strip())
        dbg("SQL FROM PROMPT A", sql_to_run)

        if not sql_to_run:
            return done("prompt_a_no_sql",
                        narrative="No Prompt A interpretation / plan available.",
                        error="Prompt A did not produce SQL")

        for attempt_idx in range(1, MAX_SQL_FIX_ATTEMPTS + 1):
            ok, rows, cols, err = run_sql(sql_to_run)
            if ok:
                rows = convert_decimals(rows)
                # dbg("ROW COUNT", f"{len(rows)} rows, cols = {cols}")
                narrative, visualization, c_raw = _narrate(question, a_parts, sql_to_run, rows, cols)
                # dbg("ANSWER", narrative)
                return done("success", narrative=narrative, visualization=visualization,
                            sql=sql_to_run, rows=rows, cols=cols, prompt_c_raw=c_raw)

            attempts.append({"sql": sql_to_run, "error": err})
            # dbg(f"SQL FAILED (attempt {attempt_idx})", err)

            local_repair = apply_known_velodb_compatibility_repairs(sql_to_run, err)
            if local_repair != sql_to_run:
                sql_to_run = local_repair
                continue

            if attempt_idx == MAX_SQL_FIX_ATTEMPTS:
                return done("sql_execution_failure", narrative="SQL execution failed.",
                            sql=sql_to_run, error=err)

            b_raw = call_gemini(
                build_prompt_b(
                    user_query=question,
                    interpretation=a_parts.get("interpretation_summary", ""),
                    plan=a_parts.get("sql_plan", ""),
                    attempt_history=format_attempt_history(attempts[-3:]),
                    latest_sql=sql_to_run,
                    latest_error=err,
                    attempt_index=attempt_idx,
                ),
                max_tokens=PROMPT_B_MAX_TOKENS,
                temperature=0.0,
            )
            sql_to_run = enforce_static_velodb_sql_contract(parse_prompt_b_output(b_raw))

        return done("sql_execution_failure", narrative="SQL execution failed.",
                    sql=sql_to_run, error="No attempts were made")
    except Exception as e:
        return done("pipeline_exception", narrative="Pipeline exception occurred.",
                    sql=sql_to_run, error=f"{type(e).__name__}: {e}")
