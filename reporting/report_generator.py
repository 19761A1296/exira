"""
Report handling. run_pipeline stays exactly as it is.

Every log_report() call in run_pipeline is immediately followed by a return,
so log_report IS the final capture point. It now stamps the report, prints it,
and hands the full dict to report_percent.
"""

from datetime import datetime, timezone


_FIRED = "_handed_off"

def init_report():
    """Create the initial run report dict."""
    return {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "attempt": 0,
        "company_input": None,
        "company_found_in_db": None,
        "company_name": None,
        "url": None,
        "scraped_info": None,
        "country": None,
        "product": None,
        "source": None,   # "db_pipeline" | "scraped_info" | "search_selection" | "partial_query" | None
        "tag": None,      # which exit path the run left from
    }


def update_report(report, **fields):
    """Update report dict with any given fields. Returns the same dict."""
    report.update(fields)
    return report


def log_report(report, tag=""):
    """
    Final checkpoint. Called once per run, from whichever exit path fires.

    Prints the report, then hands it to report_percent. Guarded so a stray
    second call can't fire the handoff twice.
    """
    print(f"REPORT {tag}:", report)

    if report.get(_FIRED):
        return None

    update_report(
        report,
        tag=tag,
        finished_at=datetime.now(timezone.utc).isoformat(),
        **{_FIRED: True},
    )

    return final_percent(_public(report))


def _public(report):
    """Copy without internal bookkeeping keys, so report_percent sees a clean dict."""
    return {k: v for k, v in report.items() if not k.startswith("_")}




def final_percent(report):
    print("Final Report:", report)
    pass

if __name__ == "__main__":
    pass