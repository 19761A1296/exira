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
    Snowflake  ──error──►  Prompt B (thinking) rewrites the SQL,
       │                   up to MAX_SQL_FIX_ATTEMPTS attempts in total
       ▼
    Prompt C (thinking)  →  narrative + visualization spec
       │
       ▼
    result dict

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
    SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PASSWORD, SNOWFLAKE_WAREHOUSE,
    SNOWFLAKE_DATABASE, SNOWFLAKE_SCHEMA, SNOWFLAKE_ROLE, TRADE_TABLE_NAME
    NORMALIZE_MAX_TOKENS, PROMPT_A_MAX_TOKENS, PROMPT_B_MAX_TOKENS, PROMPT_C_MAX_TOKENS,
    MAX_SQL_FIX_ATTEMPTS
    DEBUG_TRADE
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
from typing import Any, Dict, List, Tuple

import requests
import snowflake.connector
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

SNOWFLAKE_ACCOUNT = os.getenv("SNOWFLAKE_ACCOUNT", "")
SNOWFLAKE_USER = os.getenv("SNOWFLAKE_USER", "")
SNOWFLAKE_PASSWORD = os.getenv("SNOWFLAKE_PASSWORD", "")
SNOWFLAKE_WAREHOUSE = os.getenv("SNOWFLAKE_WAREHOUSE", "")
SNOWFLAKE_DATABASE = os.getenv("SNOWFLAKE_DATABASE", "")
SNOWFLAKE_SCHEMA = os.getenv("SNOWFLAKE_SCHEMA", "PUBLIC")
SNOWFLAKE_ROLE = os.getenv("SNOWFLAKE_ROLE", "")

TRADE_TABLE_NAME = os.getenv("TRADE_TABLE_NAME", "imports_exports")

# =========================================================
# SCHEMA (used by Prompt B)
# =========================================================
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
    return (PROMPT_A_THINKING_TEMPLATE
            .replace("{current_date}", current_date_pretty())
            .replace("{user_query}", user_query))


def build_prompt_b(user_query: str, interpretation: str, plan: str, attempt_history: str,
                   latest_sql: str, latest_error: str, attempt_index: int) -> str:
    return (PROMPT_B_THINKING_TEMPLATE
            .replace("{current_date}", current_date_pretty())
            .replace("{user_query}", user_query)
            .replace("{interpretation_summary}", interpretation)
            .replace("{sql_implementation_plan}", plan)
            .replace("{schema_description}", SCHEMA_DESCRIPTION)
            .replace("{attempt_history}", attempt_history)
            .replace("{latest_sql}", latest_sql)
            .replace("{latest_error}", latest_error)
            .replace("{latest_attempt_index}", str(attempt_index)))


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
    raw = strip_sql_end(text or "")
    m = re.search(r"(?is)\b(WITH\b[\s\S]*?;|\bSELECT\b[\s\S]*?;)", raw)
    return m.group(1).strip() if m else raw.strip()


def format_attempt_history(attempts: List[Dict[str, str]]) -> str:
    lines = []
    for i, a in enumerate(attempts, start=1):
        lines.append(f"Attempt {i}:\nSQL:\n{a['sql']}\nERROR:\n{a['error']}\n")
    return "\n".join(lines).strip()


# =========================================================
# SNOWFLAKE
# =========================================================
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
    """Run one complete question: Normalize → Prompt A → Snowflake (+ Prompt B fixes) → Prompt C.

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
        sql_to_run = (a_parts.get("sql") or "").strip()
        dbg("SQL FROM PROMPT A", sql_to_run)

        if not sql_to_run:
            return done("prompt_a_no_sql",
                        narrative="No Prompt A interpretation / plan available.",
                        error="Prompt A did not produce SQL")

        for attempt_idx in range(1, MAX_SQL_FIX_ATTEMPTS + 1):
            ok, rows, cols, err = run_snowflake(sql_to_run)
            if ok:
                rows = convert_decimals(rows)
                # dbg("ROW COUNT", f"{len(rows)} rows, cols = {cols}")
                narrative, visualization, c_raw = _narrate(question, a_parts, sql_to_run, rows, cols)
                # dbg("ANSWER", narrative)
                return done("success", narrative=narrative, visualization=visualization,
                            sql=sql_to_run, rows=rows, cols=cols, prompt_c_raw=c_raw)

            attempts.append({"sql": sql_to_run, "error": err})
            # dbg(f"SQL FAILED (attempt {attempt_idx})", err)
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
            sql_to_run = parse_prompt_b_output(b_raw)

        return done("sql_execution_failure", narrative="SQL execution failed.",
                    sql=sql_to_run, error="No attempts were made")
    except Exception as e:
        return done("pipeline_exception", narrative="Pipeline exception occurred.",
                    sql=sql_to_run, error=f"{type(e).__name__}: {e}")
