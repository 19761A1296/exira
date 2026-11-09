# exira_core/onboarding.py
"""
Onboarding wizard (add a company) as plain actions.

Same flow as the Streamlit wizard:
    input -> [narrow] -> pick_text -> [ask_urls] -> scrape_known -> profile
    input -> [ask_urls] -> review_urls -> [narrow] -> pick_url -> profile

Each button in the old UI is one function here. The current step is
S.onb_step; when a step needs the links read, the scrape runs at the end of
the action that led there (auto_scrape), so the next view already has it.
A failed attempt sets S.onb_failure; the user then calls retry() or give_up().
"""

import pandas as pd

from database import info_database as db
from database.snowflake_db import companies_list, build_query, run_query
from extraction.extracting_urls import get_company_information_links
from reporting.report_generator import init_report, update_report, log_report
from summarizer.user_summary import build_user_summary
from summarizer.user_summary_aggregate import aggregate

from exira_core.state import S, UserError, notify, warn, error, progress

MAX_ATTEMPTS = 3
NARROW_THRESHOLD = 30

STEPS = ("input", "narrow", "pick_text", "ask_urls", "scrape_known", "review_urls", "pick_url")


# ───────────────────────── helpers (from the old UI) ─────────────────────────

def onb_reset_for_retry():
    S.onb_matches = []
    S.onb_company = ""
    S.onb_scraped_info = ""
    S.onb_scraped_name = ""
    S.onb_pending = {}
    S.onb_scrape_attempt = 0
    S.onb_country = ""
    S.onb_hs2 = ""
    S.onb_failure = None


def onb_lookup(name: str, country: str = "", hs2: str = "") -> list:
    if not name:
        return []
    progress("Looking up the company…")
    try:
        return companies_list(name, limit=500, country=country or None, hs2=hs2 or None) or []
    except TypeError:
        if country or hs2:
            warn("companies_list does not support country/HS filters yet.")
        try:
            return companies_list(name) or []
        except Exception as exc:
            error(f"Company lookup failed: {exc}")
            return []
    except Exception as exc:
        error(f"Company lookup failed: {exc}")
        return []


def onb_scrape(urls: str) -> dict:
    try:
        return get_company_information_links(urls) or {}
    except Exception as exc:
        error(f"Scrape failed: {exc}")
        return {}


def onb_build_profile(company_name: str, scraped_info: str):
    progress("Building the company profile…")
    data = []
    if company_name:
        try:
            df = run_query(build_query(company_name))
            if isinstance(df, pd.DataFrame) and not df.empty:
                agg = aggregate(df, company_name)
                facts = agg.get("text", "") if agg else ""
                if facts:
                    data.append(facts)
                    notify("info", f"Pulled {len(df)} shipment rows for {company_name}.")
                else:
                    notify("info", f"No rows matched {company_name} on either side.")
            else:
                notify("info", f"No shipment rows found for {company_name}.")
        except Exception as exc:
            warn(f"Snowflake query failed: {exc}")

    if scraped_info:
        data.append(scraped_info)

    update_report(S.report, source=("db_pipeline" if company_name else "scraped_info"))

    summary = ""
    try:
        summary = build_user_summary(company_name, data)
    except Exception as exc:
        error(f"Profile build failed: {exc}")

    log_report(S.report, tag="ui-onboarding")

    # the profile belongs to a company now, not to the user
    name = (company_name or S.onb_text or "Company").strip()
    cid = db.create_company(S.userid, company_name=name,
                            company_profile=summary or "", onboarding_report=S.report or {})
    if cid is None:
        error("That company could not be added — you may already have 5, or a company with that name.")
        return

    S.company_id = cid
    S.company_name = name
    S.user_product_info = summary or ""
    S.old_session = ""
    S.persona = None
    # a brand new company has no sessions, so pick or create one next
    S.stage = "session_picker"


def onb_fail_or_retry(reason: str):
    """Old UI showed retry buttons here; now it is a state the UI shows."""
    warn(reason)
    exhausted = S.onb_attempt >= MAX_ATTEMPTS
    if exhausted:
        error(f"Max attempts ({MAX_ATTEMPTS}) reached.")
        update_report(S.report, source=None)
        log_report(S.report, tag="exhausted")
    S.onb_failure = {"reason": reason, "exhausted": exhausted}


def enter_pick(matches: list, search_name: str, next_step: str):
    S.onb_matches = matches
    S.onb_search_name = search_name
    S.onb_next_after_pick = next_step
    S.onb_step = "narrow" if len(matches) > NARROW_THRESHOLD else next_step


def auto_scrape():
    """Read the links when the current step needs them and they aren't read yet."""
    if S.stage == "onboarding" and S.onb_step in ("scrape_known", "review_urls") and not S.onb_pending:
        S.onb_scrape_attempt += 1
        progress(f"Reading {S.onb_urls} …")
        S.onb_pending = onb_scrape(S.onb_urls)


def _need_step(*steps):
    if S.stage != "onboarding":
        raise UserError("Onboarding is not in progress.")
    if steps and S.onb_step not in steps:
        raise UserError(f"This action is not available at step '{S.onb_step}'.")


# ───────────────────────── actions ─────────────────────────

def start():
    """'+ Add a company'."""
    if db.company_slots_free(S.userid) <= 0:
        raise UserError("All 5 company slots are used. Delete one first.")
    S.onb_step = "input"
    S.onb_text = ""
    S.onb_urls = ""
    S.onb_attempt = 0
    S.report = None
    onb_reset_for_retry()
    S.stage = "onboarding"


def submit_input(text: str = "", urls: str = ""):
    """Step 'input': company name/description and/or URLs."""
    _need_step("input")
    text, urls = (text or "").strip(), (urls or "").strip()
    if not text and not urls:
        raise UserError("Enter a company name or at least one URL.")

    S.onb_failure = None
    S.onb_text = text
    S.onb_urls = urls
    S.onb_attempt += 1
    if S.report is None:
        S.report = init_report()
    update_report(S.report, attempt=S.onb_attempt, company_input=text, url=urls)

    if text:
        matches = onb_lookup(text)
        if matches:
            enter_pick(matches, text, "pick_text")
            return

    if urls:
        S.onb_step = "review_urls"
    else:
        S.onb_after_urls = "review_urls"
        S.onb_step = "ask_urls"


def narrow(country: str = "", hs2: str = ""):
    """Step 'narrow': filter a long match list by country and/or HS2."""
    _need_step("narrow")
    country, hs2 = (country or "").strip(), (hs2 or "").strip()
    if hs2 and not (S.onb_country or country):
        raise UserError("HS filter needs a country as well.")
    filtered = onb_lookup(S.onb_search_name, country=country, hs2=hs2)
    if not filtered:
        warn("Nothing matched those filters — the previous list is kept.")
        return
    S.onb_matches = filtered
    S.onb_country = country
    S.onb_hs2 = hs2
    if len(filtered) <= NARROW_THRESHOLD:
        S.onb_step = S.onb_next_after_pick


def show_all():
    """Step 'narrow': 'Show all N anyway'."""
    _need_step("narrow")
    S.onb_step = S.onb_next_after_pick


def open_narrow():
    """Steps 'pick_text' / 'pick_url': 'Narrow this list'."""
    _need_step("pick_text", "pick_url")
    if len(S.onb_matches) <= NARROW_THRESHOLD:
        raise UserError("The list is already short enough.")
    S.onb_step = "narrow"


def pick(index: int):
    """Steps 'pick_text' / 'pick_url': choose match number `index` (1-based)."""
    _need_step("pick_text", "pick_url")
    if not 1 <= int(index) <= len(S.onb_matches):
        raise UserError(f"Pick a number between 1 and {len(S.onb_matches)}.")
    name = S.onb_matches[int(index) - 1]

    if S.onb_step == "pick_text":
        S.onb_company = name
        update_report(S.report, company_name=name, company_found_in_db=True)
        if S.onb_urls:
            S.onb_step = "scrape_known"
        else:
            S.onb_after_urls = "scrape_known"
            S.onb_step = "ask_urls"
        return

    # pick_url
    update_report(S.report, company_name=name, company_found_in_db=True, source="db_pipeline")
    onb_build_profile(name, S.onb_scraped_info)


def none_of_these():
    """Steps 'pick_text' / 'pick_url': 'None of these'."""
    _need_step("pick_text", "pick_url")
    if S.onb_step == "pick_text":
        if S.onb_urls:
            S.onb_step = "review_urls"
        else:
            S.onb_after_urls = "review_urls"
            S.onb_step = "ask_urls"
        return

    # pick_url
    update_report(S.report, company_name=S.onb_scraped_name, company_found_in_db=False, source="scraped_info")
    if S.onb_scraped_info:
        onb_build_profile(S.onb_scraped_name, S.onb_scraped_info)
    else:
        onb_fail_or_retry("No database match and no scraped information.")


def give_urls(urls: str):
    """Step 'ask_urls': 'Use these links'."""
    _need_step("ask_urls")
    urls = (urls or "").strip()
    if not urls:
        raise UserError("Enter at least one URL, or skip.")
    S.onb_urls = urls
    update_report(S.report, url=urls)
    S.onb_step = S.onb_after_urls


def skip_urls():
    """Step 'ask_urls': 'Skip'."""
    _need_step("ask_urls")
    if S.onb_company:
        onb_build_profile(S.onb_company, "")
    else:
        onb_fail_or_retry("No company and no links, so nothing to work with.")


def accept_scrape():
    """Step 'scrape_known': '✅ Accept' the scraped information."""
    _need_step("scrape_known")
    info = (S.onb_pending or {}).get("company_info") or ""
    update_report(S.report, scraped_info=info, source="db_pipeline")
    onb_build_profile(S.onb_company, info)


def skip_links():
    """Step 'scrape_known': '🚫 Skip links'."""
    _need_step("scrape_known")
    onb_build_profile(S.onb_company, "")


def retry_scrape(urls: str = ""):
    """Steps 'scrape_known' / 'review_urls': '🔁 Retry' with optional new links."""
    _need_step("scrape_known", "review_urls")
    if S.onb_scrape_attempt >= MAX_ATTEMPTS:
        raise UserError(f"Scrape retries used up ({MAX_ATTEMPTS}).")
    urls = (urls or "").strip()
    if urls:
        S.onb_urls = urls
        update_report(S.report, url=urls)
    S.onb_pending = {}


def review_continue(take_name: bool = True, take_info: bool = True):
    """Step 'review_urls': 'Continue' with the accepted name / information."""
    _need_step("review_urls")
    pending = S.onb_pending or {}
    name = pending.get("company_name") or ""
    info = pending.get("company_info") or ""

    S.onb_scraped_name = name if (take_name and name) else ""
    S.onb_scraped_info = info if (take_info and info) else ""

    if S.onb_scraped_info:
        update_report(S.report, scraped_info=S.onb_scraped_info)

    if S.onb_scraped_name:
        matches = onb_lookup(S.onb_scraped_name)
        if matches:
            enter_pick(matches, S.onb_scraped_name, "pick_url")
            return

    if S.onb_scraped_info:
        update_report(S.report, company_name=S.onb_scraped_name, company_found_in_db=False,
                      source="scraped_info")
        onb_build_profile(S.onb_scraped_name, S.onb_scraped_info)
        return

    onb_fail_or_retry("Nothing was accepted from those links.")


def retry(extra: str = ""):
    """After a failure: 'Try again', optionally adding more information."""
    _need_step()
    if S.onb_attempt >= MAX_ATTEMPTS:
        raise UserError(f"Max attempts ({MAX_ATTEMPTS}) reached. Continue without a profile.")
    extra = (extra or "").strip()
    if extra:
        S.onb_text = f"{S.onb_text} {extra}".strip()
    S.onb_urls = ""
    onb_reset_for_retry()
    S.onb_step = "input"


def give_up():
    """After attempts run out: 'Continue without a profile' (or cancel onboarding)."""
    _need_step()
    S.user_product_info = ""
    S.old_session = ""
    S.persona = None
    S.candidates = []
    S.onb_failure = None
    S.companies = db.list_companies(S.userid)
    S.stage = "company_picker" if S.companies else "login"


ACTIONS = {
    "start": start,
    "input": submit_input,
    "narrow": narrow,
    "show_all": show_all,
    "open_narrow": open_narrow,
    "pick": pick,
    "none_of_these": none_of_these,
    "give_urls": give_urls,
    "skip_urls": skip_urls,
    "accept_scrape": accept_scrape,
    "skip_links": skip_links,
    "retry_scrape": retry_scrape,
    "review_continue": review_continue,
    "retry": retry,
    "give_up": give_up,
}


def run(action: str, **args):
    fn = ACTIONS.get(action)
    if fn is None:
        raise UserError(f"Unknown onboarding action '{action}'.")
    fn(**args)
    auto_scrape()
