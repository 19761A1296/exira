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
VELODB_TRANSIENT_RETRY_DELAY_SECONDS = float(
    os.getenv("THINKING_VELODB_TRANSIENT_RETRY_DELAY_SECONDS")
    or os.getenv("VELODB_TRANSIENT_RETRY_DELAY_SECONDS")
    or "1.0"
)

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
Physical columns allowed for thinking Text2SQL:
- shipping_date (DATE)
- quantity (DOUBLE)
- shipment_value (DECIMAL(18,4))
- product_description_norm (text; ONLY product-description field used by thinking Text2SQL; already uppercase, whitespace-clean, and special-character-normalized)
- buyer_country, buyer_state, buyer_city, buyer_address, buyer_company (varchar; uppercase + whitespace-clean)
- port_of_destination (varchar; uppercase + whitespace-clean)
- seller_country, seller_state, seller_city, seller_address, seller_company (varchar; uppercase + whitespace-clean)
- port_of_origin (varchar; uppercase + whitespace-clean)
- unit (varchar; uppercase + whitespace-clean)
- decl_no (varchar, nullable shipment reference; not a logical unique key)
- hs_code (varchar; digit-only, leading zeros preserved)
Raw product_description may physically exist in VeloDB but MUST NOT be used by thinking Text2SQL.
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
- HS_CODE is a clean digit-only VARCHAR. Preserve leading zeros and keep the physical hs_code bare. Never wrap hs_code in REGEXP_REPLACE/REPLACE, SUBSTR, LEFT, or casts for filtering. Sanitize only user-provided HS text. Treat every supplied HS digit string as a prefix by default: use hs_code LIKE '<code>%' and OR direct LIKE predicates for multiple codes. Use equality only when the CURRENT user explicitly says exact HS / exact HS code; never infer exactness from digit length.
- PRODUCT_DESCRIPTION_NORM is the ONLY product-description column allowed for thinking Text2SQL. Raw product_description may physically exist but MUST NOT be referenced. product_description_norm is already uppercase, whitespace-clean, and special-character-normalized.
- Product matching uses exact substring plus token-level NGRAM_SEARCH for ASCII product tokens length >=4. Use SPLIT_BY_STRING(td.product_description_norm, ' ') directly, require token-length difference <=2, and NGRAM_SEARCH(token, pattern, 2) >= 0.80. Never wrap product_description_norm in UPPER/LOWER/TRIM/REGEXP_REPLACE and never run NGRAM_SEARCH against the entire long description.
- ARRAY_COUNT, SPLIT_BY_STRING, CHAR_LENGTH, ABS and NGRAM_SEARCH are permitted for that canonical product predicate. REGEXP_EXTRACT_ALL must NOT be used as the ARRAY source on this production cluster.
- All stored text dimensions used by thinking Text2SQL are already uppercase and whitespace-clean. Keep columns bare in equality, LIKE, joins, DISTINCT, GROUP BY, COUNT(DISTINCT), and window partitions. Normalize only user-provided literals before SQL generation.
- When both product-description matching and HS scope are active for one product, combine them as (PRODUCT_MATCH) AND (HS_SCOPE).
- For multiple product-specific mappings, preserve each pair independently and combine complete pairs with OR: (PRODUCT_1 AND HS_1) OR (PRODUCT_2 AND HS_2) ... . Never flatten HS codes across products.
- SYSTEM_RESOLVED_PRODUCT_FILTER_CONTRACT, when present in the incoming question/context, is authoritative. Preserve every product-specific pair and overall predicate exactly.
- HS precedence: CURRENT explicit user HS > current-query company-profile resolver scope supplied in the authoritative contract > older remembered/resolved scope.
- Ports and countries are standardized in production. Countries use direct equality after canonical resolution. Ports resolve against DISTINCT stored candidates (exact first, contains second, NGRAM_SEARCH >= 0.90 only as typo fallback), then fact rows use direct equality. Never fuzzy-match every fact row. Company matching keeps its existing rules, but final comparisons use bare canonical columns.
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


PRODUCT_SCOPE_CONTRACT_HEADER = "SYSTEM_RESOLVED_PRODUCT_FILTER_CONTRACT"

def split_authoritative_product_scope_contract(text: str) -> Tuple[str, str]:
    """Normalize only the human query; preserve an upstream authoritative scope contract byte-for-byte."""
    raw = text or ""
    marker = "\n\n" + PRODUCT_SCOPE_CONTRACT_HEADER
    if marker in raw:
        base, contract = raw.split(marker, 1)
        return base.rstrip(), PRODUCT_SCOPE_CONTRACT_HEADER + contract
    if raw.startswith(PRODUCT_SCOPE_CONTRACT_HEADER):
        return "", raw
    return raw, ""

def reattach_authoritative_product_scope_contract(base_query: str, contract: str) -> str:
    base = (base_query or "").strip()
    contract = (contract or "").strip()
    if not contract:
        return base
    if not base:
        return contract
    return base + "\n\n" + contract


HS_INPUT_CONTRACT_HEADER = "SYSTEM_RESOLVED_HS_INPUT_CONTRACT"


def normalize_hs_token(code: str) -> Optional[str]:
    raw = re.sub(r"[^0-9]", "", (code or "").strip())
    if not re.fullmatch(r"\d{1,18}", raw):
        return None
    if len(raw) <= 2:
        return raw.zfill(2)
    return raw


def _expand_hs_fragment(fragment: str) -> List[str]:
    frag = (fragment or "").strip().strip(",;/& ")
    if not frag:
        return []
    frag = re.sub(r"(?i)\band\b", ",", frag)
    parts = [x.strip() for x in re.split(r"\s*(?:,|/|&)\s*", frag) if x.strip()]
    out: List[str] = []
    for part in parts:
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
    raw = text or ""
    marker_re = re.compile(
        r"(?i)\b(?P<exact>exact\s+)?hs(?:\s*(?:codes?|chapters?|headings?|prefix(?:es)?))?\s*[-:]?\s*"
    )
    mentions: List[Dict[str, Any]] = []
    for marker in marker_re.finditer(raw):
        seg = re.match(
            r"(?i)([0-9][0-9\s\.,/&\-–—]*(?:(?:\band\b|\bto\b)[0-9\s\.,/&\-–—]*)*)",
            raw[marker.end():],
        )
        if not seg:
            continue
        codes = _expand_hs_fragment(seg.group(1).rstrip())
        if codes:
            end = marker.end() + seg.end(1)
            mentions.append({
                "text": raw[marker.start():end],
                "codes": codes,
                "exact_codes": list(codes) if marker.group("exact") else [],
            })
    if not mentions and re.fullmatch(r"\s*\d{2,18}\s*", raw):
        code = normalize_hs_token(raw)
        if code:
            mentions.append({"text": raw.strip(), "codes": [code], "exact_codes": []})
    return mentions


def extract_exact_hs_codes(text: str) -> List[str]:
    out: List[str] = []
    for mention in extract_explicit_hs_mentions(text):
        for code in mention.get("exact_codes") or []:
            if code not in out:
                out.append(code)
    return out


def build_hs_input_contract(raw_query: str) -> str:
    mentions = extract_explicit_hs_mentions(raw_query)
    if not mentions:
        return ""
    lines = [
        f"{HS_INPUT_CONTRACT_HEADER} (AUTHORITATIVE PARSING; INTERNAL)",
        "The runtime parsed the CURRENT user's explicit HS wording below. Preserve the original grammatical product association; NEVER flatten codes from separate product mentions into one shared list.",
        "Every parsed HS digit string is a hierarchical prefix by default and must use hs_code LIKE '<code>%'. Use equality only for codes listed under Exact codes, which means the user explicitly said exact HS / exact HS code.",
        "A chapter range such as HS 50-63 has already been expanded to every chapter 50 through 63. Dotted/spaced HS formatting has already been normalized without truncation.",
    ]
    for idx, m in enumerate(mentions, 1):
        lines.append(f"Mention {idx}: {m['text']}")
        lines.append("Normalized codes: " + ", ".join(m.get("codes") or []))
        lines.append("Exact codes: " + (", ".join(m.get("exact_codes") or []) or "(none)"))
    return "\n".join(lines)

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


# Known production compatibility repair/backstop.
# The optimized production contract is:
#   * product_description_norm is the only product field used by thinking Text2SQL,
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
    """Canonicalize stale model SQL to the optimized thinking production access contract."""
    s = sql or ""

    # Raw product_description physically exists but is forbidden for thinking Text2SQL.
    s = re.sub(r"(?i)\bproduct_description\b", "product_description_norm", s)

    # product_description_norm is already normalized; remove stale row-level cleanup.
    s = re.sub(
        r"(?is)REGEXP_REPLACE\(\s*(?:UPPER\(\s*)?(?P<col>(?:[A-Za-z_][A-Za-z0-9_]*\.)?product_description_norm)\s*\)?\s*,\s*'\[\^A-Z0-9\]\+'\s*,\s*' '\s*\)",
        lambda m: m.group("col"),
        s,
    )

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
    low = (error or "").lower()
    return bool(low and any(marker in low for marker in _TRANSIENT_VELODB_ERROR_MARKERS))

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
        base_question, scope_contract = split_authoritative_product_scope_contract(raw_question)
        normalized_base = normalize_message(base_question)
        question = reattach_authoritative_product_scope_contract(normalized_base, scope_contract)
        hs_input_contract = build_hs_input_contract(base_question)
        if hs_input_contract:
            question = question.rstrip() + "\n\n" + hs_input_contract
        # dbg("QUERY", raw_question)
        # dbg("NORMALIZED", question)
        if scope_contract:
            dbg("AUTHORITATIVE PRODUCT SCOPE CONTRACT PRESERVED", scope_contract)
        if hs_input_contract:
            dbg("AUTHORITATIVE HS INPUT CONTRACT", hs_input_contract)
        a_raw = call_gemini(build_prompt_a(question), max_tokens=PROMPT_A_MAX_TOKENS, temperature=0.0)
        a_parts = parse_prompt_a_output(a_raw)
        sql_to_run = enforce_static_velodb_sql_contract((a_parts.get("sql") or "").strip(), question)
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

            if is_transient_velodb_error(err):
                if attempt_idx == MAX_SQL_FIX_ATTEMPTS:
                    return done(
                        "sql_infrastructure_failure",
                        narrative="The trade-data service is temporarily unavailable, so this analysis could not be completed from trade records.",
                        sql=sql_to_run,
                        error=err,
                    )
                if VELODB_TRANSIENT_RETRY_DELAY_SECONDS > 0:
                    # Back off between retries so a briefly unhealthy compute group has
                    # time to recover.  The SQL text itself is never changed for an
                    # infrastructure failure.
                    delay = VELODB_TRANSIENT_RETRY_DELAY_SECONDS * (2 ** (attempt_idx - 1))
                    time.sleep(delay)
                continue

            local_repair = apply_known_velodb_compatibility_repairs(sql_to_run, err, question)
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
            sql_to_run = enforce_static_velodb_sql_contract(parse_prompt_b_output(b_raw), question)

        return done("sql_execution_failure", narrative="SQL execution failed.",
                    sql=sql_to_run, error="No attempts were made")
    except Exception as e:
        return done("pipeline_exception", narrative="Pipeline exception occurred.",
                    sql=sql_to_run, error=f"{type(e).__name__}: {e}")
