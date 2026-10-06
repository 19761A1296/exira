import os
import json
import requests
from dotenv import load_dotenv

from summarizer.summarizer_prompts.summarizer_prompts import PROMPT_USER_SUMMARY

load_dotenv()

OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_MODEL   = os.getenv("OPENROUTER_MODEL_USER_SUMMARY")
OPENROUTER_URL     = "https://openrouter.ai/api/v1/chat/completions"

EMPTY_CARD = {
    "memory_version": "v1",
    "company_name": "",
    "home_city": "",
    "home_state": "",
    "home_country": "",
    "profile": {"summary": ""},
    "topics": {
        k: {"summary": "", "details": []}
        for k in ("trade_role", "exports", "imports", "markets",
                  "logistics", "capabilities", "future_scope", "watch_points")
    },
}




def _as_text(data):
    """data: list holding the facts block and/or scraped text. Both optional.
    Order inside the list does not matter: the facts block is recognised by its
    COMPANY: header."""
    if data is None:
        return ""
    if not isinstance(data, (list, tuple)):
        data = [data]

    facts, texts = [], []
    for item in data:
        if not isinstance(item, str) or not item.strip():
            continue
        block = item.strip()
        if block.startswith("COMPANY:") or "\nEXPORTS - " in block or "\nIMPORTS - " in block:
            facts.append(block)
        else:
            texts.append(block)

    parts = []
    if facts:
        parts.append("SHIPMENT FACTS:\n" + "\n\n".join(facts))
    if texts:
        parts.append("COMPANY INFORMATION (from web):\n" + "\n\n".join(texts))

    return "\n\n".join(parts)


def _strip_fences(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _call_llm(prompt, user_content, temperature=0.2, timeout=120):
    resp = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENROUTER_MODEL,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "max_tokens": 8000,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_content},
            ],
        },
        timeout=timeout,
    )
    #print("STATUS:", resp.status_code)
    if not resp.ok:
        #print("OPENROUTER ERROR:")
        #print(resp.text)
        pass

    resp.raise_for_status()
    payload = resp.json()
    choice = payload["choices"][0]
    #print("finish_reason:", choice.get("finish_reason"), "| usage:", payload.get("usage"))
    return choice["message"].get("content")


def build_user_summary(company_name, data=None):
    """company_name: str.
    data: list holding the facts block and/or scraped text. Both optional.
    Returns a dict in card format."""
    company_name = (company_name or "").strip()
 

    body = _as_text(data)
    user_content = f"company_name: {company_name}\n\n{body if body else 'data: (none)'}"
    #print(f"{company_name} | body {len(body)} chars")
    #print()
    try:
        raw = _call_llm(PROMPT_USER_SUMMARY, user_content)
        if not raw:
            raise ValueError("model returned no content")
        card = json.loads(_strip_fences(raw))
    except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError) as e:
        #print(f"card build failed for {company_name}: {e}")
        card = dict(EMPTY_CARD)

    card["memory_version"] = "v1"
    if not card.get("company_name"):
        card["company_name"] = company_name

    return card


if __name__ == "__main__":
    result = build_user_summary("", data=["Google LLC is a multinational technology company that specializes in Internet-related services and products, which include online advertising technologies, a search engine, cloud computing, software, and hardware. It is best known for its search engine, Google Search, and its various services such as Google Maps, Google Drive, and YouTube, serving billions of users worldwide."])
    print("\nFinal Result:\n", result)