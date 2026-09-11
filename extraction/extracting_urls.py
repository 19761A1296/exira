import json
import os

import requests
from dotenv import load_dotenv


load_dotenv()


from extraction.webscraping_urls import scrape





MAX_INFO_CHARS = 10000
MAX_TEXT_CHARS = 12000  # guard against huge scraped pages

PROMPT1 = f"""You are given the scraped content of a web page (url, title, text).

Identify the company that OWNS the page - not companies merely mentioned,
advertised, partnered with, or linked on it - and describe what it does.

Reply with ONLY JSON: {{"company_name": ..., "company_info": ...}}
- "company_name": the official company/brand name.
- "company_info": at most {MAX_INFO_CHARS} characters covering what the company
  does - its business, main products/services and who it serves. Plain factual
  prose, single line, no marketing language.
Use null for anything the page does not support. Do not guess."""


COMPANY_FIELDS = ("company_name", "company_info")

# things models emit when they mean "nothing"
_EMPTY = {"", "null", "none", "n/a", "na", "unknown", "not mentioned", "not specified"}







## _clean, _truncate, _call_llm, _build_input are the helpers that prepare the scraped page and call the LLM to extract company info.

def _clean(value):
    """Normalize a single field to a string ('' when absent)."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):          # model sometimes returns a list
        value = ", ".join(str(v) for v in value if v)
    value = str(value).strip()
    return "" if value.lower() in _EMPTY else value


def _truncate(text, limit=MAX_INFO_CHARS):
    """Trim to `limit` chars on a word boundary (models overshoot)."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return cut.rstrip(" ,;:-")


def _call_llm(prompt, text):
    """Single OpenRouter call, returns the parsed dict ({} on bad output)."""
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}"},
        json={
            "model": os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": text},
            ],
        },
        timeout=60,
    )
    response.raise_for_status()
    raw = response.json()["choices"][0]["message"]["content"]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def _build_input(page):
    """Flatten the scraped page dict into the text sent to the model."""
    return "\n".join([
        f"URL: {_clean(page.get('url'))}",
        f"TITLE: {_clean(page.get('title'))}",
        f"SOURCE: {_clean(page.get('source'))}",
        "",
        "PAGE CONTENT:",
        " ".join(_clean(page.get("text")).split())[:MAX_TEXT_CHARS],
    ])




def extract_company(page: dict) -> dict:
    """
    page -> {"source": ..., "url": ..., "title": ..., "text": ...}
    returns -> {"company_name": str, "company_info": str}  (always this shape)
    """
    if not isinstance(page, dict):
        return {field: "" for field in COMPANY_FIELDS}

    data = _call_llm(PROMPT1, _build_input(page))

    return {
        "company_name": _clean(data.get("company_name")),
        "company_info": _truncate(_clean(data.get("company_info"))),
    }



def get_company_information_links(urls):
    data = scrape(urls)
    response = extract_company(data)
    return response


if __name__ == "__main__":
    pass

    # response = get_company_name_from_url("http://jobs.lever.co/stripe")
    # print(response)
    #print(extract("PETER ENGLAND"))
    #print(get_company_information_links("https://www.tcs.com/"))
    # {'company': 'Acme', 'product': 'WidgetPro', 'country': ''}
    # result = extract("ISSGF INDIA PVT LTD began selling Fashion in INDIA")
    # print(result)
 