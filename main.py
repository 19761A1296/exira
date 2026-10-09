"""main.py — CLI entry point.

    user id  →  pick a company  →  pick a session  →  chat

A user may hold up to 5 companies, and each company up to 10 sessions. A
session keeps its own summary, persona, scope and its newest 20 answers, so
reopening one days later picks up where it left off.

    python main.py
"""

from database import info_database as db
from summarizer.session_summarizer import session_summary
from src.run_pipeline import run_pipeline
from src.router import begin_session

EXIT_WORDS = {"exit", "quit", "q"}


# ───────────────────────── small helpers ─────────────────────────

def ask(prompt: str) -> str:
    """Read a line. Returns "" on Ctrl-C or Ctrl-D so callers can bail out."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def when(row: dict) -> str:
    stamp = row.get("last_used_at") or row.get("created_at") or ""
    return stamp[:10] or "never"


# ───────────────────────── companies ─────────────────────────

def add_company(userid: str) -> int | None:
    """Run onboarding and store the result as a new company. None if it fails."""
    if db.company_slots_free(userid) <= 0:
        print("You already have 5 companies. Delete one first.")
        return None

    print("\nTell me about the company. Either field alone is enough.")
    text = ask("  Company name or description: ")
    urls = ask("  Company URLs: ")

    if not text and not urls:
        print("Nothing given, so nothing to build.")
        return None

    profile = run_pipeline(text, urls)
    if not profile:
        print("No profile could be built for that.")
        return None

    name = text or "Company"
    company_id = db.create_company(userid, company_name=name,
                                   company_profile=profile)

    if company_id is None:
        print("That company could not be added — the name may already be in use.")
        return None

    print(f"\nAdded '{name}'.")
    return company_id


def choose_company(userid: str) -> int | None:
    """List the user's companies and let them pick, add or delete. None to quit."""
    while True:
        companies = db.list_companies(userid)
        free = db.company_slots_free(userid)

        if not companies:
            print("\nNo companies yet. Let's add your first one.")
            company_id = add_company(userid)
            if company_id is None:
                return None
            continue

        print(f"\nYour companies ({len(companies)} of 5)")
        for i, c in enumerate(companies, 1):
            print(f"  {i}. {c['company_name']}")
            print(f"     company {c['slot']} · {c['session_count']} of 10 sessions"
                  f" · last used {when(c)}")

        if free:
            print(f"  a. Add a company ({free} slot{'s' if free != 1 else ''} free)")
        print("  d. Delete a company")
        print("  q. Quit")

        choice = ask("Select> ").lower()

        if not choice or choice in EXIT_WORDS:
            return None

        if choice == "a" and free:
            add_company(userid)
            continue

        if choice == "d":
            target = ask("  Which number to delete? ")
            if target.isdigit() and 1 <= int(target) <= len(companies):
                victim = companies[int(target) - 1]
                sure = ask(f"  Delete '{victim['company_name']}' and all its "
                           "sessions? (y/n) ").lower()
                if sure in ("y", "yes"):
                    db.delete_company(victim["company_id"])
                    print("  Deleted.")
            continue

        if choice.isdigit() and 1 <= int(choice) <= len(companies):
            picked = companies[int(choice) - 1]
            db.touch_company(picked["company_id"])
            return picked["company_id"]

        print("  Enter a number from the list.")


# ───────────────────────── sessions ─────────────────────────

def choose_session(company_id: int) -> tuple[int | None, bool]:
    """Pick a session to resume, or start a new one.

    Returns (session_id, is_new). session_id is None to go back.
    """
    while True:
        sessions = db.list_sessions(company_id)
        free = db.session_slots_free(company_id)
        company = db.get_company(company_id)
        name = company["company_name"] if company else "this company"

        print(f"\nSessions for {name} ({len(sessions)} of 10)")

        if not sessions:
            print("  none yet")

        for i, s in enumerate(sessions, 1):
            print(f"  {i}. session {s['slot']} · {s['scope_label'] or 'no scope yet'}")
            print(f"     {s['answer_count']} answers · last used {when(s)}")

        if free:
            print(f"  n. New session ({free} slot{'s' if free != 1 else ''} free)")
        print("  d. Delete a session")
        print("  b. Back to companies")

        choice = ask("Select> ").lower()

        if not choice or choice in EXIT_WORDS or choice == "b":
            return None, False

        if choice == "n" and free:
            return None, True

        if choice == "d":
            target = ask("  Which number to delete? ")
            if target.isdigit() and 1 <= int(target) <= len(sessions):
                victim = sessions[int(target) - 1]
                sure = ask(f"  Delete session {victim['slot']}? (y/n) ").lower()
                if sure in ("y", "yes"):
                    db.delete_session(victim["session_id"])
                    print("  Deleted.")
            continue

        if choice.isdigit() and 1 <= int(choice) <= len(sessions):
            picked = sessions[int(choice) - 1]
            db.touch_session(picked["session_id"])
            return picked["session_id"], False

        print("  Enter a number from the list.")


def show_recent_answers(session_id: int, limit: int = 5) -> None:
    """A short reminder of where this session left off."""
    answers = db.get_answers(session_id)
    if not answers:
        return

    print(f"\nWhere you left off — last {min(limit, len(answers))} of "
          f"{len(answers)} answers")
    for item in answers[-limit:]:
        question = item.get("question") or "(no question recorded)"
        answer = " ".join(item["answer"].split())
        print(f"\n  Q: {question}")
        print(f"  A: {answer[:300]}{'...' if len(answer) > 300 else ''}")


# ───────────────────────── the run ─────────────────────────

def run_session(company_id: int, session_id: int | None, is_new: bool) -> None:
    """Open one chat session and save it when the user leaves."""
    company = db.get_company(company_id)
    company_profile = (company["company_profile"] or "") if company else ""

    if is_new:
        session_id = db.create_session(company_id)
        if session_id is None:
            print("That company already has 10 sessions. Delete one first.")
            return
        print(f"\nStarted session {db.get_session(session_id)['slot']}.")
        old_summary = ""
    else:
        row = db.get_session(session_id)
        if not row:
            print("That session is no longer there.")
            return
        old_summary = row["session_summary"] or ""
        print(f"\nResuming session {row['slot']}"
              f"{' · ' + row['scope_label'] if row['scope_label'] else ''}.")
        show_recent_answers(session_id)

    current = begin_session(old_summary, company_profile, session_id)

    notes = old_summary
    if current:
        try:
            notes = session_summary(old_summary, current)
        except Exception as exc:
            print(f"Session merge failed: {exc}")
            notes = current

    db.save_session(session_id, session_summary=notes or "", ended=True)
    db.touch_company(company_id)

    print("\nSession saved.")
    if notes:
        print("\nSession notes:")
        print(notes)


def main() -> None:
    db.init_db()

    userid = ask("Enter your userid: ")
    if not userid:
        print("userid cannot be empty.")
        return

    db.ensure_user(userid)

    while True:
        company_id = choose_company(userid)
        if company_id is None:
            print("\nBye.")
            return

        while True:
            session_id, is_new = choose_session(company_id)
            if session_id is None and not is_new:
                break                      # back to the company list
            run_session(company_id, session_id, is_new)


if __name__ == "__main__":
    main()