"""
EXIRA persona memory — Phase 1 (initial build) + Phase 2 (incremental update).

Lifted out of tweaked_exira.ipynb into a single importable module.
Function names are unchanged from the notebook.

.env
----
    OPENROUTER_API_KEY="sk-or-v1-..."
    PERSONA_LLM_MODEL="google/gemma-4-31b-it"     # optional

Storage
-------
    persona/persona_db/<user_id>.json           # one file per user
"""

import json
import os
import re
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

PERSONA_DIR = Path(__file__).resolve().parent / "persona_db"
PERSONA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_PERSONA_MODEL = os.getenv("PERSONA_LLM_MODEL", "google/gemma-4-31b-it")

PERSONA_KEYS = [
    "user_id", "created_at", "last_updated",
    "business_context", "trade_profile", "product_interests",
    "market_interests", "entity_interests", "preferred_analysis_style",
    "preferred_metrics_and_filters", "top_of_mind", "brief_history",
    "long_term_background",
]

# the sections that actually get rendered into the router context
_CONTEXT_SECTIONS = [
    "business_context", "trade_profile", "product_interests",
    "market_interests", "entity_interests", "preferred_analysis_style",
    "preferred_metrics_and_filters", "top_of_mind", "brief_history",
    "long_term_background",
]


# -------------------------
# storage
# -------------------------

def _safe_user_id(user_id: str) -> str:
    """Keep a user id usable as a filename."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", str(user_id or "").strip())
    return cleaned or "anonymous"


def persona_path(user_id: str) -> Path:
    return PERSONA_DIR / f"{_safe_user_id(user_id)}.json"


def persona_exists(user_id: str) -> bool:
    return persona_path(user_id).exists()


# -------------------------
# LLM plumbing
# -------------------------

def get_openrouter_response(messages, api_key=None, model_name=None, temperature=0.1, max_tokens=2500):
    api_key = api_key or os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set in the environment/.env")

    model_name = model_name or DEFAULT_PERSONA_MODEL

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://exira-persona-exposure-test.local",
        "X-Title": "EXIRA Persona Exposure Test",
    }

    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    response = requests.post(url, headers=headers, json=payload, timeout=120)
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


def extract_json(text):
    text = (text or "").strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        return json.loads(match.group(0))

    raise ValueError("Could not parse JSON from LLM output.")


# -------------------------
# persona memory helpers
# -------------------------

def empty_persona(user_id):
    today = str(date.today())
    return {
        "user_id": user_id,
        "created_at": today,
        "last_updated": today,
        "business_context": "Not enough information is available yet to determine the user's business context.",
        "trade_profile": "Not enough information is available yet to determine how the user uses EXIRA.",
        "product_interests": "Not enough information is available yet to determine product interests.",
        "market_interests": "Not enough information is available yet to determine market interests.",
        "entity_interests": "Not enough information is available yet to determine entity interests.",
        "preferred_analysis_style": "Not enough information is available yet to determine preferred analysis style.",
        "preferred_metrics_and_filters": "Not enough information is available yet to determine preferred metrics and filters.",
        "top_of_mind": "Not enough information is available yet to determine the user's current focus.",
        "brief_history": "No meaningful prior conversation history is available yet.",
        "long_term_background": "Not enough information is available yet to determine stable long-term behavior.",
    }


def load_persona(user_id):
    path = persona_path(user_id)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                persona = json.load(f)
            if isinstance(persona, dict):
                base = empty_persona(user_id)
                base.update({k: v for k, v in persona.items() if k in PERSONA_KEYS})
                base["user_id"] = user_id
                return base
        except Exception:
            pass
    return empty_persona(user_id)


def save_persona(persona):
    path = persona_path(persona.get("user_id", ""))
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(persona, f, indent=2, ensure_ascii=False)
    tmp.replace(path)
    return path


PHASE_1_SYSTEM_PROMPT = """
You are the EXIRA Initial User Persona Memory Generator.

EXIRA is a trade intelligence chatbot used for shipment-data analysis, buyer discovery, supplier discovery, competitor analysis, HS code analysis, market research, logistics analysis, and trade decision support.

Your task is to read the raw conversation history between a user and EXIRA and generate the user's initial structured persona memory.

Return only valid JSON.
Do not return markdown.
Do not return explanation.
Do not add comments.
Do not wrap the JSON in code fences.
Do not add any keys outside the schema.

The memory should be similar to a Claude-style structured memory: compact, section-based, meaningful, and bounded by word limits.

You must not summarize every message.
You must extract only durable, reusable, personalization-worthy information.

The memory should help EXIRA understand the user in future conversations.

Do not store greetings, thanks, small talk, temporary UI actions, or one-time commands unless they reveal a stable preference.

Do not invent information.
Do not assume the user's business role unless there is evidence.
If something is uncertain, write it cautiously using phrases like:
- "The user appears to..."
- "The user has shown interest in..."
- "The conversation suggests..."

Use clear business language.
Avoid vague generic summaries.
Avoid repeating the same information across multiple sections.

You must generate all sections in the schema.

SECTION DEFINITIONS:

1. business_context
Describe the user's apparent business role, company type, industry context, or work context if available.
Examples:
- exporter
- importer
- manufacturer
- sourcing team
- trade analyst
- sales user
- logistics user
- compliance user
- market research user

If unknown, write:
"Not enough information is available yet to determine the user's business context."

Maximum 60 words.

2. trade_profile
Describe how the user appears to use EXIRA for trade intelligence.
Focus on trade behavior such as:
- buyer discovery
- supplier discovery
- competitor tracking
- shipment-data analysis
- market prioritization
- pricing analysis
- demand analysis
- compliance guidance
- HS code exploration
- logistics or port analysis

Maximum 80 words.

3. product_interests
Summarize products, commodities, HS categories, sectors, or industries the user repeatedly asks about or appears to care about.

Mention long-term interests separately from newer/active interests when possible.

Maximum 80 words.

4. market_interests
Summarize countries, regions, origin markets, destination markets, or trade lanes the user focuses on.

Mention current active markets and recurring markets when possible.

Maximum 80 words.

5. entity_interests
Summarize important buyers, suppliers, competitors, ports, HS codes, or companies the user has shown interest in.

Do not list every entity if there are many.
Keep only the most important or repeated entities.

Maximum 100 words.

6. preferred_analysis_style
Summarize the type of analysis the user prefers.

Examples:
- active buyer lists
- competitor comparisons
- buyer prioritization
- market-entry recommendations
- shipment trend analysis
- pricing comparison
- HS code explanation
- step-by-step technical implementation
- short direct business recommendations

Maximum 80 words.

7. preferred_metrics_and_filters
Summarize metrics, filters, and time ranges the user prefers.

Examples:
- shipment count
- FOB value
- CIF value
- quantity
- unit price
- growth rate
- last shipment date
- buyer frequency
- supplier count
- last 6 months
- last 12 months
- active buyers
- recent shipments
- premium buyers

Maximum 100 words.

8. top_of_mind
Summarize the user's most recent active focus from the latest part of the conversation.

This section should capture what the user is working on right now.
It can change frequently in future updates.

Maximum 80 words.

9. brief_history
Summarize important recent themes from the conversation history.

This should not be a full timeline.
Mention only meaningful workstreams, repeated themes, products, markets, decisions, or technical directions.

Maximum 150 words.

10. long_term_background
Summarize stable long-term behavior, domain, repeated preferences, and recurring EXIRA usage pattern.

This should be more stable than top_of_mind.

Maximum 100 words.

STRICT WORD LIMITS:

- business_context: maximum 60 words
- trade_profile: maximum 80 words
- product_interests: maximum 80 words
- market_interests: maximum 80 words
- entity_interests: maximum 100 words
- preferred_analysis_style: maximum 80 words
- preferred_metrics_and_filters: maximum 100 words
- top_of_mind: maximum 80 words
- brief_history: maximum 150 words
- long_term_background: maximum 100 words

If the conversation does not contain enough information for a section, write a short cautious statement rather than inventing details.

OUTPUT RULES:

1. Return exactly one JSON object.
2. Use the exact schema provided.
3. Do not add extra top-level keys.
4. Do not remove any keys.
5. Use valid JSON with double quotes.
6. Do not use trailing commas.
7. Do not use null values.
8. Use empty strings only if absolutely no information is available.
9. Keep the memory compact and useful.
10. Do not include raw conversation excerpts unless needed as concise evidence inside the summary.
11. Do not mention that you are an AI or that you are summarizing.
12. Do not include markdown formatting.

OUTPUT JSON SCHEMA:

{
  "user_id": "string",
  "created_at": "YYYY-MM-DD",
  "last_updated": "YYYY-MM-DD",
  "business_context": "string",
  "trade_profile": "string",
  "product_interests": "string",
  "market_interests": "string",
  "entity_interests": "string",
  "preferred_analysis_style": "string",
  "preferred_metrics_and_filters": "string",
  "top_of_mind": "string",
  "brief_history": "string",
  "long_term_background": "string"
}

FINAL INSTRUCTION:
Read the full input conversation carefully and return only the initial EXIRA persona memory JSON.

"""

PHASE_2_SYSTEM_PROMPT = """
You are the EXIRA User Persona Memory Updater.

EXIRA is a trade intelligence chatbot used for shipment-data analysis, buyer discovery, supplier discovery, competitor analysis, HS code analysis, market research, logistics analysis, compliance guidance, and trade decision support.

Your task is to update an existing structured user persona memory using:
1. existing_user_persona
2. new_session_conversation

Return only valid JSON.
Do not return markdown.
Do not return explanation.
Do not add comments.
Do not wrap the JSON in code fences.
Do not add keys outside the schema.

The output must follow the same schema as the existing_user_persona.

Your goal is to preserve stable long-term user memory while incorporating meaningful new information from the new session.

This is not a normal chat summary task.
This is a controlled persona memory update task.

You must:
- Keep important existing persona information.
- Add new stable information from the new session.
- Reinforce existing interests if the new session repeats them.
- Update top_of_mind based mainly on the latest session.
- Move older top_of_mind items into brief_history if still important.
- Update product_interests if the user shifts to or adds new products.
- Update market_interests if the user adds, removes, or prioritizes markets.
- Update entity_interests if the user focuses on new buyers, suppliers, competitors, ports, HS codes, or companies.
- Update preferred_analysis_style if the user shows a new repeated analysis preference.
- Update preferred_metrics_and_filters if the user asks for new metrics, filters, or time ranges.
- Keep long_term_background stable unless the new session clearly changes the user’s long-term behavior.
-Do not delete old important memory.
-Keep stable interests.
-Mark new interests as active/current.
-Move old active focus into brief_history if it is no longer current.


Do not:
- Store greetings, thanks, small talk, or temporary UI actions.
- Invent facts not supported by the existing persona or new session.
- Treat assistant suggestions as user preferences unless the user accepts, asks follow-up, or clearly focuses on them.
- Overwrite long-term memory just because the latest session has a new topic.
- Remove important older memory unless contradicted or clearly outdated.
- Expand the memory endlessly.
- Exceed the word limits.

SECTION DEFINITIONS AND UPDATE RULES:

1. business_context
Purpose:
The user's apparent role, business type, company type, industry context, or work context.

Update rule:
Usually stable. Update only if the new session gives stronger evidence about the user's role or business context.

Maximum 60 words.

2. trade_profile
Purpose:
How the user uses EXIRA for trade intelligence.

Update rule:
Update if new sessions show new recurring use cases such as compliance checking, supplier discovery, pricing analysis, logistics planning, or market monitoring.

Maximum 80 words.

3. product_interests
Purpose:
Products, commodities, HS categories, sectors, or industries the user cares about.

Update rule:
Preserve long-term products. Add new products if the user clearly focuses on them. Mention shifts between old and new active focus.

Maximum 80 words.

4. market_interests
Purpose:
Countries, regions, origin markets, destination markets, or trade lanes the user focuses on.

Update rule:
Preserve important recurring markets. Add new active markets. If the user deprioritizes a market, describe the updated priority.

Maximum 80 words.

5. entity_interests
Purpose:
Important buyers, suppliers, competitors, ports, HS codes, or companies the user tracks.

Update rule:
Keep only the most important or repeated entities. Add new entities if the user asks follow-up questions about them or uses them for decisions.

Maximum 100 words.

6. preferred_analysis_style
Purpose:
The type of analysis the user prefers.

Examples:
active buyer lists, competitor comparisons, market-entry recommendations, shipment trend analysis, pricing comparison, supplier discovery, compliance guidance, short direct recommendations, tables, step-by-step analysis.

Update rule:
Update when the new session shows a new analysis style or reinforces an existing one.

Maximum 80 words.

7. preferred_metrics_and_filters
Purpose:
Metrics, filters, and time ranges the user prefers.

Examples:
shipment count, FOB value, CIF value, quantity, unit price, growth rate, last shipment date, buyer frequency, supplier count, market share, last 6 months, last 12 months, active buyers, premium buyers.

Update rule:
Add new metrics/filters only if user asks for them directly or repeatedly.

Maximum 100 words.

8. top_of_mind
Purpose:
The user's current active focus from the latest session.

Update rule:
This should change most frequently. It should reflect what the user is working on right now, not the entire history.

Maximum 80 words.

9. brief_history
Purpose:
A compact history of important recent themes across sessions.

Update rule:
Move older top_of_mind topics here if still important. Keep it concise and chronological enough to understand recent direction.

Maximum 150 words.

10. long_term_background
Purpose:
Stable long-term behavior, domain, repeated preferences, and recurring EXIRA usage pattern.

Update rule:
Change slowly. Only update if the new session reveals a durable long-term pattern.

Maximum 100 words.

WORD LIMITS:
- business_context: maximum 60 words
- trade_profile: maximum 80 words
- product_interests: maximum 80 words
- market_interests: maximum 80 words
- entity_interests: maximum 100 words
- preferred_analysis_style: maximum 80 words
- preferred_metrics_and_filters: maximum 100 words
- top_of_mind: maximum 80 words
- brief_history: maximum 150 words
- long_term_background: maximum 100 words

OUTPUT RULES:
1. Return exactly one JSON object.
2. Use the exact schema below.
3. Do not add extra top-level keys.
4. Do not remove any keys.
5. Use valid JSON with double quotes.
6. Do not use trailing commas.
7. Do not use null values.
8. Keep the memory compact and useful.
9. Preserve important older information.
10. Update only where the new session supports an update.
11. Do not include raw conversation excerpts.
12. Do not mention that you are an AI or that you are summarizing.
13. Do not include markdown formatting.

OUTPUT JSON SCHEMA:

{
  "user_id": "string",
  "created_at": "YYYY-MM-DD",
  "last_updated": "YYYY-MM-DD",
  "business_context": "string",
  "trade_profile": "string",
  "product_interests": "string",
  "market_interests": "string",
  "entity_interests": "string",
  "preferred_analysis_style": "string",
  "preferred_metrics_and_filters": "string",
  "top_of_mind": "string",
  "brief_history": "string",
  "long_term_background": "string"
}

FINAL INSTRUCTION:
Read existing_user_persona and new_session_conversation carefully. Return only the updated EXIRA persona memory JSON.

"""

# -------------------------
# conversation normalisation
# -------------------------

def _as_conversation(messages_for_memory):
    """Accept either chat messages or a plain list of query strings.

    The router only has user queries, the notebook had full chat messages.
    Both are coerced to [{"role": ..., "content": ...}, ...].
    """
    if not messages_for_memory:
        return []

    if isinstance(messages_for_memory, str):
        return [{"role": "user", "content": messages_for_memory}]

    turns = []
    for item in messages_for_memory:
        if isinstance(item, dict):
            role = str(item.get("role") or "user")
            content = item.get("content")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            content = content.strip()
            if content:
                turns.append({"role": role, "content": content})
        else:
            content = str(item or "").strip()
            if content:
                turns.append({"role": "user", "content": content})
    return turns


# -------------------------
# Phase 1 / Phase 2 driver
# -------------------------

def update_persona_after_turn(
    persona,
    messages_for_memory,
    user_id,
    api_key=None,
    persona_model=None,
    recent_turns=None,
):
    """Run Phase 1 if the persona is still empty, otherwise Phase 2.

    Args:
        persona: the current persona dict (use load_persona(user_id)).
        messages_for_memory: chat messages, or a plain list of query strings.
        user_id: the user this persona belongs to.
        api_key: defaults to OPENROUTER_API_KEY.
        persona_model: defaults to PERSONA_LLM_MODEL.
        recent_turns: for Phase 2, how many trailing turns count as the new
                      session. None (default) sends the whole list, which is
                      what you want when updating once at end of session.
                      Pass 2 to reproduce the notebook's per-turn behaviour.

    Returns:
        the updated persona dict (not saved to disk — call save_persona).
    """
    today = str(date.today())
    conversation = _as_conversation(messages_for_memory)

    if not conversation:
        return persona

    persona = persona or empty_persona(user_id)
    has_real_memory = not str(persona.get("business_context", "")).startswith(
        "Not enough information"
    )

    if not has_real_memory:
        user_payload = {
            "user_id": user_id,
            "date": today,
            "raw_conversation_history": conversation,
        }
        messages = [
            {"role": "system", "content": PHASE_1_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, indent=2)},
        ]
    else:
        new_session = conversation if recent_turns is None else conversation[-recent_turns:]
        user_payload = {
            "user_id": user_id,
            "date": today,
            "existing_user_persona": persona,
            "new_session_conversation": new_session,
        }
        messages = [
            {"role": "system", "content": PHASE_2_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, indent=2)},
        ]

    raw = get_openrouter_response(
        messages=messages,
        api_key=api_key,
        model_name=persona_model,
        temperature=0.0,
        max_tokens=2500,
    )

    updated = extract_json(raw)
    if not isinstance(updated, dict):
        return persona

    for key in PERSONA_KEYS:
        if key not in updated or updated[key] is None:
            updated[key] = persona.get(key, "")

    updated = {k: updated[k] for k in PERSONA_KEYS}
    updated["user_id"] = user_id
    updated["last_updated"] = today

    if not updated.get("created_at"):
        updated["created_at"] = persona.get("created_at", today)

    return updated


# -------------------------
# rendering for the router
# -------------------------

def persona_to_text(persona) -> str:
    """Flatten a persona dict into the free-text block the router expects."""
    if not isinstance(persona, dict):
        return ""

    lines = []
    for key in _CONTEXT_SECTIONS:
        value = str(persona.get(key, "") or "").strip()
        if not value or value.startswith("Not enough information") or value.startswith("No meaningful"):
            continue
        lines.append(f"{key}: {value}")

    return "\n".join(lines)


def get_persona_context(user_id) -> str:
    """One-liner for callers that just want the text block."""
    return persona_to_text(load_persona(user_id))


def build_and_save_persona(
    user_id,
    messages_for_memory,
    api_key=None,
    persona_model=None,
    recent_turns=None,
    persona=None,
):
    """Load -> Phase 1/2 -> save.

    Args:
        persona: pass the in-memory persona to skip the disk read. Useful when
                 updating after every turn, where the caller already holds the
                 current persona from the previous turn.
        recent_turns: forwarded to update_persona_after_turn. Pass 2 for
                      per-turn updates (only the newest user+assistant pair is
                      new); leave None when updating once per session.
    """
    persona = persona if persona is not None else load_persona(user_id)
    updated = update_persona_after_turn(
        persona=persona,
        messages_for_memory=messages_for_memory,
        user_id=user_id,
        api_key=api_key,
        persona_model=persona_model,
        recent_turns=recent_turns,
    )
    save_persona(updated)
    return updated


if __name__ == "__main__":
    demo_user = "demo_user"
    demo_turns = [
        "Who are the active buyers for HS 300490 in Vietnam?",
        "Show me their shipment counts for the last 6 months.",
        "Which of them switched suppliers recently?",
    ]
    result = build_and_save_persona(demo_user, demo_turns)
    print(json.dumps(result, indent=2))
    print("\nsaved to:", persona_path(demo_user))
    print("\nrouter context block:\n")
    print(persona_to_text(result))