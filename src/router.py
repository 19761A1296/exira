# router.py
import re

from engine_adapter import ENGINE, HS2_DOMAIN_MAP
from hs_memory import collect_hs_from_memory
from persona.tweaked_exira import build_and_save_persona, load_persona, persona_to_text
from summarizer.session_summarizer import query_summary
from summarizer.classifier import classify_query
from summarizer.memory_answer import answer_from_memory

MAX_SESSION_QUERIES = 5
EXIT_WORDS = {"exit", "quit"}
YES = {"y", "yes", "ok", "proceed", "run"}


# ───────────────────────── memory ─────────────────────────

def build_memory(user_product_info, old_session, append_queries, persona) -> dict:
    return {
        "user_product_info": user_product_info or "",
        "old_session_summary": old_session or "",
        "current_session_queries": append_queries[-MAX_SESSION_QUERIES:],
        "persona": persona_to_text(persona) if persona else "",
    }


# ───────────────────────── console io ─────────────────────────

def render(resp: dict) -> list[str]:
    """Print engine messages, return the selectable options it offered."""
    messages = resp.get("messages", [])
    # the domain clarification text already lists the options, so don't echo them twice
    has_selector = any(m.get("type") == "domain_selector" for m in messages)

    options = []
    for m in messages:
        kind = m.get("type")
        if kind == "domain_selector":
            continue
        if kind in ("answer", "clarification"):
            print(f"\n{m['content']}\n")
        elif kind == "confirm":
            print(f"\nRun this?\n  {m['data']['resolved_query']}")
        elif kind == "followup":
            items = m["data"]["items"]
            if not has_selector:
                print(f"\n{m['content']}")
                for i, q in enumerate(items, 1):
                    print(f"  {i}. {q}")
            options = items
    return options


def read_input(options: list[str], selecting: bool) -> tuple[str, bool]:
    """Returns (text, picked_from_list)."""
    prompt = "\nSelect> " if selecting else "\nQuery> "
    raw = input(prompt).strip()
    if raw.isdigit() and options and 1 <= int(raw) <= len(options):
        picked = options[int(raw) - 1]
        print(f"-> {picked}")
        return picked, True
    return raw, False


# ───────────────────────── scope selection ─────────────────────────

def is_hs_digits(text: str) -> bool:
    return bool(re.fullmatch(r"\d{2,10}", re.sub(r"[.\-\s]", "", text or "")))


def clean_hs(text: str) -> str:
    return re.sub(r"[.\-\s]", "", text or "")


def enter_scope_manually() -> str | None:
    """Accepts an HS code OR a product name. Returns the raw choice, or None to quit."""
    raw = input("Enter an HS code (digits) or a product name, or 'exit': ").strip()
    if not raw or raw.lower() in EXIT_WORDS:
        return None
    return clean_hs(raw) if is_hs_digits(raw) else raw


def choose_scope(candidates: list[dict]) -> str | None:
    """None found -> ask. One found -> use it. Several -> pick, or type a code/product."""
    if not candidates:
        return enter_scope_manually()

    if len(candidates) == 1:
        pick = candidates[0]
        print(f"Using HS {pick['code']} - {pick['domain']} (from your profile).")
        return pick["code"]

    print("\nHS codes from your profile:")
    for i, c in enumerate(candidates, 1):
        print(f"  {i}. HS {c['code']} - {c['domain']}")
    print("  0. None of these - enter a different HS code or a product name")

    while True:
        raw = input("Select> ").strip()
        if not raw or raw.lower() in EXIT_WORDS:
            return None
        if raw == "0":
            return enter_scope_manually()
        if raw.isdigit() and 1 <= int(raw) <= len(candidates):
            return candidates[int(raw) - 1]["code"]
        # anything else: treat as an HS code or a product name
        return clean_hs(raw) if is_hs_digits(raw) else raw


def ask_scope_shift(current_label: str, candidates: list[dict]) -> str | None:
    """After a completed query. Enter keeps the scope, 'c' changes it.

    Returns the new choice, "" to keep, or None to quit.
    """
    raw = input(f"\n[{current_label}] Enter to continue, 'c' to change scope: ").strip().lower()
    if raw in EXIT_WORDS:
        return None
    if raw != "c":
        return ""
    picked = choose_scope(candidates)
    return "" if picked is None else picked


def remember_hs(candidates: list[dict], code: str) -> None:
    """Add a manually typed HS code to the list so it's offered next time."""
    if not is_hs_digits(code):
        return
    if any(c["code"] == code for c in candidates):
        return
    candidates.append({
        "code": code,
        "domain": HS2_DOMAIN_MAP.get(code[:2].zfill(2), "Unknown / Other"),
        "source": "current_session",
    })
    candidates.sort(key=lambda x: x["code"])


def starter_followups(hs_code: str) -> list[str]:
    return [
        f"Who are the top buyers of HS {hs_code} in the last 24 months?",
        f"Which countries are showing increasing demand for HS {hs_code} in the last 24 months?",
        f"Which month does demand for HS {hs_code} usually peak over the last 36 months?",
    ]


# ───────────────────────── session ─────────────────────────

def begin_session(old_session_memory: str = "", user_product_info: str = "",
                  user_id: str = "") -> str | None:

    append_queries: list[str] = []
    persona_turns: list[dict] = []
    persona = load_persona(user_id) if user_id else None

    memory = build_memory(user_product_info, old_session_memory, append_queries, persona)
    candidates = collect_hs_from_memory(memory, HS2_DOMAIN_MAP)

    choice = choose_scope(candidates)
    if not choice:
        return None

    hs_code = choice if is_hs_digits(choice) else None
    if hs_code:
        remember_hs(candidates, hs_code)
    scope_label = f"HS {hs_code}" if hs_code else choice

    sid = ENGINE.start()
    ENGINE.inject_memory(sid, memory)

    # a product name goes in as-is so the engine can run its HS domain probe
    options = render(ENGINE.send(sid, scope_label if hs_code else choice))
    selecting = ENGINE.scope_pending(sid)

    while True:
        try:
            text, from_list = read_input(options, selecting)
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not text or text.lower() in EXIT_WORDS:
            break

        memory = build_memory(user_product_info, old_session_memory, append_queries, persona)
        ENGINE.inject_memory(sid, memory)

        was_selecting = selecting

        try:
            if was_selecting:
                # this turn is an HS domain pick, not a question
                options = render(ENGINE.send(sid, text))
                selecting = ENGINE.scope_pending(sid)
                continue

            if not from_list and classify_query(text, memory) == "PERSONAL":
                print(f"\n{answer_from_memory(text, memory)}\n")
            else:
                resp = ENGINE.send(sid, text, source="followup" if from_list else None)
                options = render(resp)
                selecting = ENGINE.scope_pending(sid)

                if selecting:
                    # the question introduced a new product needing an HS pick
                    continue

                if resp.get("state") == "AWAITING_CONFIRMATION":
                    if input("Proceed? (y/n): ").strip().lower() in YES:
                        options = render(ENGINE.confirm(sid, "proceed"))
                    else:
                        render(ENGINE.confirm(sid, "cancel"))
        except Exception as exc:
            print(f"  error: {exc}\n")
            continue

        # ---- memory upkeep, only after a completed question ----
        append_queries.append(text)
        if len(append_queries) > MAX_SESSION_QUERIES:
            append_queries = [query_summary(append_queries)[0]]

        if user_id:
            persona_turns.append({"role": "user", "content": text})
            try:
                persona = build_and_save_persona(user_id, persona_turns,
                                                 recent_turns=1, persona=persona)
            except Exception as exc:
                print(f"  persona update failed: {exc}\n")

        # ---- scope shift, only after a completed question ----
        new_choice = ask_scope_shift(scope_label, candidates)
        if new_choice is None:
            break
        if new_choice and new_choice != (hs_code or scope_label):
            if is_hs_digits(new_choice):
                hs_code = new_choice
                scope_label = f"HS {hs_code}"
                remember_hs(candidates, hs_code)
                ENGINE.set_hs(sid, hs_code)
                options = starter_followups(hs_code)
                selecting = False
                print(f"\nSwitched to HS {hs_code}. Try:")
                for i, q in enumerate(options, 1):
                    print(f"  {i}. {q}")
            else:
                hs_code = None
                scope_label = new_choice
                print(f"\nSwitching to {new_choice}...")
                options = render(ENGINE.send(sid, new_choice))
                selecting = ENGINE.scope_pending(sid)

    if not append_queries:
        return None
    return query_summary(append_queries)[0]