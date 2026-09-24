# summarizer/web_search.py
"""Web research for suggestive / advisory questions, via Perplexity sonar-pro.

Called when the classifier returns WEB. Sonar does its own searching, so there
is no separate search step here: one request in, a grounded answer and a list of
sources out.

    result = web_search(query, memory)
    result["answer"]     -> str
    result["citations"]  -> [str]
    result["error"]      -> str, empty when it worked

    as_context(result)   -> a text block to append under a trade answer

The user's stored profile is passed in as background so suggestions fit their
actual business, with an explicit instruction never to treat it as fact about
the wider market.

.env
----
    OPENROUTER_API_KEY=sk-or-...
    OPENROUTER_MODEL_WEB=perplexity/sonar-pro     # optional
"""

import json
import os

import requests
from dotenv import load_dotenv

load_dotenv()

URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.getenv("OPENROUTER_MODEL_WEB", "perplexity/sonar-pro")
MAX_TOKENS = int(os.getenv("WEB_MAX_TOKENS", "2000"))
TIMEOUT = int(os.getenv("WEB_TIMEOUT", "120"))
MAX_CITATIONS = int(os.getenv("WEB_MAX_CITATIONS", "6"))
PRINT_WEB = (os.getenv("PRINT_WEB", "1") or "").strip() not in {"0", "false", "False", ""}


SYSTEM_PROMPT = """You are a trade research assistant for an import-export business.
You answer suggestive and advisory questions using current information from the web.

WHAT YOU ARE FOR
- expansion options: which markets, which adjacent or related product categories
- transhipment routes, corridors, ports and logistics options
- tariffs, duties, sanctions, regulations, certifications and compliance
- market conditions, demand outlook, risks, disruptions, freight and prices
- background on companies, buyers and suppliers

HOW TO ANSWER
- Be concrete. Name countries, product categories, HS chapters, ports and
  corridors. A list of three to six specific options beats a general essay.
- Give a short reason for each option: why that market, why that category, why
  that route.
- Say plainly when something is uncertain, regional, or changing.
- Use current information and prefer recent sources.
- Keep it tight: a short paragraph of framing, then the options. No headings,
  no markdown tables. Under 300 words unless the question needs more.

ABOUT THE USER PROFILE
You are given a profile of the user's own company as background, so your
suggestions fit what they actually trade. Treat it as context only:
- Never present anything from the profile as a fact about the wider market.
- Never quote figures from it as if you verified them.
- If the profile is empty, answer generally for the trade in question.

WHAT NOT TO DO
- Do not invent the user's shipment volumes, buyers, suppliers or prices. Those
  come from their database, not from you.
- Do not tell the user to consult their own data; that part is handled elsewhere.
- Do not repeat the question back before answering."""


def _payload(query: str, memory: dict) -> dict:
    profile = ""
    if memory:
        profile = json.dumps(memory, ensure_ascii=False, default=str)[:3000]

    user_content = (
        f"<user_profile>\n{profile or '(none)'}\n</user_profile>\n\n"
        f"<question>\n{query.strip()}\n</question>"
    )

    return {
        "model": MODEL,
        "temperature": 0.2,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }


def _extract_citations(payload: dict, choice: dict) -> list:
    """Sonar returns sources in a few shapes depending on the route."""
    seen, out = set(), []

    def add(url):
        url = str(url or "").strip()
        if url and url not in seen:
            seen.add(url)
            out.append(url)

    for block in (payload.get("citations"), choice.get("citations")):
        if isinstance(block, list):
            for item in block:
                add(item if isinstance(item, str) else (item or {}).get("url"))

    for block in (payload.get("search_results"), choice.get("search_results")):
        if isinstance(block, list):
            for item in block:
                if isinstance(item, dict):
                    add(item.get("url"))

    annotations = (choice.get("message") or {}).get("annotations")
    if isinstance(annotations, list):
        for item in annotations:
            cite = (item or {}).get("url_citation") or {}
            add(cite.get("url"))

    return out[:MAX_CITATIONS]


def web_search(query: str, memory: dict = None) -> dict:
    """Ask sonar-pro. Never raises; check result['error']."""
    query = (query or "").strip()
    if not query:
        return {"answer": "", "citations": [], "error": "empty query"}

    try:
        r = requests.post(
            URL,
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
            json=_payload(query, memory or {}),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        payload = r.json()

        if payload.get("error"):
            msg = payload["error"].get("message", "OpenRouter error")
            print("web_search: API error:", msg)
            return {"answer": "", "citations": [], "error": msg}

        choice = payload["choices"][0]
        answer = (choice["message"].get("content") or "").strip()
        citations = _extract_citations(payload, choice)

        if not answer:
            finish = choice.get("finish_reason")
            print(f"web_search: empty content, finish_reason = {finish}")
            return {"answer": "", "citations": citations,
                    "error": f"empty reply ({finish})"}

        if PRINT_WEB:
            print(f"\n--- WEB ({MODEL}) | {query[:70]}")
            print(f"    {answer[:400]}")
            if citations:
                print(f"    sources: {len(citations)}")

        return {"answer": answer, "citations": citations, "error": ""}

    except Exception as exc:
        print("web_search failed:", type(exc).__name__, exc)
        return {"answer": "", "citations": [], "error": f"{type(exc).__name__}: {exc}"}


def as_context(result: dict, heading: str = "Additional context from the web") -> str:
    """Format a result for appending under a trade answer. '' when empty."""
    if not result or not result.get("answer"):
        return ""

    block = f"{heading}:\n{result['answer']}"
    if result.get("citations"):
        block += "\n\nSources:\n" + "\n".join(f"- {u}" for u in result["citations"])
    return block


# ───────────────────────── run directly ─────────────────────────
#
#   python -m summarizer.web_search

if __name__ == "__main__":
    MEMORY = {
        "user_product_info": "",     # paste a company card to see it personalise
        "old_session_summary": "",
        "current_session_queries": [],
        "persona": "",
    }

    QUERIES = [
        # fill for a batch run; leave empty for the prompt loop
        # "Suggest adjacent product categories and transhipment routes for a "
        # "Tirupur cotton knitwear exporter selling to the USA and Spain",
    ]

    def show(q):
        result = web_search(q, MEMORY)
        print(f"\n=== {q}")
        if result["error"]:
            print(f"  error: {result['error']}")
            return
        print(f"\n{result['answer']}\n")
        for url in result["citations"]:
            print(f"  - {url}")

    print(f"web search — model {MODEL}\n")

    if QUERIES:
        for q in QUERIES:
            show(q)
    else:
        print("blank line or 'exit' to quit\n")
        while True:
            try:
                q = input("Search> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not q or q.lower() in {"exit", "quit"}:
                break
            show(q)