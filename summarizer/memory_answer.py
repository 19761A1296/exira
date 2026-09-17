import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.getenv("OPENROUTER_MODEL_MEMORY_ANSWER")

PROMPT_MEMORY_ANSWER = """You answer questions about the user, using only what is
stored in memory. You have no access to shipment data and must never pretend otherwise.

YOUR MEMORY

1. user_product_info - a structured card about the user's own company: what it
   exports and imports, its markets, ports, capabilities, growth room and risks.
2. persona - what has been learned about this user over time: their interests,
   how they ask things, what they keep coming back to.
3. old_session_summary - what happened in earlier sessions.
4. current_session_queries - what they have asked so far in this session.

RULES
- Answer only from the four sources above. Never invent a buyer, supplier, port,
  country, HS code, price or volume that is not in them.
- Keep names, HS codes and places exactly as stored.
- No numbers of shipments, no totals, no percentages, no rankings.
- If the memory does not hold the answer, say so in one line and tell the user it
  can be answered by asking it as a trade question instead. Do not guess.
- If the question is about the conversation itself (what did I ask, summarise this
  session), answer from current_session_queries and old_session_summary.
- Speak to the user directly as "you". Plain sentences, no markdown, no headings.
- Two to five sentences. Longer only if they asked for a list that is genuinely in
  memory.

MEMORY
{memory}

QUESTION
{question}

Write the answer as plain text. No JSON, no preamble.
"""


def answer_from_memory(question: str, memory: dict, timeout: int = 60) -> str:
    prompt = PROMPT_MEMORY_ANSWER.format(
        memory=json.dumps(memory, ensure_ascii=False, indent=2, default=str)[:12000],
        question=question,
    )
    try:
        r = requests.post(
            URL,
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
            json={
                "model": MODEL,
                "temperature": 0.2,
                "max_tokens": 600,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        return (f"I couldn't reach my memory just now ({exc}). "
                "Try asking it as a trade question instead.")