"""
HS resolution layer (product -> HS codes). Replaces the old Gemma product resolver.

STEP 0  parse_explicit_hs(): read every explicit HS code in the question and link it
        to its own product term. Terms are sorted into
            explicit  - has its own code        -> product word AND code, no ladder
            wrapper   - "apple-branded electronic products" around a coded list -> ignored
            product   - no code                 -> resolution ladder
        Codes that cannot be linked are shared by the product terms without a code,
        or used as an HS-only filter when there are none.

LADDER  (per product term without a code; first hit wins)
    1. CACHE         per company + session; a term can hold several meanings
                     (apple -> fruit / electronics). Sonar picks the meaning by context;
                     the default (most records) is used when context cannot decide;
                     a meaning not in the cache sends the term down the ladder again.
    -  CAP           only the first HS_LAYER_MAX_PRODUCTS terms that still need resolving
    2. PROFILE       exact item match in the company profile
    3. COMPANY_DATA  company shipments, last 36 completed months, buyer or seller side,
                     MATCH_ALL. One Sonar call keeps/rejects codes and groups the kept ones
                     by meaning. Context decides the group, else most records, tie random.
                     Top 10 codes by record count. All groups are cached.
    4. WEB           Sonar maps the product to 6-digit HS from context (every question).
    5. PICKER        old HS2 domain picker (only when ambiguous or everything failed).

All LLM calls here use Sonar (perplexity/sonar). This module has no import-time
dependency on text2sql_tool: run_sql / call_model / save_scope / normalize_key are
passed in, which avoids a circular import.
"""

from __future__ import annotations

import ast
import datetime
import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

# =========================================================
# CONFIG
# =========================================================
HS_SONAR_MODEL = (os.getenv("HS_SONAR_MODEL") or "perplexity/sonar").strip()
HS_VALIDATOR_MODEL = HS_SONAR_MODEL
HS_WEB_MODEL = HS_SONAR_MODEL
HS_MEANING_MODEL = HS_SONAR_MODEL
HS_MEANING_MAX_TOKENS = int(os.getenv("HS_MEANING_MAX_TOKENS", "600"))
HS_GROUP_TOP_CODES = int(os.getenv("HS_GROUP_TOP_CODES", "10"))
HS_PARALLEL_WORKERS = int(os.getenv("HS_PARALLEL_WORKERS", "5"))
HS_VALIDATOR_MAX_TOKENS = int(os.getenv("HS_VALIDATOR_MAX_TOKENS", "4000"))
HS_WEB_MAX_TOKENS = int(os.getenv("HS_WEB_MAX_TOKENS", "1500"))
HS_LAYER_MAX_PRODUCTS = int(os.getenv("HS_LAYER_MAX_PRODUCTS", "5"))
HS_COMPANY_LOOKBACK_MONTHS = int(os.getenv("HS_COMPANY_LOOKBACK_MONTHS", "36"))
HS_VALIDATOR_MAX_CANDIDATES = int(os.getenv("HS_VALIDATOR_MAX_CANDIDATES", "300"))
HS_CONTEXT_MAX_CHARS = 2500

# session["active_focus"] keys owned by this module
TRACE_KEY = "hs_resolution_trace"
FLOW_CTX_KEY = "hs_flow_context"
CACHE_COMPANY_KEY = "product_hs_memory_company"
IGNORED_KEY = "hs_ignored_terms"
UNSCOPED_KEY = "hs_unscoped_terms"   # probe found no data: run without HS scope

# memory entry "source" values
SRC_EXPLICIT = "EXPLICIT"
SRC_PROFILE = "COMPANY_PROFILE"
SRC_COMPANY_DATA = "COMPANY_DATA_VALIDATED"
SRC_WEB = "WEB_SONAR_HS6"
SRC_EXPLICIT_SHARED = "EXPLICIT_SHARED"
SRC_PICKER = "HS_PICKER"

SOURCE_LABELS = {
    SRC_EXPLICIT: "User-explicit HS",
    SRC_EXPLICIT_SHARED: "User-explicit HS (shared)",
    SRC_PROFILE: "Company profile",
    SRC_COMPANY_DATA: "Company data (Sonar validated)",
    SRC_WEB: "Web (Sonar, HS6)",
    SRC_PICKER: "HS domain picker",
}


# =========================================================
# SMALL HELPERS
# =========================================================
def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _digits(code: Any) -> str:
    return re.sub(r"[^0-9]", "", str(code or ""))


def _dedupe(items: List[str]) -> List[str]:
    seen, out = set(), []
    for x in items or []:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _row_get(row: Dict[str, Any], *names: str) -> Any:
    """Case-insensitive column lookup (VeloDB may return aliases in either case)."""
    lower = {str(k).lower(): v for k, v in (row or {}).items()}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None


# def _sql_literal(value: str) -> str:
#     # MySQL-protocol engines treat backslash as an escape character too.
#     return (value or "").replace("\\", "\\\\").replace("'", "''").strip()


def _parse_json_loose(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    raw = re.sub(r"```(?:json|JSON)?", "", raw).strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"(?s)\{.*\}", raw)
        if m:
            return json.loads(m.group(0))
    raise ValueError("model did not return JSON")


# =========================================================
# COMPANY PROFILE
# =========================================================
def _profile_obj(profile: Any) -> Any:
    """Profile may arrive as dict, JSON string, or Python-repr string."""
    if isinstance(profile, (dict, list)):
        return profile
    text = str(profile or "").strip()
    if not text:
        return None
    for loader in (json.loads, ast.literal_eval):
        try:
            return loader(text)
        except Exception:
            continue
    return text


def profile_blob(profile: Any) -> str:
    obj = _profile_obj(profile)
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


def company_name_from_profile(profile: Any) -> Optional[str]:
    """Exact company name as written in the profile's company_name field."""
    obj = _profile_obj(profile)
    if isinstance(obj, dict):
        name = obj.get("company_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    blob = profile_blob(profile)
    m = re.search(r"""["']company_name["']\s*:\s*["']([^"']+)["']""", blob)
    return m.group(1).strip() if m else None


# Simple descriptors that do not change what the product is (user-approved list).
PROFILE_DESCRIPTORS = {"FRESH", "FROZEN", "DRIED", "DRY", "CHILLED", "RAW", "WHOLE", "ORGANIC", "NATURAL"}

_HS_IN_PROFILE_RE = re.compile(
    r"\(\s*HS(?:\s*codes?)?\s*[:#]?\s*([0-9][0-9 ,./;\-]*)\)", re.IGNORECASE
)
_PHRASE_SPLIT_RE = re.compile(
    r"""[,;:.\n\[\]{}()"]|(?<![A-Za-z])'|'(?![A-Za-z])"""
    r"""|\b(?:are|is|include|includes|including|such\s+as|like|namely)\b""",
    re.IGNORECASE,
)
_PART_SPLIT_RE = re.compile(r"\s+(?:and|&|or)\s+|/", re.IGNORECASE)


def _singular(word: str) -> str:
    w = word
    if len(w) <= 3:
        return w
    if w.endswith("IES") and len(w) > 4:
        return w[:-3] + "Y"
    if w.endswith("OES"):
        return w[:-2]
    if re.search(r"(SSES|XES|CHES|SHES|ZES)$", w):
        return w[:-2]
    if w.endswith("S") and not w.endswith("SS") and not w.endswith("US") and not w.endswith("IS"):
        return w[:-1]
    return w


def normalize_item_phrase(text: str) -> str:
    """Upper-case, drop simple descriptors, singularise each word.

    "fresh pineapples" -> "PINEAPPLE";  "cotton garments" -> "COTTON GARMENT".
    Equality on this form means the product word is never matched when it is
    only a modifier ("COTTON" != "COTTON GARMENT").
    """
    words = re.findall(r"[A-Z0-9]+(?:-[A-Z0-9]+)*", (text or "").upper())
    words = [w for w in words if w not in PROFILE_DESCRIPTORS]
    return " ".join(_singular(w) for w in words)


def parse_profile_hs_items(profile: Any) -> List[Dict[str, Any]]:
    """[{phrase, norm, codes}] for every '<item> (HS ...)' in the profile."""
    blob = profile_blob(profile)
    items: List[Dict[str, Any]] = []
    for m in _HS_IN_PROFILE_RE.finditer(blob):
        codes = [c for c in re.findall(r"\d{2,18}", m.group(1))]
        if not codes:
            continue
        before = blob[max(0, m.start() - 200):m.start()]
        pieces = _PHRASE_SPLIT_RE.split(before)
        phrase = (pieces[-1] if pieces else "").strip()
        phrase = re.sub(r"(?i)^(?:and|or)\s+", "", phrase).strip()
        if not phrase:
            continue
        for part in _PART_SPLIT_RE.split(phrase):
            part = part.strip()
            norm = normalize_item_phrase(part)
            if not norm or len(norm.split()) > 6:
                continue
            items.append({"phrase": part, "norm": norm, "codes": codes})
    return items


def profile_exact_match(profile: Any, product: str) -> Optional[Dict[str, Any]]:
    target = normalize_item_phrase(product)
    if not target:
        return None
    matched = [it for it in parse_profile_hs_items(profile) if it["norm"] == target]
    if not matched:
        return None
    return {
        "phrases": _dedupe([it["phrase"] for it in matched]),
        "codes": _dedupe([c for it in matched for c in it["codes"]]),
    }


# =========================================================
# COMPANY DATA SEARCH
# =========================================================
def company_search_window(today: Optional[datetime.date] = None) -> Tuple[datetime.date, datetime.date]:
    """Rolling window of the last N completed months ending at the anchor date
    (last day of the most recently completed month)."""
    today = today or datetime.date.today()
    first_this_month = today.replace(day=1)
    anchor = first_this_month - datetime.timedelta(days=1)
    y, m = divmod(first_this_month.year * 12 + (first_this_month.month - 1) - HS_COMPANY_LOOKBACK_MONTHS, 12)
    start = datetime.date(y, m + 1, 1)
    return start, anchor


def build_company_hs_sql(table: str, company: str, product: str,
                         start: datetime.date, end: datetime.date) -> str:
    company = (company or "").replace("\\", "\\\\").replace("'", "''").strip()
    product = (product or "").upper().replace("\\", "\\\\").replace("'", "''").strip()
    return f"""
SELECT
    td.hs_code AS HS_CODE,
    COUNT(*) AS RECORD_COUNT,
    MIN(td.product_description_norm) AS SAMPLE_DESCRIPTION
FROM {table} td
WHERE td.shipping_date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'
  AND (
        td.seller_company = '{company}'
        OR td.buyer_company = '{company}'
      )
  AND td.product_description_norm MATCH_ALL '{product}'
  AND td.hs_code IS NOT NULL
  AND td.hs_code <> ''
GROUP BY td.hs_code
ORDER BY RECORD_COUNT DESC
""".strip()


def search_company_hs(run_sql: Callable, table: str, company: str, product: str,
                      start: datetime.date, end: datetime.date) -> Dict[str, Any]:
    sql = build_company_hs_sql(table, company, product, start, end)
    t0 = time.time()
    ok, rows, _cols, err = run_sql(sql)
    out = {"sql": sql, "ok": bool(ok), "error": err or "", "rows": [],
           "elapsed_ms": int((time.time() - t0) * 1000)}
    if not ok:
        return out
    for r in rows or []:
        code = _digits(_row_get(r, "HS_CODE", "hs_code"))
        if not code:
            continue
        try:
            cnt = int(_row_get(r, "RECORD_COUNT") or 0)
        except Exception:
            cnt = 0
        out["rows"].append({
            "hs_code": code,
            "record_count": cnt,
            "sample_description": str(_row_get(r, "SAMPLE_DESCRIPTION") or "")[:160],
        })
    return out


# =========================================================
# SONAR CALLS
# =========================================================
# =========================================================
# STEP 0: EXPLICIT HS PARSING AND LINKING
# =========================================================
_MARKER_RE = re.compile(
    r"(?i)(?<![A-Z0-9])(?P<exact>exact\s+)?hs"
    r"(?:\s*(?:codes?|chapters?|headings?|prefix(?:es)?))?\s*[-:#]?\s*(?=\d)"
)
_CODE_GROUP_RE = re.compile(r"\s*(\d[\d.\s/\-\u2013\u2014]*\d|\d)(?:\s*(?i:to)\s*(\d{1,2})(?!\d))?")
_LABEL_RE = re.compile(r"\s*\(([^()]*)\)")
_LIST_SEP_RE = re.compile(r"(?i)\s*(?:(?:,|;|&)\s*(?:(?:and|or)\b\s*)?|(?:and|or)\b\s*)(?=\d)")
_CONJ_BEFORE_RE = re.compile(r"(?i)\s*(?:,|&|\band\b|\bor\b)\s*(?:and\s+)?$")
_BARE_PAREN_RE = re.compile(r"\(\s*(\d[\d.\s/,]*\d)\s*\)")
_DASH_CODE_RE = re.compile(r"(?<=[A-Za-z])\s*[-\u2013\u2014]\s*(\d[\d.\s]*\d)(?![\d.])")
_SHARED_SCOPE_RE = re.compile(r"(?is)([^,;:.!?]{0,240})\b(?:in|under|within|using)\s*$")

WRAPPER_WORDS = {
    "PRODUCT", "PRODUCTS", "GOODS", "ITEM", "ITEMS", "ELECTRONIC", "ELECTRONICS",
    "BRANDED", "CATEGORY", "CATEGORIES", "MERCHANDISE", "COMMODITY", "COMMODITIES",
    "PORTFOLIO", "RANGE", "LINEUP",
}


def _expand_codes(text: str, to_end: Optional[str] = None) -> List[str]:
    """'8471.41/8471.49' -> two codes; '50-63' -> 50..63; '6306 22 00' -> 63062200."""
    out: List[str] = []
    parts = [p for p in re.split(r"/", text or "") if p.strip()]
    for idx, part in enumerate(parts):
        rng = re.fullmatch(r"\s*(\d{1,2})\s*[-\u2013\u2014]\s*(\d{1,2})\s*", part)
        if rng:
            a, b = int(rng.group(1)), int(rng.group(2))
            if a <= b:
                out.extend(str(n).zfill(2) for n in range(a, b + 1))
                continue
        digits = _digits(part)
        if not digits or len(digits) > 18:
            continue
        if to_end and idx == len(parts) - 1 and len(digits) <= 2:
            a, b = int(digits), int(to_end)
            if a <= b:
                out.extend(str(n).zfill(2) for n in range(a, b + 1))
                continue
        out.append(digits.zfill(2) if len(digits) == 1 else digits)
    return _dedupe(out)


def _is_year(digits: str) -> bool:
    return len(digits) == 4 and 1900 <= int(digits) <= 2100


def _scan_explicit_items(raw: str) -> List[Dict[str, Any]]:
    """Every explicit HS mention with its span, codes, optional label and anchor."""
    items: List[Dict[str, Any]] = []
    consumed: List[Tuple[int, int]] = []

    def taken(a: int, b: int) -> bool:
        return any(a < e and b > s for s, e in consumed)

    # (1) "HS ..." marker lists, with optional "(label)" after each code group
    for marker in _MARKER_RE.finditer(raw):
        if taken(marker.start(), marker.end()):
            continue
        pos = marker.end()
        exact = bool(marker.group("exact"))
        first = True
        list_id = marker.start()
        while True:
            g = _CODE_GROUP_RE.match(raw, pos)
            if not g:
                break
            codes = _expand_codes(g.group(1), g.group(2))
            item_start = marker.start() if first else g.start(1)
            pos = g.end()
            label, label_span = "", None
            lm = _LABEL_RE.match(raw, pos)
            if lm and re.search(r"[A-Za-z]", lm.group(1)):
                label, label_span = lm.group(1).strip(), (lm.start(1), lm.end(1))
                pos = lm.end()
            if codes:
                items.append({
                    "kind": "marker", "codes": codes, "exact": exact, "list_id": list_id,
                    "start": item_start, "end": pos, "label": label, "label_span": label_span,
                    "anchor": marker.start() if first else g.start(1),
                    "text": raw[item_start:pos].strip(),
                })
            first = False
            sep = _LIST_SEP_RE.match(raw, pos)
            if not sep:
                break
            pos = sep.end()
        consumed.append((marker.start(), pos))

    # (2) bare digits in brackets after a product: "bananas (080390110000)"
    for m in _BARE_PAREN_RE.finditer(raw):
        if taken(m.start(), m.end()):
            continue
        codes = [c for c in _expand_codes(m.group(1).replace(",", "/")) if len(c) >= 4 and not _is_year(c)]
        if codes:
            items.append({"kind": "paren", "codes": codes, "exact": False, "start": m.start(), "end": m.end(),
                          "label": "", "label_span": None, "anchor": m.start(), "text": m.group(0)})
            consumed.append((m.start(), m.end()))

    # (3) "rice - 1080"
    for m in _DASH_CODE_RE.finditer(raw):
        if taken(m.start(1), m.end(1)):
            continue
        digits = _digits(m.group(1))
        if len(digits) < 2 or _is_year(digits):
            continue
        items.append({"kind": "dash", "codes": [digits], "exact": False, "start": m.start(), "end": m.end(),
                      "label": "", "label_span": None, "anchor": m.start(), "text": m.group(0).strip()})
        consumed.append((m.start(), m.end()))

    items.sort(key=lambda x: x["start"])
    return items


def explicit_mentions(text: str) -> List[Dict[str, Any]]:
    """Old extract_explicit_hs_mentions() shape: [{start, end, text, codes, exact_codes}]."""
    raw = text or ""
    out = [{
        "start": it["start"], "end": it["end"], "text": it["text"],
        "codes": list(it["codes"]), "exact_codes": list(it["codes"]) if it["exact"] else [],
    } for it in _scan_explicit_items(raw)]
    if not out and re.fullmatch(r"\s*\d{2,18}\s*", raw):
        code = raw.strip()
        out.append({"start": 0, "end": len(raw), "text": code, "codes": [code], "exact_codes": []})
    return out


def _term_occurrences(raw_upper: str, term: str) -> List[Tuple[int, int]]:
    t = re.sub(r"\s+", " ", (term or "").strip().upper())
    if not t:
        return []
    pat = r"(?<![A-Z0-9])" + re.escape(t).replace(r"\ ", r"[\s\-]+") + r"(?:E?S)?(?![A-Z0-9])"
    return [(m.start(), m.end()) for m in re.finditer(pat, raw_upper)]


def is_wrapper_term(term: str) -> bool:
    words = set(re.findall(r"[A-Z]+", (term or "").upper()))
    return bool(words & WRAPPER_WORDS)


def parse_explicit_hs(raw: str, product_terms: List[str], normalize_key: Callable) -> Dict[str, Any]:
    """Link every explicit code to its own product term (no LLM).

    Returns {links: {KEY: [codes]}, link_kind: {KEY: how}, shared: {KEY: [codes]},
             hs_only: [codes], wrappers: [terms], exact_codes: [codes], items: [...], all_codes}.
    """
    raw = raw or ""
    up = raw.upper()
    items = _scan_explicit_items(raw)
    terms = _dedupe([re.sub(r"\s+", " ", t.strip()) for t in product_terms or [] if isinstance(t, str) and t.strip()])
    occ = {t: _term_occurrences(up, t) for t in terms}
    links: Dict[str, List[str]] = {}
    link_kind: Dict[str, str] = {}
    unlinked_items: List[Dict[str, Any]] = []

    def link(term: str, codes: List[str], how: str) -> None:
        key = normalize_key(term)
        links.setdefault(key, [])
        for c in codes:
            if c not in links[key]:
                links[key].append(c)
        link_kind.setdefault(key, how)

    list_terms: Dict[int, List[str]] = {}

    def conj_group(term: str, start: int) -> List[str]:
        """'rice and wheat HS ...': the product just before the code plus the ones joined to it."""
        group, cursor = [term], start
        while True:
            m = _CONJ_BEFORE_RE.search(up[:cursor])
            if not m:
                break
            prev = None
            for t in terms:
                for s_, e_ in occ[t]:
                    if e_ == m.start() and t not in group:
                        prev = (t, s_)
            if not prev:
                break
            group.append(prev[0])
            cursor = prev[1]
        return group

    for it in items:
        linked = False
        # code first: "HS 8517.62 (wearables such as apple watch)"
        if it["label_span"]:
            a, b = it["label_span"]
            for t in terms:
                if any(s >= a and e <= b for s, e in occ[t]):
                    link(t, it["codes"], "label")
                    linked = True
        if linked:
            continue
        if it.get("list_id") is not None and it["list_id"] in list_terms:
            for t in list_terms[it["list_id"]]:
                link(t, it["codes"], "product_first")
            continue
        # product first: "rice (HS 1006)", "rice HS 1006", "bananas (0803...)", "rice - 1080"
        anchor = it["anchor"]
        best, best_gap = None, None
        for t in terms:
            for s, e in occ[t]:
                if e <= anchor:
                    gap = up[e:anchor]
                    if len(gap) <= 6 and re.fullmatch(r"[\s(\-\u2013\u2014:,]*", gap):
                        if best_gap is None or len(gap) < best_gap:
                            best, best_gap = t, len(gap)
        if best:
            best_start = max(s_ for s_, e_ in occ[best] if e_ <= anchor)
            group = conj_group(best, best_start)
            for t in group:
                link(t, it["codes"], "product_first")
            if it.get("list_id") is not None:
                list_terms[it["list_id"]] = group
            continue
        # shared: "coffee and tea in HS 09"
        before = up[:it["start"]]
        ms = _SHARED_SCOPE_RE.search(before)
        if ms:
            clause_start = ms.start(1)
            scoped = [t for t in terms if any(s >= clause_start and e <= it["start"] for s, e in occ[t])]
            if scoped:
                for t in scoped:
                    link(t, it["codes"], "shared_scope")
                continue
        unlinked_items.append(it)

    pool = _dedupe([c for it in unlinked_items for c in it["codes"]])
    all_codes = _dedupe([c for it in items for c in it["codes"]])

    wrappers: List[str] = []
    no_code_terms: List[str] = []
    for t in terms:
        if normalize_key(t) in links:
            continue
        if all_codes and is_wrapper_term(t):
            wrappers.append(t)
        else:
            no_code_terms.append(t)

    # single product + codes anywhere: the codes are that product's
    if pool and len(terms) == 1 and no_code_terms:
        link(no_code_terms[0], pool, "single_product")
        no_code_terms, pool = [], []

    shared: Dict[str, List[str]] = {}
    hs_only: List[str] = []
    if pool:
        if no_code_terms:
            for t in no_code_terms:
                shared[normalize_key(t)] = list(pool)
        else:
            hs_only = list(pool)

    return {
        "links": links,
        "link_kind": link_kind,
        "shared": shared,
        "hs_only": hs_only,
        "wrappers": wrappers,
        "exact_codes": _dedupe([c for it in items if it["exact"] for c in it["codes"]]),
        "all_codes": all_codes,
        "items": [{k: it[k] for k in ("kind", "text", "codes", "label", "exact")} for it in items],
    }


# =========================================================
# SONAR CALLS
# =========================================================
VALIDATION_PROMPT = """You validate HS codes for a trade-data product filter.

PRODUCT the user asked about: "{product}"
USER QUESTION: {question}
CONVERSATION / FLOW CONTEXT:
{context}

The codes below were found in the company's own shipment records because the word(s)
of the product appear in the shipment description. Each row: full HS code as stored
(national extension), number of shipment rows, one sample description.

CANDIDATES (JSON):
{candidates}

Decide for EVERY candidate code whether the goods under it ARE this product, and group
the kept codes by meaning.

Rules:
1. KEEP a code only if the goods classified under it ARE the product itself, including its
   varieties, grades and simple forms (for "rice": paddy, husked, milled, broken rice).
2. If the user's wording narrows the product ("basmati rice"), keep only codes for that.
3. REJECT codes for different articles that merely mention the product word: appliances
   (rice cooker), tableware (rice bowl), utensils, prepared foods, snacks, flavours,
   by-products, unless the user's wording itself names that article.
4. The product word may have several unrelated meanings ("apple": fruit vs Apple-brand
   electronics). Keep the codes of EVERY real meaning and put each kept code in a
   "group" named after its meaning, e.g. "apple (fruit)", "Apple brand electronics".
   Use the same group name for all codes of one meaning. One meaning = one group.
5. "context_group": if the question or context clearly shows which meaning the user means
   (e.g. "mango, apple" -> fruit; "apple phone" -> electronics), give that group's name.
   Otherwise null. Do NOT use record counts to decide this.
6. Judge each code by the meaning of its first 6 digits in the HS nomenclature AND its
   sample description. Use the code strings exactly as given; never invent codes.
7. Every candidate code must appear exactly once, in "kept" or in "rejected".

Return ONLY this JSON (no prose, no markdown, no citations):
{{"kept": [{{"hs_code": "<code>", "group": "<meaning>", "reason": "<short>"}}],
  "rejected": [{{"hs_code": "<code>", "reason": "<short>"}}],
  "context_group": "<group name>" | null}}
"""

WEB_HS6_PROMPT = """You map a product to HS (Harmonized System, 2022) 6-digit subheadings.

PRODUCT: "{product}"
USER QUESTION: {question}
OTHER PRODUCTS IN THIS QUESTION (their codes are NOT for this product):
{others}
CONVERSATION / FLOW CONTEXT:
{context}

Return codes ONLY for "{product}". The question may name other products and HS codes;
never return those codes for this product.

Using the question and the context to understand what the product means here
(e.g. "oil" in a cooking / restaurant / biryani context means edible vegetable oil),
return the 6-digit HS subheadings that cover THIS product itself (its main forms,
e.g. crude and refined), not articles made from it or used with it.

- If the product is a broad category ("grains", "spices", "frozen food"), return the
  subheadings of its main products, at most {top} codes.
- If it is not a tradeable physical good (water, labour, services, rent, salary),
  return status "NOT_GOODS".
- If the context genuinely cannot decide between unrelated meanings, return "AMBIGUOUS".
- Codes must be exactly 6 digits. At most {top} codes, most relevant first.

Return ONLY this JSON (no prose, no markdown, no citations):
{{"status": "OK" | "NOT_GOODS" | "AMBIGUOUS",
  "interpretation": "<what the product means here, few words>",
  "hs6": [{{"code": "<6 digits>", "description": "<short>"}}]}}
"""

MEANING_PROMPT = """A product word was resolved to HS codes earlier, with these meanings:
{meanings}

PRODUCT: "{product}"
USER QUESTION: {question}
CONVERSATION / FLOW CONTEXT:
{context}

Which meaning does the user mean NOW?
- Give the meaning name exactly as listed if the question/context shows it.
- "DEFAULT" if the question/context does not show which meaning is meant.
- "NEW" if the question/context clearly means a different meaning that is not listed
  (e.g. listed only "apple (fruit)" but the user asks about Apple iPhones).

Return ONLY this JSON (no prose, no markdown, no citations):
{{"choice": "<meaning name>" | "DEFAULT" | "NEW", "reason": "<short>"}}
"""


def _choose_group(groups: Dict[str, Dict[str, Any]], context_group: Optional[str]) -> Tuple[str, str]:
    """Context first, then most records, tie -> random."""
    if context_group and context_group in groups:
        return context_group, "context"
    best = max(g["record_count"] for g in groups.values())
    tied = [name for name, g in groups.items() if g["record_count"] == best]
    if len(tied) == 1:
        return tied[0], "record count"
    return random.choice(tied), "record count tie (random)"


def validate_candidates(call_model: Callable, product: str, question: str, context: str,
                        candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    cands = candidates[:HS_VALIDATOR_MAX_CANDIDATES]
    prompt = VALIDATION_PROMPT.format(
        product=product, question=question or "(none)", context=context or "(none)",
        candidates=json.dumps(cands, ensure_ascii=False),
    )
    out: Dict[str, Any] = {
        "model": HS_VALIDATOR_MODEL, "status": "ERROR", "kept": [], "rejected": [],
        "groups": [], "chosen_group": "", "choice_reason": "", "context_group": None,
        "error": "", "raw": "", "candidates_sent": len(cands), "candidates_total": len(candidates),
    }
    t0 = time.time()
    try:
        raw = call_model(prompt, HS_VALIDATOR_MAX_TOKENS, HS_VALIDATOR_MODEL)
        out["raw"] = (raw or "")[:4000]
        data = _parse_json_loose(raw)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["elapsed_ms"] = int((time.time() - t0) * 1000)
        return out
    out["elapsed_ms"] = int((time.time() - t0) * 1000)

    counts = {c["hs_code"]: c.get("record_count", 0) for c in cands}
    kept, rejected = [], []
    for item in data.get("kept") or []:
        code = _digits((item or {}).get("hs_code"))
        if code in counts and code not in {k["hs_code"] for k in kept}:
            group = re.sub(r"\s+", " ", str((item or {}).get("group") or product)).strip()[:80]
            kept.append({"hs_code": code, "group": group, "record_count": counts[code],
                         "reason": str((item or {}).get("reason") or "")[:200]})
    kept_set = {k["hs_code"] for k in kept}
    for item in data.get("rejected") or []:
        code = _digits((item or {}).get("hs_code"))
        if code in counts and code not in kept_set:
            rejected.append({"hs_code": code, "record_count": counts[code],
                             "reason": str((item or {}).get("reason") or "")[:200]})
    judged = kept_set | {r["hs_code"] for r in rejected}
    for c in cands:
        if c["hs_code"] not in judged:
            rejected.append({"hs_code": c["hs_code"], "record_count": c.get("record_count", 0),
                             "reason": "not returned by validator"})
    out["kept"], out["rejected"] = kept, rejected
    if not kept:
        out["status"] = "NONE"
        return out

    groups: Dict[str, Dict[str, Any]] = {}
    for k in kept:
        g = groups.setdefault(k["group"], {"meaning": k["group"], "record_count": 0, "codes": []})
        g["record_count"] += int(k["record_count"] or 0)
        g["codes"].append((k["hs_code"], int(k["record_count"] or 0)))
    for g in groups.values():
        g["codes"].sort(key=lambda x: -x[1])
        g["hs_codes"] = [c for c, _ in g["codes"][:HS_GROUP_TOP_CODES]]
        g["codes_total"] = len(g["codes"])
        del g["codes"]
    ctx_group = data.get("context_group")
    ctx_group = str(ctx_group).strip() if ctx_group else None
    chosen, why = _choose_group(groups, ctx_group)
    out.update({
        "status": "OK",
        "context_group": ctx_group,
        "chosen_group": chosen,
        "choice_reason": why,
        "groups": sorted(groups.values(), key=lambda g: -g["record_count"]),
    })
    return out


def _hs6(code: str) -> str:
    return code[:6] if len(code) >= 6 else ""


def web_lookup_hs6(call_model: Callable, product: str, question: str, context: str,
                   other_products: Optional[List[Tuple[str, List[str]]]] = None) -> Dict[str, Any]:
    others = other_products or []
    others_text = "\n".join(f"- {t}: HS {', '.join(c) or '(not resolved yet)'}" for t, c in others) or "(none)"
    prompt = WEB_HS6_PROMPT.format(
        product=product, question=question or "(none)", context=context or "(none)",
        top=HS_GROUP_TOP_CODES, others=others_text,
    )
    out: Dict[str, Any] = {"model": HS_WEB_MODEL, "status": "ERROR", "interpretation": "",
                           "hs6": [], "error": "", "raw": ""}
    t0 = time.time()
    try:
        raw = call_model(prompt, HS_WEB_MAX_TOKENS, HS_WEB_MODEL)
        out["raw"] = (raw or "")[:3000]
        data = _parse_json_loose(raw)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["elapsed_ms"] = int((time.time() - t0) * 1000)
        return out
    out["elapsed_ms"] = int((time.time() - t0) * 1000)

    codes: List[Dict[str, str]] = []
    for item in data.get("hs6") or []:
        code = _digits((item or {}).get("code") if isinstance(item, dict) else item)
        if len(code) == 6 and code not in {c["code"] for c in codes}:
            desc = (item or {}).get("description") if isinstance(item, dict) else ""
            codes.append({"code": code, "description": str(desc or "")[:160]})
    other_hs6 = {_hs6(c): t for t, cs in others for c in cs if _hs6(c)}
    removed = [{"code": c["code"], "belongs_to": other_hs6[c["code"]]} for c in codes if c["code"] in other_hs6]
    codes = [c for c in codes if c["code"] not in other_hs6]
    out["removed_other_product_codes"] = removed
    codes = codes[:HS_GROUP_TOP_CODES]
    status = str(data.get("status") or "").upper().strip()
    if status not in {"OK", "NOT_GOODS", "AMBIGUOUS"}:
        status = "OK" if codes else "NONE"
    if status == "OK" and not codes:
        status = "NONE"
    if status != "OK":
        codes = []
    out.update({"status": status, "interpretation": str(data.get("interpretation") or "")[:200], "hs6": codes})
    return out


def pick_cached_meaning(call_model: Callable, product: str, question: str, context: str,
                        meanings: List[Dict[str, Any]]) -> Dict[str, Any]:
    listing = "\n".join(f'- "{m["meaning"]}": HS {", ".join(m.get("hs_codes") or [])}' for m in meanings)
    prompt = MEANING_PROMPT.format(meanings=listing, product=product,
                                   question=question or "(none)", context=context or "(none)")
    out = {"model": HS_MEANING_MODEL, "choice": "DEFAULT", "reason": "", "error": ""}
    try:
        data = _parse_json_loose(call_model(prompt, HS_MEANING_MAX_TOKENS, HS_MEANING_MODEL))
        choice = str(data.get("choice") or "DEFAULT").strip()
        names = {m["meaning"] for m in meanings}
        if choice not in names and choice.upper() not in {"DEFAULT", "NEW"}:
            choice = "DEFAULT"
        out["choice"] = choice if choice in names else choice.upper()
        out["reason"] = str(data.get("reason") or "")[:200]
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# =========================================================
# SESSION: CACHE / FLOW CONTEXT / TRACE
# =========================================================
def _user_profile(session: Dict[str, Any]) -> Any:
    user_memory = (session.get("active_focus") or {}).get("user_memory") or {}
    if isinstance(user_memory, dict):
        return user_memory.get("user_product_info")
    return None


def sync_cache_company(session: Dict[str, Any]) -> bool:
    """Clear product_hs_memory when the company profile changes. True if cleared."""
    focus = session.setdefault("active_focus", {})
    company = company_name_from_profile(_user_profile(session)) or ""
    stored = focus.get(CACHE_COMPANY_KEY)
    if stored is None:
        focus[CACHE_COMPANY_KEY] = company
        if company and focus.get("product_hs_memory"):
            focus["product_hs_memory"] = {}
            return True
        return False
    if stored != company:
        focus["product_hs_memory"] = {}
        focus[CACHE_COMPANY_KEY] = company
        focus[TRACE_KEY] = None
        return True
    return False


def set_flow_context(session: Dict[str, Any], ctx: Optional[Dict[str, Any]]) -> None:
    session.setdefault("active_focus", {})[FLOW_CTX_KEY] = dict(ctx or {})


def get_flow_context(session: Dict[str, Any]) -> Dict[str, Any]:
    return dict((session.get("active_focus") or {}).get(FLOW_CTX_KEY) or {})


def cache_view(session: Dict[str, Any]) -> Dict[str, Any]:
    """HS cache panel rows: one row per product meaning."""
    focus = session.get("active_focus") or {}
    rows = []
    for key, item in (focus.get("product_hs_memory") or {}).items():
        product = item.get("product_term") or key
        meanings = item.get("meanings") or []
        if meanings:
            for m in meanings:
                rows.append({
                    "product": product,
                    "meaning": m.get("meaning") or "",
                    "hs_codes": ", ".join(m.get("hs_codes") or []),
                    "records": m.get("record_count"),
                    "default": "yes" if m.get("meaning") == item.get("default_meaning") else "",
                    "source": SOURCE_LABELS.get(m.get("source"), m.get("source") or ""),
                })
        else:
            rows.append({
                "product": product, "meaning": "",
                "hs_codes": ", ".join(item.get("selected_hs_codes") or []),
                "records": None, "default": "yes",
                "source": SOURCE_LABELS.get(item.get("source"), item.get("source") or "HS domain picker"),
            })
    return {"company": focus.get(CACHE_COMPANY_KEY) or "", "rows": rows}


def _entry(trace: Dict[str, Any], product: str) -> Dict[str, Any]:
    for e in trace["products"]:
        if e["product"] == product:
            return e
    e = {"product": product, "steps": [], "final_codes": [], "source": "", "meaning": "", "status": "PENDING"}
    trace["products"].append(e)
    return e


def _step(entry: Dict[str, Any], step: str, outcome: str, note: str = "", **detail: Any) -> None:
    rec = {"step": step, "outcome": outcome, "note": note}
    if detail:
        rec["detail"] = detail
    entry["steps"].append(rec)


def _finish(entry: Dict[str, Any], codes: List[str], source: str, meaning: str = "") -> None:
    entry["final_codes"] = list(codes)
    entry["source"] = SOURCE_LABELS.get(source, source)
    entry["meaning"] = meaning
    entry["status"] = "RESOLVED"


def current_trace(session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return (session.get("active_focus") or {}).get(TRACE_KEY)


def pop_trace(session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    focus = session.setdefault("active_focus", {})
    trace = focus.get(TRACE_KEY)
    focus[TRACE_KEY] = None
    return trace


def tag_cache(session: Dict[str, Any], key: str, source: str) -> None:
    """Mark the source of a product_hs_memory entry written outside this module."""
    focus = session.get("active_focus") or {}
    item = (focus.get("product_hs_memory") or {}).get(key)
    if item is not None:
        item["source"] = source
        item["company"] = focus.get(CACHE_COMPANY_KEY) or ""
        item["resolved_at"] = _now_iso()
        item.pop("meanings", None)
        item.pop("default_meaning", None)


def ignored_keys(session: Dict[str, Any]) -> List[str]:
    """Product keys the picker must skip: over the cap, dropped, or already found to have no data."""
    focus = session.get("active_focus") or {}
    return list(focus.get(IGNORED_KEY) or []) + list(focus.get(UNSCOPED_KEY) or [])


def mark_unscoped(session: Dict[str, Any], key: str) -> None:
    focus = session.setdefault("active_focus", {})
    focus.setdefault(UNSCOPED_KEY, [])
    if key not in focus[UNSCOPED_KEY]:
        focus[UNSCOPED_KEY].append(key)


def record_picker(session: Dict[str, Any], normalize_key: Callable, product: str,
                  codes: List[str], how: str, domains: Optional[List[str]] = None) -> None:
    """Called by the old HS2 picker path (user pick / single-domain auto pick / no data)."""
    focus = session.setdefault("active_focus", {})
    key = normalize_key(product)
    item = (focus.get("product_hs_memory") or {}).get(key)
    if item is not None and codes:
        item["source"] = SRC_PICKER
        item["company"] = focus.get(CACHE_COMPANY_KEY) or ""
        item["resolved_at"] = _now_iso()
    trace = focus.get(TRACE_KEY)
    if not trace:
        return
    entry = _entry(trace, product)
    _step(entry, "PICKER", "HIT" if codes else "MISS", how, hs_codes=list(codes or []), domains=domains or [])
    if codes:
        _finish(entry, codes, SRC_PICKER)
    else:
        entry["status"] = "UNSCOPED"


def note_step(session: Dict[str, Any], product: str, step: str, outcome: str, note: str = "",
              status: Optional[str] = None, **detail: Any) -> None:
    """Append a step to the current trace (no-op when no trace is active)."""
    trace = (session.get("active_focus") or {}).get(TRACE_KEY)
    if not trace:
        return
    entry = _entry(trace, product)
    _step(entry, step, outcome, note, **detail)
    if status:
        entry["status"] = status


def _context_text(session: Dict[str, Any], recent_turns: List[Dict[str, str]]) -> str:
    lines = []
    flow = get_flow_context(session)
    if flow.get("original_question"):
        lines.append(f"Original user question: {flow['original_question']}")
    for p in flow.get("parents") or []:
        lines.append(f"Earlier step ({p.get('route')}): asked {p.get('asked', '')!r}; "
                     f"answered: {str(p.get('answer') or '')[:900]}")
    for t in (recent_turns or [])[-6:]:
        lines.append(f"{t.get('role')}: {str(t.get('content') or '')[:300]}")
    text = "\n".join(lines)
    return text[-HS_CONTEXT_MAX_CHARS:] if len(text) > HS_CONTEXT_MAX_CHARS else text


def _cache_lookup(memory: Dict[str, Any], key: str) -> Optional[str]:
    """Exact key, else a key that is the same product ignoring plurals (rice / rices)."""
    if key in memory:
        return key
    target = normalize_item_phrase(key)
    for k in memory:
        if normalize_item_phrase(k) == target:
            return k
    return None


# =========================================================
# LADDER
# =========================================================
def _remote_ladder(call_model: Callable, run_sql: Callable, table: str, company: Optional[str],
                   term: str, query: str, context: str, start: datetime.date, end: datetime.date,
                   wanted_new_meaning: bool,
                   other_products: Optional[List[Tuple[str, List[str]]]] = None) -> Dict[str, Any]:
    """COMPANY_DATA then WEB for one term. Pure (no session access) so it can run in a thread."""
    steps: List[Dict[str, Any]] = []
    result: Dict[str, Any] = {"term": term, "steps": steps, "outcome": None}

    if company:
        res = search_company_hs(run_sql, table, company, term, start, end)
        if not res["ok"]:
            steps.append({"step": "COMPANY_DATA", "outcome": "ERROR", "note": res["error"][:300],
                          "detail": {"sql": res["sql"]}})
        elif not res["rows"]:
            steps.append({"step": "COMPANY_DATA", "outcome": "MISS",
                          "note": f"0 shipments of {company} match this product",
                          "detail": {"sql": res["sql"], "elapsed_ms": res["elapsed_ms"]}})
        else:
            v = validate_candidates(call_model, term, query, context, res["rows"])
            detail = {
                "sql": res["sql"], "elapsed_ms": res["elapsed_ms"], "candidates": res["rows"],
                "validator": {k: v.get(k) for k in (
                    "model", "status", "kept", "rejected", "groups", "context_group", "chosen_group",
                    "choice_reason", "error", "elapsed_ms", "candidates_sent", "candidates_total")},
            }
            if v["status"] == "OK" and v["groups"]:
                chosen = next(g for g in v["groups"] if g["meaning"] == v["chosen_group"])
                note = (f"{len(v['groups'])} meaning(s); picked \"{chosen['meaning']}\" by {v['choice_reason']}"
                        f" ({chosen['record_count']} records, {len(chosen['hs_codes'])} code(s))")
                steps.append({"step": "COMPANY_DATA", "outcome": "HIT", "note": note, "detail": detail})
                result["outcome"] = {"source": SRC_COMPANY_DATA, "groups": v["groups"],
                                     "chosen": chosen["meaning"]}
                return result
            note = {"NONE": "no candidate code is the product itself",
                    "ERROR": "validator call failed: " + (v.get("error") or "")}.get(v["status"], v["status"])
            steps.append({"step": "COMPANY_DATA", "outcome": "MISS", "note": note, "detail": detail})
    else:
        steps.append({"step": "COMPANY_DATA", "outcome": "SKIPPED",
                      "note": "no company profile / company_name"})

    w = web_lookup_hs6(call_model, term, query, context, other_products)
    wdetail = {k: w.get(k) for k in ("model", "status", "interpretation", "hs6", "removed_other_product_codes",
                                     "error", "elapsed_ms")}
    if w["status"] == "OK" and w["hs6"]:
        codes = [c["code"] for c in w["hs6"]]
        meaning = w.get("interpretation") or term
        steps.append({"step": "WEB", "outcome": "HIT", "note": f"Sonar mapped to {len(codes)} HS6 code(s)",
                      "detail": wdetail})
        result["outcome"] = {"source": SRC_WEB, "chosen": meaning,
                             "groups": [{"meaning": meaning, "hs_codes": codes, "record_count": None}]}
        return result
    if w["status"] == "NOT_GOODS":
        steps.append({"step": "WEB", "outcome": "SKIPPED", "note": "not a physical good: no HS scope",
                      "detail": wdetail})
        result["outcome"] = {"drop": True}
        return result
    steps.append({"step": "WEB", "outcome": "MISS",
                  "note": w.get("error") or f"status {w['status']}", "detail": wdetail})
    return result


def _default_meaning(meanings: List[Dict[str, Any]], fallback: str) -> str:
    """Default = the meaning with the most records (ties keep the chosen one)."""
    best = max((m.get("record_count") or 0) for m in meanings)
    top = [m["meaning"] for m in meanings if (m.get("record_count") or 0) == best]
    return fallback if fallback in top else top[0]


def _merge_meanings(old: List[Dict[str, Any]], new: List[Dict[str, Any]], source: str) -> List[Dict[str, Any]]:
    by_name = {m["meaning"]: dict(m) for m in old or []}
    for g in new:
        by_name[g["meaning"]] = {"meaning": g["meaning"], "hs_codes": list(g["hs_codes"]),
                                 "record_count": g.get("record_count"), "source": source}
    return list(by_name.values())


def run_layers(
    session: Dict[str, Any],
    query: str,
    product_terms: List[str],
    explicit_map: Dict[str, List[str]],
    *,
    save_scope: Callable,
    normalize_key: Callable,
    run_sql: Callable,
    call_model: Callable,
    table: str,
    recent_turns: List[Dict[str, str]],
    log: Optional[Callable] = None,
    parse: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve product terms. Returns {unresolved (for picker), ignored, dropped, trace}.

    explicit_map: product KEY -> codes the user gave (step 0). Resolved terms are saved
    into session product_hs_memory, so the picker / filter contract use them unchanged.
    """
    focus = session.setdefault("active_focus", {})
    focus.setdefault("product_hs_memory", {})
    sync_cache_company(session)
    memory = focus["product_hs_memory"]

    profile = _user_profile(session)
    has_profile = profile not in (None, "", {}, [])
    company = company_name_from_profile(profile) if has_profile else None
    flow_ctx = get_flow_context(session)
    start, end = company_search_window()
    context = _context_text(session, recent_turns)
    parse = parse or {}

    trace: Dict[str, Any] = {
        "query": query,
        "company": company or "",
        "has_profile": has_profile,
        "window": [start.isoformat(), end.isoformat()],
        "derived_from_flow": bool(flow_ctx.get("derived")),
        "flow_parent_routes": [p.get("route") for p in flow_ctx.get("parents") or []],
        "max_products": HS_LAYER_MAX_PRODUCTS,
        "explicit_parse": {k: parse.get(k) for k in ("items", "hs_only", "wrappers", "link_kind")} if parse else {},
        "products": [],
        "ignored": [],
        "dropped": [],
        "started_at": _now_iso(),
    }

    def save(term: str, groups: List[Dict[str, Any]], chosen: str, source: str) -> List[str]:
        key = normalize_key(term)
        old = (memory.get(key) or {}).get("meanings") or []
        old_default = (memory.get(key) or {}).get("default_meaning")
        meanings = _merge_meanings(old, groups, source)
        codes = next(m["hs_codes"] for m in meanings if m["meaning"] == chosen)
        save_scope(session, term, [{"domain": f"{SOURCE_LABELS[source]}: {chosen}", "selected_hs_codes": list(codes)}])
        item = memory.get(key)
        if item is not None:
            item.update({
                "source": source, "company": company or "", "resolved_at": _now_iso(),
                "meanings": meanings, "active_meaning": chosen,
                "default_meaning": old_default if (old_default and any(m["meaning"] == old_default for m in meanings))
                else _default_meaning(meanings, chosen),
            })
        return codes

    terms = _dedupe([re.sub(r"\s+", " ", t.strip()) for t in product_terms or [] if isinstance(t, str) and t.strip()])
    explicit_map = explicit_map or {}
    shared_keys = set((parse.get("shared") or {}).keys())

    pending: List[Tuple[str, bool]] = []          # (term, wanted_new_meaning)
    for term in terms:
        key = normalize_key(term)
        entry = _entry(trace, term)
        if key in explicit_map and explicit_map[key]:
            codes = list(explicit_map[key])
            how = (parse.get("link_kind") or {}).get(key, "shared" if key in shared_keys else "")
            _step(entry, "EXPLICIT", "HIT", f"HS given in the question ({how or 'linked'}); trusted as-is",
                  hs_codes=codes)
            _finish(entry, codes, SRC_EXPLICIT_SHARED if key in shared_keys else SRC_EXPLICIT)
            continue
        ck = _cache_lookup(memory, key)
        if ck is None:
            _step(entry, "CACHE", "MISS")
            pending.append((term, False))
            continue
        item = memory[ck]
        meanings = item.get("meanings") or []
        if len(meanings) <= 1:
            codes = list(item.get("selected_hs_codes") or [])
            if not codes and meanings:
                codes = list(meanings[0].get("hs_codes") or [])
            _step(entry, "CACHE", "HIT", "resolved earlier in this session", hs_codes=codes,
                  source=SOURCE_LABELS.get(item.get("source"), item.get("source") or "HS domain picker"))
            if ck != key:
                save_scope(session, term, [{"domain": "Session cache", "selected_hs_codes": codes}])
                memory[key].update({k: v for k, v in item.items() if k not in ("product_term",)})
            _finish(entry, codes, item.get("source") or SRC_PICKER, (meanings[0]["meaning"] if meanings else ""))
            entry["source"] = "Session cache"
            continue
        pick = pick_cached_meaning(call_model, term, query, context, meanings)
        choice = pick["choice"]
        detail = {"meanings": meanings, "default": item.get("default_meaning"), "sonar": pick}
        if choice == "NEW":
            _step(entry, "CACHE", "MISS", "context points to a meaning not in the cache: resolving again", **detail)
            pending.append((term, True))
            continue
        if choice == "DEFAULT" or choice not in {m["meaning"] for m in meanings}:
            choice = item.get("default_meaning") or meanings[0]["meaning"]
            why = "context did not decide: default meaning"
        else:
            why = "meaning picked by context"
        codes = next(m["hs_codes"] for m in meanings if m["meaning"] == choice)
        source = next((m.get("source") for m in meanings if m["meaning"] == choice), item.get("source"))
        save(term, [{"meaning": m["meaning"], "hs_codes": m["hs_codes"], "record_count": m.get("record_count")}
                    for m in meanings if m["meaning"] == choice], choice, source or SRC_COMPANY_DATA)
        _step(entry, "CACHE", "HIT", f"{why}: \"{choice}\"", hs_codes=codes, **detail)
        _finish(entry, codes, source or SRC_COMPANY_DATA, choice)
        entry["source"] = "Session cache"

    capped, ignored = pending[:HS_LAYER_MAX_PRODUCTS], pending[HS_LAYER_MAX_PRODUCTS:]
    for term, _ in ignored:
        e = _entry(trace, term)
        e["status"] = "IGNORED"
        _step(e, "CAP", "IGNORED", f"only the first {HS_LAYER_MAX_PRODUCTS} products are resolved per question")
    trace["ignored"] = [t for t, _ in ignored]
    focus[IGNORED_KEY] = [normalize_key(t) for t, _ in ignored]
    focus[UNSCOPED_KEY] = []

    # ---- PROFILE (local, fast) ----
    remote: List[Tuple[str, bool]] = []
    for term, new_meaning in capped:
        entry = _entry(trace, term)
        if not has_profile:
            _step(entry, "PROFILE", "SKIPPED", "no company profile loaded")
            remote.append((term, new_meaning))
            continue
        pm = None if new_meaning else profile_exact_match(profile, term)
        if pm and pm["codes"]:
            _step(entry, "PROFILE", "HIT", "exact item match in company profile",
                  matched_items=pm["phrases"], hs_codes=pm["codes"])
            meaning = pm["phrases"][0]
            codes = save(term, [{"meaning": meaning, "hs_codes": pm["codes"], "record_count": None}], meaning, SRC_PROFILE)
            _finish(entry, codes, SRC_PROFILE, meaning)
            continue
        _step(entry, "PROFILE", "MISS" if not new_meaning else "SKIPPED",
              "no exact item match in company profile" if not new_meaning else "looking for a new meaning")
        remote.append((term, new_meaning))

    # ---- COMPANY_DATA + WEB (remote; terms in parallel) ----
    results: List[Dict[str, Any]] = []
    if remote:
        # Codes already given to / resolved for the other products of this question.
        known = {e["product"]: list(e["final_codes"]) for e in trace["products"] if e.get("final_codes")}
        all_terms = [e["product"] for e in trace["products"]]

        def others_for(term: str) -> List[Tuple[str, List[str]]]:
            return [(t, known.get(t, [])) for t in all_terms if t != term]

        args = [(call_model, run_sql, table, company, t, query, context, start, end, nm, others_for(t))
                for t, nm in remote]
        if len(args) == 1:
            results = [_remote_ladder(*args[0])]
        else:
            with ThreadPoolExecutor(max_workers=min(HS_PARALLEL_WORKERS, len(args))) as pool:
                results = list(pool.map(lambda a: _remote_ladder(*a), args))

    unresolved: List[str] = []
    dropped: List[str] = []
    for res in results:
        term = res["term"]
        entry = _entry(trace, term)
        entry["steps"].extend(res["steps"])
        out = res["outcome"]
        if out and out.get("drop"):
            entry["status"] = "DROPPED"
            dropped.append(term)
            continue
        if out:
            codes = save(term, out["groups"], out["chosen"], out["source"])
            _finish(entry, codes, out["source"], out["chosen"])
            continue
        entry["status"] = "TO_PICKER"
        unresolved.append(term)

    focus[IGNORED_KEY] = list(focus.get(IGNORED_KEY) or []) + [normalize_key(t) for t in dropped]
    trace["dropped"] = dropped
    trace["picker_products"] = unresolved
    focus[TRACE_KEY] = trace
    if log:
        try:
            log("HS RESOLUTION LAYER", trace)
        except Exception:
            pass
    return {"unresolved": unresolved, "ignored": [t for t, _ in ignored], "dropped": dropped, "trace": trace}