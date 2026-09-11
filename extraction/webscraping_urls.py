"""
Simple scraper with a fallback strategy:

  1. Try plain HTTP + BeautifulSoup  (fast, free)
  2. If the page looks JS-rendered   -> Firecrawl (headless browser, paid API)

Install:
    pip install requests beautifulsoup4 firecrawl-py
    export FIRECRAWL_API_KEY="fc-..."
"""
from dotenv import load_dotenv
load_dotenv() 
import os
import requests
from bs4 import BeautifulSoup
from firecrawl import Firecrawl  # v2 SDK

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MyScraper/1.0)"}
MIN_TEXT_LENGTH = 10000  # tune this per site


# ---------- 1. static HTML path ----------------------------------------------
def scrape_static(url: str) -> dict | None:
    """Return parsed data, or None if the page looks JS-rendered."""
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # drop noise before measuring
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(" ", strip=True)

    if not is_static_enough(soup, text):
        return None  # -> caller falls back to Firecrawl

    return {
        "source": "beautifulsoup",
        "url": url,
        "title": soup.title.string.strip() if soup.title else None,
        "headings": [h.get_text(strip=True) for h in soup.select("h1, h2")],
        "links": [a["href"] for a in soup.select("a[href]")][:20],
        "text": text,
    }


def is_static_enough(soup, text: str) -> bool:
    """Heuristics: did the server actually send us content?"""
    if len(text) < MIN_TEXT_LENGTH:
        return False

    # classic SPA shell: <div id="root"></div> / <div id="__next"></div>
    for spa_id in ("root", "app", "__next", "__nuxt"):
        node = soup.find(id=spa_id)
        if node and len(node.get_text(strip=True)) < 100:
            return False

    return True


# ---------- 2. JS-heavy path --------------------------------------------------
def scrape_with_firecrawl(url: str) -> dict:
    

    app = Firecrawl(api_key=os.getenv("FIRECRAWL_API_KEY"))
    api_key = os.getenv("FIRECRAWL_API_KEY")
    #print(repr(api_key), len(api_key))
    doc = app.scrape(url, formats=["markdown"], only_main_content=True)

    return {
        "source": "firecrawl",
        "url": url,
        "title": getattr(doc.metadata, "title", None),
        "text": doc.markdown,
    }




# ---------- router ------------------------------------------------------------
def scrape(url: str) -> dict:
    try:
        result = scrape_static(url)
        if result:
            return result
        print(f"[info] {url} looks JS-rendered, switching to Firecrawl")
    except requests.RequestException as e:
        print(f"[warn] direct fetch failed ({e}), switching to Firecrawl")

    return scrape_with_firecrawl(url)


if __name__ == "__main__":
    for target in [
        "https://books.toscrape.com/", # static -> BeautifulSoup
        "https://www.booking.com/",  # JS-rendered -> Firecrawl
    ]:
         
        data = scrape(target)
        print("***************")
        print(data)
        print("****************")
        print(f"\n=== {data['url']} via {data['source']} ===")
        print(data["title"])
        print(data["text"][0:300], "...")