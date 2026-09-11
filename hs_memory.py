# hs_memory.py
import json
import re

HS_PATTERN = re.compile(r"(?i)\bHS[\s:\-]*(\d{2,10})\b")


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def collect_hs_from_memory(memory: dict, domain_map: dict) -> list[dict]:
    """Scan every memory field for HS codes. Returns [{code, domain, source}]."""
    found: dict[str, dict] = {}

    for source in ("user_product_info", "old_session_summary", "persona"):
        for match in HS_PATTERN.finditer(_as_text(memory.get(source))):
            code = match.group(1)
            if code not in found:
                found[code] = {
                    "code": code,
                    "domain": domain_map.get(code[:2].zfill(2), "Unknown / Other"),
                    "source": source,
                }

    for q in memory.get("current_session_queries", []):
        for match in HS_PATTERN.finditer(_as_text(q)):
            code = match.group(1)
            if code not in found:
                found[code] = {
                    "code": code,
                    "domain": domain_map.get(code[:2].zfill(2), "Unknown / Other"),
                    "source": "current_session",
                }

    return sorted(found.values(), key=lambda x: x["code"])