"""info_database.py — storage for users, companies, sessions and answers.

    users      one row per person
      └─ companies   up to 5 per user      slot 1..5    the company card
           ├─ onboarding_reports   exactly one per company, written once
           └─ sessions   up to 10 per company  slot 1..10   summary, persona, scope
                └─ answers   newest 20 per session         question + answer

Ids and slots are different things on purpose:
  company_id / session_id   auto-generated, globally unique, used as foreign keys
  slot                      1..5 and 1..10, restarts per parent, what the user sees

So "c1-s3" means company slot 1, session slot 3 — the row ids behind it might be
company_id 7 and session_id 42. Deleting company 2 frees slot 2 for reuse without
disturbing anything that referenced the old company_id.

Caps are enforced by UNIQUE (user_id, slot) and UNIQUE (company_id, slot), so a
bug cannot create a sixth company or an eleventh session even under a race.
On Postgres the parent row is also locked while a slot is picked, so two
simultaneous "new session" clicks get different slots instead of one failing.

Backend is chosen once at import:
    DATABASE_URL set   -> PostgreSQL, through a connection pool  (production)
    DATABASE_URL unset -> SQLite file database/exira.db           (local POC)

The public functions are identical for both, so callers don't change.

Env:
    DATABASE_URL     postgresql://user:password@host:5432/dbname
                     (URL-encode special characters in the password, e.g. @ -> %40)
    DB_POOL_MIN      connections kept open, default 1
    DB_POOL_MAX      most connections at once, default 10
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DB_NAME = Path(__file__).parent / "exira.db"
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
IS_PG = bool(DATABASE_URL)

MAX_COMPANIES = 5
MAX_SESSIONS = 10
MAX_ANSWERS = 20


# ───────────────────────── helpers ─────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_text(value):
    """Store dicts/lists as JSON text, everything else as str."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _from_text(value):
    """Try to decode JSON back into a dict/list, else return raw text."""
    if value is None:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


# ───────────────────────── connections ─────────────────────────

if IS_PG:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    _POOL = ConnectionPool(
        DATABASE_URL,
        min_size=int(os.getenv("DB_POOL_MIN", "1")),
        max_size=int(os.getenv("DB_POOL_MAX", "10")),
        kwargs={"row_factory": dict_row},
        check=ConnectionPool.check_connection,      # drop dead connections
        open=True,
    )
    import atexit
    atexit.register(_POOL.close)   
    _INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)
    print("[info_database] using PostgreSQL")
else:
    _POOL = None
    _INTEGRITY_ERRORS = (sqlite3.IntegrityError,)
    print(f"[info_database] DATABASE_URL not set: using SQLite ({DB_NAME.name})")


class _Conn:
    """Same calls for both backends: SQL is written with ? placeholders."""

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=()):
        if IS_PG:
            sql = sql.replace("?", "%s")
        return self.raw.execute(sql, params)

    def rollback(self):
        self.raw.rollback()


@contextmanager
def get_conn():
    """One transaction: commits on success, rolls back on error."""
    if IS_PG:
        with _POOL.connection() as raw:      # commit / rollback handled by the pool
            yield _Conn(raw)
        return

    raw = sqlite3.connect(DB_NAME, timeout=30)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")      # per connection, not per database
    try:
        yield _Conn(raw)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def _lock_parent(conn, table, key_col, key):
    """Postgres: hold the parent row so concurrent slot picks queue up."""
    if IS_PG:
        conn.execute(f"SELECT 1 FROM {table} WHERE {key_col} = ? FOR UPDATE", (key,))


def _next_slot(conn, table, parent_col, parent_id, cap):
    """Lowest free slot, or None when full. Reuses a gap left by a delete."""
    rows = conn.execute(
        f"SELECT slot FROM {table} WHERE {parent_col} = ?", (parent_id,)
    ).fetchall()
    taken = {r["slot"] for r in rows}
    for n in range(1, cap + 1):
        if n not in taken:
            return n
    return None


# ───────────────────────── schema ─────────────────────────
#
# Timestamps stay as ISO-8601 text on both backends, so values read back
# are the same strings the app already handles.

_ID_SQLITE = "INTEGER PRIMARY KEY AUTOINCREMENT"
_ID_PG = "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
_FK_INT_SQLITE = "INTEGER"
_FK_INT_PG = "BIGINT"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id      TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS companies (
    company_id        {ID},
    user_id           TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    slot              INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 5),
    company_name      TEXT NOT NULL,
    company_profile   TEXT,
    onboarding_report TEXT,
    created_at        TEXT NOT NULL,
    last_used_at      TEXT,
    UNIQUE (user_id, slot),
    UNIQUE (user_id, company_name)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id      {ID},
    company_id      {FK} NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    slot            INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 10),
    label           TEXT,
    session_summary TEXT,
    persona         TEXT,
    scope_label     TEXT,
    hs_code         TEXT,
    engine_sid      TEXT,
    created_at      TEXT NOT NULL,
    last_used_at    TEXT,
    ended_at        TEXT,
    UNIQUE (company_id, slot)
);

CREATE TABLE IF NOT EXISTS onboarding_reports (
    company_id {FK} PRIMARY KEY REFERENCES companies(company_id) ON DELETE CASCADE,
    report     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS answers (
    answer_id   {ID},
    session_id  {FK} NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    question    TEXT,
    answer      TEXT NOT NULL,
    trace       TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_companies_user ON companies(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_company ON sessions(company_id);
CREATE INDEX IF NOT EXISTS idx_answers_session ON answers(session_id, answer_id);
"""


def _schema() -> str:
    if IS_PG:
        return SCHEMA.replace("{ID}", _ID_PG).replace("{FK}", _FK_INT_PG)
    return SCHEMA.replace("{ID}", _ID_SQLITE).replace("{FK}", _FK_INT_SQLITE)


def init_db():
    """Create the tables if missing. Safe to call on every start."""
    with get_conn() as conn:
        for statement in _schema().split(";"):
            if statement.strip():
                conn.execute(statement)
        _migrate(conn)


def _columns(conn, table) -> set:
    if IS_PG:
        rows = conn.execute(
            "SELECT column_name AS name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = ?", (table,))
    else:
        rows = conn.execute(f"PRAGMA table_info({table})")
    return {r["name"] for r in rows.fetchall()}


def _migrate(conn):
    """Add columns that older databases are missing. Safe to re-run."""
    if "engine_sid" not in _columns(conn, "sessions"):
        conn.execute("ALTER TABLE sessions ADD COLUMN engine_sid TEXT")
    if "trace" not in _columns(conn, "answers"):
        conn.execute("ALTER TABLE answers ADD COLUMN trace TEXT")


# ───────────────────────── users ─────────────────────────

def user_exists(user_id) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (str(user_id),)
        ).fetchone()
    return row is not None


def ensure_user(user_id) -> str:
    """Create the user if new, and stamp last_seen_at. Returns the id."""
    user_id = str(user_id)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at) VALUES (?, ?)"
            " ON CONFLICT DO NOTHING",
            (user_id, _now()))
        conn.execute(
            "UPDATE users SET last_seen_at = ? WHERE user_id = ?",
            (_now(), user_id))
    return user_id


def list_users() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id FROM users"
        ).fetchall()
    return [r["user_id"] for r in rows]


def delete_user(user_id) -> bool:
    """Removes the user and, by cascade, every company, session and answer."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM users WHERE user_id = ?", (str(user_id),))
        return cur.rowcount > 0


# ───────────────────────── companies ─────────────────────────

def create_company(user_id, company_name, company_profile="",
                   onboarding_report="") -> int | None:
    """New company in the lowest free slot. None when the user has 5 already."""
    user_id = ensure_user(user_id)
    now = _now()

    with get_conn() as conn:
        _lock_parent(conn, "users", "user_id", user_id)
        slot = _next_slot(conn, "companies", "user_id", user_id, MAX_COMPANIES)
        if slot is None:
            return None

        try:
            row = conn.execute(
                "INSERT INTO companies (user_id, slot, company_name, company_profile,"
                " created_at, last_used_at) VALUES (?,?,?,?,?,?)"
                " RETURNING company_id",
                (user_id, slot, str(company_name), _to_text(company_profile),
                 now, now)).fetchone()
        except _INTEGRITY_ERRORS:
            conn.rollback()
            return None                 # same company name for this user

        company_id = row["company_id"]

        if onboarding_report:
            # written once, at onboarding, and never touched again
            conn.execute(
                "INSERT INTO onboarding_reports (company_id, report, created_at)"
                " VALUES (?,?,?) ON CONFLICT DO NOTHING",
                (company_id, _to_text(onboarding_report), now))

        return company_id


def list_companies(user_id) -> list:
    """Every company for a user, most recently used first, with a session count."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM sessions s"
            "             WHERE s.company_id = c.company_id) AS session_count"
            " FROM companies c WHERE c.user_id = ?"
            " ORDER BY COALESCE(c.last_used_at, c.created_at) DESC",
            (str(user_id),)
        ).fetchall()

    return [{
        "company_id": r["company_id"],
        "slot": r["slot"],
        "company_name": r["company_name"],
        "company_profile": _from_text(r["company_profile"]),
        "session_count": r["session_count"],
        "free_session_slots": MAX_SESSIONS - r["session_count"],
        "created_at": r["created_at"],
        "last_used_at": r["last_used_at"],
    } for r in rows]


def get_company(company_id) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM companies WHERE company_id = ?", (company_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "company_id": row["company_id"],
        "user_id": row["user_id"],
        "slot": row["slot"],
        "company_name": row["company_name"],
        "company_profile": _from_text(row["company_profile"]),
        "onboarding_report": get_onboarding_report(row["company_id"]),
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
    }


def get_company_profile(company_id):
    company = get_company(company_id)
    return company["company_profile"] if company else None


def update_company_profile(company_id, company_profile) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE companies SET company_profile = ?, last_used_at = ?"
            " WHERE company_id = ?",
            (_to_text(company_profile), _now(), company_id))
        return cur.rowcount > 0


def touch_company(company_id) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE companies SET last_used_at = ? WHERE company_id = ?",
                     (_now(), company_id))


def delete_company(company_id) -> bool:
    """Frees the slot, and cascades to its sessions and answers."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM companies WHERE company_id = ?", (company_id,))
        return cur.rowcount > 0


def save_onboarding_report(company_id, report) -> bool:
    """Write the report once. Later calls are ignored, so it cannot be lost."""
    if not report:
        return False
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO onboarding_reports (company_id, report, created_at)"
            " VALUES (?,?,?) ON CONFLICT DO NOTHING",
            (company_id, _to_text(report), _now()))
        return cur.rowcount > 0


def get_onboarding_report(company_id):
    """The report as stored, or None when the company never had one."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT report FROM onboarding_reports WHERE company_id = ?",
            (company_id,)
        ).fetchone()
    return _from_text(row["report"]) if row else None


def company_slots_free(user_id) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM companies WHERE user_id = ?", (str(user_id),)
        ).fetchone()
    return MAX_COMPANIES - row["n"]


# ───────────────────────── sessions ─────────────────────────

def create_session(company_id, label="", scope_label="", hs_code="") -> int | None:
    """New session in the lowest free slot. None when the company has 10 already."""
    now = _now()
    with get_conn() as conn:
        _lock_parent(conn, "companies", "company_id", company_id)
        slot = _next_slot(conn, "sessions", "company_id", company_id, MAX_SESSIONS)
        if slot is None:
            return None

        row = conn.execute(
            "INSERT INTO sessions (company_id, slot, label, scope_label, hs_code,"
            " created_at, last_used_at) VALUES (?,?,?,?,?,?,?)"
            " RETURNING session_id",
            (company_id, slot, label or f"s{slot}",
             scope_label or None, hs_code or None, now, now)).fetchone()
        conn.execute("UPDATE companies SET last_used_at = ? WHERE company_id = ?",
                     (now, company_id))
        return row["session_id"]


def list_sessions(company_id) -> list:
    """Every session for a company, most recently used first, with answer counts."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM answers a"
            "             WHERE a.session_id = s.session_id) AS answer_count"
            " FROM sessions s WHERE s.company_id = ?"
            " ORDER BY COALESCE(s.last_used_at, s.created_at) DESC",
            (company_id,)
        ).fetchall()

    return [{
        "session_id": r["session_id"],
        "slot": r["slot"],
        "label": r["label"],
        "scope_label": r["scope_label"],
        "hs_code": r["hs_code"],
        "answer_count": r["answer_count"],
        "created_at": r["created_at"],
        "last_used_at": r["last_used_at"],
        "ended_at": r["ended_at"],
    } for r in rows]


def get_session(session_id) -> dict | None:
    """Everything needed to resume: summary, persona, scope, and the company."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT s.*, c.user_id, c.company_name, c.company_profile,"
            "       c.slot AS company_slot"
            " FROM sessions s JOIN companies c ON c.company_id = s.company_id"
            " WHERE s.session_id = ?",
            (session_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "session_id": row["session_id"],
        "company_id": row["company_id"],
        "company_slot": row["company_slot"],
        "company_name": row["company_name"],
        "company_profile": _from_text(row["company_profile"]),
        "user_id": row["user_id"],
        "slot": row["slot"],
        "label": row["label"],
        "session_summary": _from_text(row["session_summary"]),
        "persona": _from_text(row["persona"]),
        "scope_label": row["scope_label"],
        "hs_code": row["hs_code"],
        "engine_sid": row["engine_sid"],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "ended_at": row["ended_at"],
    }


def save_session(session_id, session_summary=None, persona=None,
                 scope_label=None, hs_code=None, label=None,
                 engine_sid=None, ended: bool = False) -> bool:
    """Update whichever fields are given. None means leave that column alone."""
    sets, values = [], []

    for column, value in (("session_summary", session_summary),
                          ("persona", persona),
                          ("scope_label", scope_label),
                          ("hs_code", hs_code),
                          ("label", label),
                          ("engine_sid", engine_sid)):
        if value is not None:
            sets.append(f"{column} = ?")
            values.append(_to_text(value))

    sets.append("last_used_at = ?")
    values.append(_now())

    if ended:
        sets.append("ended_at = ?")
        values.append(_now())

    values.append(session_id)

    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE sessions SET {', '.join(sets)} WHERE session_id = ?", values)
        updated = cur.rowcount > 0
        conn.execute(
            "UPDATE companies SET last_used_at = ? WHERE company_id ="
            " (SELECT company_id FROM sessions WHERE session_id = ?)",
            (_now(), session_id))
    return updated


def get_session_summary(session_id):
    session = get_session(session_id)
    return session["session_summary"] if session else None


def load_persona(session_id):
    session = get_session(session_id)
    return session["persona"] if session else None


def save_persona(session_id, persona) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE sessions SET persona = ?, last_used_at = ? WHERE session_id = ?",
            (_to_text(persona), _now(), session_id))
        return cur.rowcount > 0


def touch_session(session_id) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE sessions SET last_used_at = ? WHERE session_id = ?",
                     (_now(), session_id))


def delete_session(session_id) -> bool:
    """Frees the slot, and cascades to its answers."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        return cur.rowcount > 0


def session_slots_free(company_id) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE company_id = ?", (company_id,)
        ).fetchone()
    return MAX_SESSIONS - row["n"]


# ───────────────────────── answers ─────────────────────────

MAX_TRACE_ROWS = 300   # rows kept per chart so the DB stays small


def _slim_trace(obj):
    """Copy of the trace with long row lists cut to MAX_TRACE_ROWS."""
    if isinstance(obj, dict):
        return {k: (v[:MAX_TRACE_ROWS] if k == "rows" and isinstance(v, list) else _slim_trace(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_slim_trace(v) for v in obj]
    return obj


def save_answer(session_id, question, answer, keep: int = MAX_ANSWERS, trace=None) -> bool:
    """Append one answered turn (with its trace) and trim to the newest `keep`. Empty answers skip."""
    answer = (answer or "").strip()
    if not answer:
        return False
    trace_json = json.dumps(_slim_trace(trace), default=str) if trace else None

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO answers (session_id, question, answer, trace, created_at)"
            " VALUES (?,?,?,?,?)",
            (session_id, (question or "").strip(), answer, trace_json, _now()))
        conn.execute(
            "DELETE FROM answers WHERE session_id = ? AND answer_id NOT IN"
            " (SELECT answer_id FROM answers WHERE session_id = ?"
            "  ORDER BY answer_id DESC LIMIT ?)",
            (session_id, session_id, keep))
        conn.execute("UPDATE sessions SET last_used_at = ? WHERE session_id = ?",
                     (_now(), session_id))
    return True


def get_answers(session_id, limit: int = MAX_ANSWERS) -> list:
    """Oldest first, so the chat window rebuilds in order."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT question, answer, trace, created_at FROM answers"
            " WHERE session_id = ? ORDER BY answer_id DESC LIMIT ?",
            (session_id, limit)
        ).fetchall()

    return [{"question": r["question"] or "",
             "answer": r["answer"],
             "trace": json.loads(r["trace"]) if r["trace"] else None,
             "created_at": r["created_at"]} for r in reversed(rows)]


def clear_answers(session_id) -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM answers WHERE session_id = ?", (session_id,))
        return cur.rowcount


# ───────────────────────── run directly ─────────────────────────
#
#   python -m database.info_database

if __name__ == "__main__":
    print("===== DB TEST START =====\n")

    init_db()

    user = ensure_user("abhishek")
    print("User:", user)

    print("All users:", list_users())

    print("Companies:", list_companies("abhishek"))
    # print("Sessions:", list_sessions("14"))

    print("===== DB TEST END =====")

    # init_db()
    # user = "TEST_MULTI"
    # delete_user(user)

    # print("1. create companies")
    # ids = []
    # for n in range(1, 7):
    #     cid = create_company(user, f"Company {n}", f"profile for company {n}")
    #     print(f"   company {n}: {'slot taken, id ' + str(cid) if cid else 'REFUSED (cap)'}")
    #     if cid:
    #         ids.append(cid)
    # print(f"   free company slots: {company_slots_free(user)}")

    # print("\n2. duplicate name is refused")
    # print("   ", create_company(user, "Company 1", "x") or "REFUSED (duplicate)")

    # print("\n3. create sessions under company 1")
    # sids = []
    # for n in range(1, 12):
    #     sid = create_session(ids[0], scope_label=f"HS 61091{n:01d}", hs_code=f"61091{n:01d}")
    #     if sid:
    #         sids.append(sid)
    #     else:
    #         print(f"   session {n}: REFUSED (cap)")
    # print(f"   created {len(sids)} sessions, free: {session_slots_free(ids[0])}")

    # print("\n4. answers, capped at 20")
    # for n in range(1, 26):
    #     save_answer(sids[0], f"question {n}", f"answer number {n}")
    # kept = get_answers(sids[0])
    # print(f"   stored 25, kept {len(kept)}")
    # print(f"   oldest kept: {kept[0]['question']}  newest: {kept[-1]['question']}")

    # print("\n5. save and reload a session")
    # save_persona(sids[0], {"tone": "direct", "focus": "exports"})
    # save_session(sids[0], session_summary="Explored buyers for cotton knitwear.")
    # row = get_session(sids[0])
    # print(f"   company : {row['company_name']} (slot {row['company_slot']})")
    # print(f"   session : slot {row['slot']}, scope {row['scope_label']}")
    # print(f"   persona : {row['persona']}")
    # print(f"   summary : {row['session_summary']}")

    # print("\n6. listings")
    # for c in list_companies(user)[:3]:
    #     print(f"   c{c['slot']} {c['company_name']}  "
    #           f"{c['session_count']} sessions, {c['free_session_slots']} free")
    # for s in list_sessions(ids[0])[:3]:
    #     print(f"   s{s['slot']} {s['scope_label']}  {s['answer_count']} answers")

    # print("\n7. delete a session frees its slot")
    # delete_session(sids[2])
    # print(f"   free session slots now: {session_slots_free(ids[0])}")
    # new_sid = create_session(ids[0], scope_label="reused slot")
    # print(f"   new session took slot: {get_session(new_sid)['slot']}")

    # print("\n8. cascade")
    # before = len(get_answers(sids[0]))
    # delete_company(ids[0])
    # print(f"   answers before deleting the company: {before}")
    # print(f"   session gone: {get_session(sids[0]) is None}")
    # print(f"   free company slots: {company_slots_free(user)}")

    # delete_user(user)
    # print("\n===== DB TEST END =====")