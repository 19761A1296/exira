# aggregate_company.py
"""Turns a shipment dataframe into a short block of facts for the card prompt.

The card holds distinct things — products, partners, ports, countries, units —
and never a figure, because PROMPT_USER_SUMMARY forbids counts, totals and
rankings. So the whole frame collapses to roughly the same size whether it has
2,000 rows or 2,000,000.

    from aggregate_company import aggregate

    facts = aggregate(df, "ABC EXPORTS")
    print(facts["text"])        # paste this into the prompt
    print(facts["stats"])       # row counts etc, for your own logging

Ranking here is a SELECTION device: it decides what to send when a list is long.
Do not let the card say "their biggest market is X" — the prompt bans that.

Run it directly to see it work on built-in sample data:

    python aggregate_company.py              # sample
    python aggregate_company.py rows.csv ABC # your own csv
"""

import sys

import pandas as pd

# how many entries of each kind to keep
MAX_PRODUCTS = 25
MAX_PARTNERS = 25
MAX_PORTS = 10
MAX_COUNTRIES = 15
MAX_ATTRS = 10
MAX_PER_COUNTRY = 5          # products listed per country
MAX_COUNTRY_ROWS = 10        # countries in the "by country" line


# ───────────────────────── helpers ─────────────────────────

def _norm(series: pd.Series) -> pd.Series:
    """Trim and upper-case, so 'Zara Trading ' and 'ZARA TRADING' are one thing."""
    return series.fillna("").astype(str).str.strip().str.upper()


def _has(frame: pd.DataFrame, *cols) -> bool:
    return not frame.empty and all(c in frame.columns for c in cols)


def _top(frame: pd.DataFrame, col: str, limit: int) -> list:
    """Most frequent distinct values, longest list first. [] if the column is absent."""
    if not _has(frame, col):
        return []
    counts = _norm(frame[col]).value_counts()
    return [v for v in counts.head(limit).index if v]


def _mode(frame: pd.DataFrame, col: str) -> str:
    """The most common spelling, not whichever row sorted first."""
    if not _has(frame, col):
        return ""
    values = _norm(frame[col])
    values = values[values != ""]
    if values.empty:
        return ""
    m = values.mode()
    return m.iat[0] if len(m) else ""


def _dedup_shipments(frame: pd.DataFrame) -> pd.DataFrame:
    """One order can be many rows. Collapse on the declaration where possible,
    so partner frequency measures relationships rather than paperwork."""
    for key in ("DECL_NO", "UUID"):
        if key in frame.columns and frame[key].notna().any():
            return frame.drop_duplicates(subset=[key])
    return frame


def _line(label: str, values, width: int = 12) -> str:
    if not values:
        return ""
    if isinstance(values, str):
        values = [values]
    return f"  {label:<{width}}: " + " | ".join(str(v) for v in values)


# ───────────────────────── extraction ─────────────────────────

def _products(frame: pd.DataFrame) -> list:
    """Distinct HS code + description pairs, most common first."""
    if not _has(frame, "HS_CODE"):
        return []

    hs = _norm(frame["HS_CODE"])
    if "PRODUCT_DESCRIPTION_NORM" in frame.columns:
        desc = _norm(frame["PRODUCT_DESCRIPTION_NORM"])
        pairs = pd.DataFrame({"hs": hs, "desc": desc})
        pairs = pairs[(pairs["hs"] != "")]
        grouped = pairs.groupby(["hs", "desc"]).size().sort_values(ascending=False)
        return [f"{h} {d}".strip() for (h, d) in grouped.head(MAX_PRODUCTS).index]

    return [h for h in hs.value_counts().head(MAX_PRODUCTS).index if h]


def _partners(frame: pd.DataFrame, company_col: str, country_col: str,
              city_col: str) -> list:
    """Counterparty with its country, and city when that is known."""
    if not _has(frame, company_col):
        return []

    work = pd.DataFrame({"co": _norm(frame[company_col])})
    work["country"] = _norm(frame[country_col]) if country_col in frame.columns else ""
    work["city"] = _norm(frame[city_col]) if city_col in frame.columns else ""
    work = work[work["co"] != ""]
    if work.empty:
        return []

    order = work["co"].value_counts().head(MAX_PARTNERS).index
    out = []
    for co in order:
        rows = work[work["co"] == co]
        country = rows["country"].mode()
        city = rows["city"].mode()
        where = ", ".join(x for x in (city.iat[0] if len(city) else "",
                                      country.iat[0] if len(country) else "") if x)
        out.append(f"{co} ({where})" if where else co)
    return out


def _by_country(frame: pd.DataFrame, country_col: str) -> list:
    """Which products each country takes — the line that shows an uneven mix."""
    if not _has(frame, country_col, "HS_CODE"):
        return []

    work = pd.DataFrame({"c": _norm(frame[country_col]),
                         "hs": _norm(frame["HS_CODE"])})
    work = work[(work["c"] != "") & (work["hs"] != "")]
    if work.empty:
        return []

    order = work["c"].value_counts().head(MAX_COUNTRY_ROWS).index
    out = []
    for country in order:
        codes = sorted(work.loc[work["c"] == country, "hs"].unique())[:MAX_PER_COUNTRY]
        out.append(f"{country} -> {', '.join(codes)}")
    return out


def _chapters(frame: pd.DataFrame) -> list:
    """First two HS digits. Chapter 61 out against 54 in is what says
    'fabric in, garment out' — the signal behind trade_role."""
    if not _has(frame, "HS_CODE"):
        return []
    return sorted({h[:2] for h in _norm(frame["HS_CODE"]) if len(h) >= 2})


def _side_facts(frame: pd.DataFrame, side: str) -> dict:
    """Everything for one side. side is 'EXPORT' or 'IMPORT'."""
    export = side == "EXPORT"
    other_co = "BUYER_COMPANY" if export else "SELLER_COMPANY"
    other_country = "BUYER_COUNTRY" if export else "SELLER_COUNTRY"
    other_city = "BUYER_CITY" if export else "SELLER_CITY"

    clean = _dedup_shipments(frame)

    return {
        "rows": len(frame),
        "products": _products(frame),
        "chapters": _chapters(frame),
        "partners": _partners(clean, other_co, other_country, other_city),
        "partner_count": _norm(clean[other_co]).nunique() if _has(clean, other_co) else 0,
        "countries": _top(frame, other_country, MAX_COUNTRIES),
        "by_country": _by_country(frame, other_country),
        "ports_origin": _top(frame, "PORT_OF_ORIGIN", MAX_PORTS),
        "ports_dest": _top(frame, "PORT_OF_DESTINATION", MAX_PORTS),
        "units": _top(frame, "UNIT", 6),
        "attrs": _top(frame, "PROD_ATTR", MAX_ATTRS),
    }


# ───────────────────────── entry point ─────────────────────────

def aggregate(df: pd.DataFrame, company_name: str) -> dict:
    """Facts block for the prompt, plus stats for logging.

    Returns {"text": str, "stats": dict, "exports": dict, "imports": dict}
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return {"text": "", "stats": {"rows": 0}, "exports": {}, "imports": {}}

    name = (company_name or "").strip().upper()

    sells = df[_norm(df["SELLER_COMPANY"]) == name] if "SELLER_COMPANY" in df.columns \
        else df.iloc[0:0]
    buys = df[_norm(df["BUYER_COMPANY"]) == name] if "BUYER_COMPANY" in df.columns \
        else df.iloc[0:0]

    exports = _side_facts(sells, "EXPORT")
    imports = _side_facts(buys, "IMPORT")

    # home location comes from the company's own side, never the counterparty
    own = sells if len(sells) >= len(buys) else buys
    prefix = "SELLER" if len(sells) >= len(buys) else "BUYER"
    home = {
        "city": _mode(own, f"{prefix}_CITY"),
        "state": _mode(own, f"{prefix}_STATE"),
        "country": _mode(own, f"{prefix}_COUNTRY"),
    }

    if len(sells) > len(buys) * 2:
        lean = "mostly exports"
    elif len(buys) > len(sells) * 2:
        lean = "mostly imports"
    elif len(sells) and len(buys):
        lean = "both sides"
    elif len(sells):
        lean = "exports only"
    elif len(buys):
        lean = "imports only"
    else:
        lean = "no matching rows"

    stats = {
        "rows_total": len(df),
        "rows_export": len(sells),
        "rows_import": len(buys),
        "lean": lean,
        "suppliers": imports["partner_count"],
        "customers": exports["partner_count"],
        "matched": len(sells) + len(buys),
    }

    text = _render(name, home, lean, exports, imports, stats)
    return {"text": text, "stats": stats, "exports": exports, "imports": imports}


def _render(name, home, lean, exports, imports, stats) -> str:
    out = [f"COMPANY: {name}"]

    where = ", ".join(x for x in (home["city"], home["state"], home["country"]) if x)
    out.append(f"HOME: {where}" if where else "HOME: (not in the data)")
    out.append(f"TRADE LEAN: {lean}")

    if stats["matched"] == 0:
        out.append("")
        out.append("No rows matched this company on either side.")
        return "\n".join(out)

    if exports["rows"]:
        out.append("")
        out.append("EXPORTS - the company is the seller")
        for label, key in (("products", "products"), ("hs chapters", "chapters"),
                           ("customers", "partners"), ("countries", "countries"),
                           ("by country", "by_country"),
                           ("ports out", "ports_origin"),
                           ("ports in", "ports_dest"),
                           ("units", "units"), ("attributes", "attrs")):
            line = _line(label, exports[key])
            if line:
                out.append(line)
        if exports["partner_count"] == 1:
            out.append("  note        : a single customer")

    if imports["rows"]:
        out.append("")
        out.append("IMPORTS - the company is the buyer")
        for label, key in (("products", "products"), ("hs chapters", "chapters"),
                           ("suppliers", "partners"), ("countries", "countries"),
                           ("by country", "by_country"),
                           ("ports out", "ports_origin"),
                           ("ports in", "ports_dest"),
                           ("units", "units"), ("attributes", "attrs")):
            line = _line(label, imports[key])
            if line:
                out.append(line)
        if imports["partner_count"] == 1:
            out.append("  note        : a single supplier")

    truncated = []
    if len(exports["products"]) == MAX_PRODUCTS:
        truncated.append("export products")
    if len(imports["products"]) == MAX_PRODUCTS:
        truncated.append("import products")
    if len(exports["partners"]) == MAX_PARTNERS:
        truncated.append("customers")
    if len(imports["partners"]) == MAX_PARTNERS:
        truncated.append("suppliers")
    if truncated:
        out.append("")
        out.append(f"(lists capped, more exist: {', '.join(truncated)})")

    return "\n".join(out)



if __name__ == "__main__":
    CSV_PATH = "C:\\Users\\TDB\\Desktop\\walmart_imports_exports.xlsx"                      # local path or an https:// url
    COMPANY_NAME = "WALMART INC"

    frame = pd.read_excel(CSV_PATH, dtype=str, keep_default_na=False)

    print(f"read {CSV_PATH}: {len(frame)} rows, {len(frame.columns)} columns\n")

    result = aggregate(frame, COMPANY_NAME)
    print("Debug line 304")
    print(f"unique_keys = {list(result.keys())}")
    print("=" * 66)
    print(result["text"] or "(nothing — no rows matched that company)")
    print("checking type....", type(result["text"]))
    print("=" * 66)
    
    print("\nstats:", result["stats"])