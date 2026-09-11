
from reporting.report_generator import init_report,update_report,log_report
from database.snowflake_db import companies_list,build_query,run_query
from extraction.extracting_urls import get_company_information_links
from summarizer.user_summary import build_user_summary

MAX_ATTEMPTS= 3
COMPANY_FIELDS = ("company_name", "company_info")

def resolve_in_db(company_name):
   
    if not company_name:
        return None

    company_list = companies_list(company_name)      # your existing DB lookup
    
    if not company_list:
        print("No company found in DB for:", company_name)
        return None

    for i, company in enumerate(company_list, start=1):
        print(f"{i}. {company}")

    choice = input("Enter no to select company: ").strip()
    if not choice.isdigit():
        print("Invalid input.")
        return None

    idx = int(choice) - 1                 # display is 1-based, list is 0-based
    if not 0 <= idx < len(company_list):
        print("No such option.")
        return None

    return company_list[idx]

def _ask(prompt):
    return input(prompt).strip()
def ask_for_urls(reason):
    print(reason)
    return _ask("Enter company related urls / links: ")


## only info scraping -- no need to get company name from text, just scrape the info from urls and return it.
def scrape_company_info(urls):
    """Scrape + human approval loop. Returns the accepted info, or None."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"[{attempt}/{MAX_ATTEMPTS}] scraping:", urls)
 
        response = get_company_information_links(urls) or {}
        info = response.get("company_info", "")
        
        print("Scraped info:", info)
 
        accept = input("Agree for response? (y/n/e to exit): ").strip().lower()
        if accept in ("y", "yes"):
            return response.get("company_info", "")
        if accept in ("e", "exit", "q", "quit"):
            print("Exiting the scrape loop.")
            break
 
        new_urls = _ask("Enter additional links/info (Enter to retry same): ")
        if new_urls:
            urls = new_urls
 
    print(f"Not accepted after {MAX_ATTEMPTS} attempts.")
    return None

## extarct company name and info from urls, if company name is found in db, return the info from db, else return the scraped info.
def review_company_data(urls):
    """Scrape once per attempt; approve each field separately.
    Returns {"company_name": str|None, "company_info": str|None}."""
    approved = {f: None for f in COMPANY_FIELDS}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        pending = [f for f, v in approved.items() if v is None]
        if not pending:
            break

        print(f"[{attempt}/{MAX_ATTEMPTS}] scraping: {urls}")
        response = get_company_information_links(urls) or {}

        aborted = False
        for field in pending:
            value = response.get(field) or ""
            if not value:
                print(f"{field}: <nothing scraped>")
                continue

            print(f"\n{field}:\n{value}")
            ans = _ask(f"Accept {field}? (y/n/e to exit): ").lower()
            if ans in ("e", "exit", "q", "quit"):
                aborted = True
                break
            if ans in ("y", "yes"):
                approved[field] = value

        if aborted or all(v is not None for v in approved.values()):
            break

        if attempt == MAX_ATTEMPTS:
            print("Not accepted after max attempts:",
                  [f for f, v in approved.items() if v is None])
            break

        new_urls = _ask("Enter additional links/info (Enter to retry same): ")
        if new_urls:
            urls = new_urls

    return approved


def run_db_pipeline(company_name="", scraped_info=""):

    data = []
    if company_name:
        print(f"Company name: {company_name}")
        queries = build_query(company_name)
        print("Queries:", queries)
    
        df = run_query(queries)
        df.to_string(index=False)
        print("DB Results:\n", df)
        data.append(df)
    if scraped_info:
        print("Scraped info:\n", scraped_info)
        data.append(scraped_info)
    print("Data to summarize:", data)
    final_summary = build_user_summary(company_name, data)    
    print("User_product_info \n", final_summary)
    return final_summary



### text and urls functions


# ─────────────────── steps ───────────────────

def resolve_from_text(text, report):
    """Extract a company name from free text and look it up in the DB."""
    if not text:
        return None

    update_report(report, company_input=text)

    print("Line 145 debugging....")
    company_name = resolve_in_db(text)
    print("Line 147 debugging....")
    update_report(report,
                  company_name=company_name,
                  company_found_in_db=bool(company_name))
    return company_name


def get_urls(urls):
    """Use the URLs given, otherwise ask once."""
    if urls:
        return urls
    return ask_for_urls("Please provide some links (optional, press Enter to skip).")


def handle_known_company(company_name, urls, report):
    """Company already resolved — URLs are optional extra context."""
    update_report(report, company_found_in_db=True)
    print(f"\n✅ S1: Company found in DB: {company_name}")

    urls = get_urls(urls)
    scraped_info = scrape_company_info(urls) if urls else ""

    update_report(report, url=urls, scraped_info=scraped_info, source="db_pipeline")
    log_report(report, tag="S1")
    return run_db_pipeline(company_name, scraped_info or "")


def handle_urls(urls, report):
    """No company yet — pull one out of the URLs and try the DB again.

    Returns (result, handled).
    """
    urls = get_urls(urls)
    if not urls:
        return None, False

    update_report(report, url=urls)
    print("\n⚠️ S2: Company not in DB. Get from URLs...")

    result = review_company_data(urls)
    scraped_info = result.get("company_info") or ""

    if scraped_info:
        update_report(report, scraped_info=scraped_info)

    raw_name = result.get("company_name")
    company_from_url = resolve_in_db(raw_name) if raw_name else None

    if company_from_url:
        update_report(report,
                      company_found_in_db=True,
                      company_name=company_from_url,
                      source="db_pipeline")
        print(f"✅ Found in DB via URL: {company_from_url}")
        log_report(report, tag="S2-db")
        return run_db_pipeline(company_from_url, scraped_info or ""), True

    if scraped_info:
        update_report(report, source="scraped_info")
        print("📊 Company not in DB. Returning scraped info.")
        log_report(report, tag="S2-scraped")
        return run_db_pipeline(raw_name, scraped_info or ""), True

    update_report(report, source=None)
    log_report(report, tag="S2-empty")
    return "", True


# ─────────────────── one attempt ───────────────────

def run_once(text, urls, report):
    """One pass. Returns (result, handled)."""
    print("Line 218 debugging....")
    company_name = resolve_from_text(text, report)
    print("Line 220 debugging....")

    if company_name:
        return handle_known_company(company_name, urls, report), True

    return handle_urls(urls, report)


# ─────────────────── driver ───────────────────

def run_pipeline(text="", urls="", max_attempts=3):
    """text and urls are both optional."""
    report = init_report()
    attempt = 0

    while attempt < max_attempts:
        attempt += 1
        update_report(report, attempt=attempt)
        print(f"\n{'='*50}")
        print(f"Pipeline Attempt {attempt}/{max_attempts}")
        print(f"{'='*50}")

        result, handled = run_once(text, urls, report)
        if handled:
            return result

        print(f"❌ Attempt {attempt} failed to find results.")

        if attempt >= max_attempts:
            print(f"❌ Max attempts ({max_attempts}) reached.")
            break

        more_info = input("\n📝 Provide additional info to try again? (y/n): ").strip().lower()
        if more_info not in ("y", "yes"):
            print("❌ User chose to stop.")
            break

        new_text = input("Enter additional information: ").strip()
        if new_text:
            text = f"{text} {new_text}".strip()
        urls = ""   # let the next attempt re-ask

    update_report(report, source=None)
    log_report(report, tag="exhausted")
    return ""







if __name__ == "__main__":
    result = run_pipeline(text="", urls="https://kis.ai/")
    print("\nFinal Result:\n", result)