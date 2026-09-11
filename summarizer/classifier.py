# summarizer/classifier.py
import os, json, requests
from dotenv import load_dotenv

load_dotenv()

PROMPT_ROUTE = """You are a router. Decide where a user's question should go.

TRADE    - anything answerable from customs shipment records: buyers, sellers,
           suppliers, demand, prices, trends, ports, countries, volumes,
           seasonality, competition, new entrants. Involves querying data.
PERSONAL - anything about the user's own company, their own profile, their own
           products, what they should do next, or the conversation itself
           (what did I ask, summarise this session, what do you know about me).

If a question mixes both, choose TRADE.
If you cannot tell, choose TRADE.

USER MEMORY (for context on who they are):
{memory}

QUESTION:
{question}

Return ONLY: {{"route": "TRADE" or "PERSONAL"}}
"""

URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.getenv("OPENROUTER_MODEL_CLASSIFIER", "anthropic/claude-haiku-4.5")


def classify_query(question: str, memory: dict) -> str:
    prompt = PROMPT_ROUTE.format(
        memory=json.dumps(memory, ensure_ascii=False, default=str)[:4000],
        question=question,
    )
    try:
        r = requests.post(
            URL,
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
            json={
                "model": MODEL,
                "temperature": 0,
                "max_tokens": 20,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        r.raise_for_status()
        out = json.loads(r.json()["choices"][0]["message"]["content"])
        return "PERSONAL" if out.get("route") == "PERSONAL" else "TRADE"
    except Exception:
        return "TRADE"          # fail toward the working pipeline