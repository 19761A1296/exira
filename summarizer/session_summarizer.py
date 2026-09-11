import json
import os

import requests
from summarizer.summarizer_prompts.summarizer_prompts import  PROMPT_QUERY_SUMMARY, PROMPT_SESSION_SUMMARY


from dotenv import load_dotenv

load_dotenv()



def query_summary(append_queries: list) -> list:
    """Summarize a list of session queries into a single summary string.

    Args:
        append_queries: list of query strings from one session.

    Returns:
        A list containing exactly one element: the summary string.
    """
    if not append_queries:
        return [""]

    text = "\n".join(f"{i}. {q}" for i, q in enumerate(append_queries, 1))

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}"},
        json={
            "model": os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": PROMPT_QUERY_SUMMARY},
                {"role": "user", "content": text},
            ],
        },
        timeout=60,
    )
    response.raise_for_status()
    raw = response.json()["choices"][0]["message"]["content"]
    data = json.loads(raw)

    summary = data.get("summary", "")
    if isinstance(summary, list):
        summary = " ".join(str(s) for s in summary)

    return [str(summary).strip()]





def session_summary(user_old_session_info: str,current_session_summary:str ) -> str:
    """Summarize a list of session queries into a single summary string.

    Args:
        user_old_session_info: It has old_session info.
        current_session_summary: Current session info.

    Returns:
        A a string : the summary string.
    """

    if user_old_session_info is None:
        user_old_session_info = ""
    
    if current_session_summary:
        text = user_old_session_info + "+" + current_session_summary
    else:
        return user_old_session_info

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}"},
        json={
            "model": os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": PROMPT_SESSION_SUMMARY},
                {"role": "user", "content": text},
            ],
        },
        timeout=60,
    )
    response.raise_for_status()
    raw = response.json()["choices"][0]["message"]["content"]
    data = json.loads(raw)

    summary = data.get("summary", "")
    
    # print("line 115....",type(summary),summary)
    return summary


if __name__ == "__main__":
    pass 
    # queries = [
    #     "how to connect postgres in fastapi",
    #     "sqlalchemy async session example",
    #     "alembic migration not detecting new column",
    #     "best way to pool connections in production",
    # ]
    # print(query_summary(queries))


    # user_old_session_info = "Major biscuit buyers include retailers, distributors, supermarkets, and food-service companies seeking branded and private-label biscuits for domestic and international markets."
    # current_session_summary = "Cotton sellers include ginners, traders, mills, and exporters supplying raw cotton and cotton yarn to textile manufacturers, garment producers, and international apparel businesses."
    # print(session_summary(user_old_session_info, current_session_summary))