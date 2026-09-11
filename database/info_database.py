"""
db.py — SQLite storage layer for the `exims_data` table.

Table: exims_data
    userid             TEXT  (primary key)
    user_product_info  TEXT  (the summary / product info)
    user_session_info  TEXT  (kept empty for now)
"""

import json
import sqlite3
from pathlib import Path

DB_NAME = Path(__file__).parent / "exims.db"


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


def get_conn():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the table if it doesn't already exist. Call this once at startup."""
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS exims_data (
                userid            TEXT PRIMARY KEY,
                user_product_info TEXT,
                user_session_info TEXT
            )
            """
        )


def user_exists(userid) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM exims_data WHERE userid = ?", (str(userid),)
        ).fetchone()
    return row is not None


def insert_user(userid, user_product_info, user_session_info) -> bool:
    """
    Insert a new user. Returns False if the userid already exists.
    """
    try:
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO exims_data (userid, user_product_info, user_session_info)
                VALUES (?, ?, ?)
                """,
                (
                    str(userid),
                    _to_text(user_product_info),
                    _to_text(user_session_info),
                ),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def get_product_info(userid):
    """Return user_product_info for a user, or None if the user isn't there."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_product_info FROM exims_data WHERE userid = ?",
            (str(userid),),
        ).fetchone()
    return _from_text(row["user_product_info"]) if row else None



def get_past_sesion_info(userid):
    """Return user_past_session_info for a user, or None if the user isn't there."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_session_info FROM exims_data WHERE userid = ?",
            (str(userid),),
        ).fetchone()
    return _from_text(row["user_session_info"]) if row else None


def get_user(userid):
    """Return the full row as a dict, or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM exims_data WHERE userid = ?", (str(userid),)
        ).fetchone()
    if not row:
        return None
    return {
        "userid": row["userid"],
        "user_product_info": _from_text(row["user_product_info"]),
        "user_session_info": _from_text(row["user_session_info"]),
    }


def update_product_info(userid, user_product_info) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE exims_data SET user_product_info = ? WHERE userid = ?",
            (_to_text(user_product_info), str(userid)),
        )
    return cur.rowcount > 0


def update_session_info(userid, user_session_info) -> bool:
    """Fill in user_session_info later, once you have it."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE exims_data SET user_session_info = ? WHERE userid = ?",
            (_to_text(user_session_info), str(userid)),
        )
    return cur.rowcount > 0


def delete_user(userid) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM exims_data WHERE userid = ?", (str(userid),))
    return cur.rowcount > 0


def list_users():
    with get_conn() as conn:
        rows = conn.execute("SELECT userid FROM exims_data").fetchall()
    return [r["userid"] for r in rows]


if __name__ == "__main__":
    print("===== DB TEST START =====")

    init_db()
    print("Users:", list_users())
    # userid = "TEST_001"

    # product_info = """
    # Company sells cotton men's T-shirts and cotton pullovers.
    # Main export markets are USA, Spain and Hong Kong.
    # """

    # session_info = """
    # User asked about future product opportunities and market expansion.
    # """

    # # Remove old test data
    # delete_user(userid)

    # # Insert
    # print("Insert:", insert_user(userid, product_info, session_info))

    # # Check user
    # print("Exists:", user_exists(userid))

    # # Read product info
    # print("Product Info:")
    # print(get_product_info(userid))

    # # Read session info
    # print("Session Info:")
    # print(get_past_sesion_info(userid))

    # # Get complete user
    # print("Full User:")
    # print(get_user(userid))

    # # Update product info
    # new_product_info = """
    # Company may expand into related knitwear products
    # and new international markets.
    # """

    # print("Update Product:", update_product_info(userid, new_product_info))

    # print("Updated Product:")
    # print(get_product_info(userid))

    # # Update session info
    # new_session_info = """
    # User previously asked about company future scope.
    # """

    # print("Update Session:", update_session_info(userid, new_session_info))

    # print("Updated Session:")
    # print(get_past_sesion_info(userid))

    # # List users
    # print("Users:", list_users())

    # # Delete
    # print("Delete:", delete_user(userid))

    # # Verify deletion
    # print("Exists after delete:", user_exists(userid))

    # print("===== DB TEST END =====")
    # print("===== DB TEST START =====")

    # # 1. Initialize database
    # init_db()
    # print("1. Database initialized")

    # # Test user
    # userid = "TEST_001"
    # product_info = {
    #     "product": "Cotton T-shirt",
    #     "category": "Apparel",
    #     "country": "India"
    # }
    # session_info = {
    #     "last_query": "Show me buyers for cotton t-shirts"
    # }

    # # 2. Clean up old test user if it exists
    # delete_user(userid)
    # print("2. Old test user removed (if existed)")

    # # 3. Check user does not exist
    # print("3. User exists before insert:", user_exists(userid))

    # # 4. Insert user
    # result = insert_user(userid, product_info, session_info)
    # print("4. Insert user:", result)

    # # 5. Check user exists
    # print("5. User exists after insert:", user_exists(userid))

    # # 6. Get product info
    # result = get_product_info(userid)
    # print("6. Product info:", result)

    # # 7. Get session info
    # result = get_past_sesion_info(userid)
    # print("7. Session info:", result)

    # # 8. Get complete user
    # result = get_user(userid)
    # print("8. Full user:")
    # print(result)

    # # 9. Update product info
    # new_product_info = {
    #     "product": "Cotton T-shirt",
    #     "category": "Apparel",
    #     "country": "India",
    #     "future_scope": "Expand into USA and Europe"
    # }

    # result = update_product_info(userid, new_product_info)
    # print("9. Product info updated:", result)

    # # 10. Verify update
    # result = get_product_info(userid)
    # print("10. Updated product info:", result)

    # # 11. Update session info
    # new_session_info = {
    #     "last_query": "Find similar buyers",
    #     "status": "completed"
    # }

    # result = update_session_info(userid, new_session_info)
    # print("11. Session info updated:", result)

    # # 12. Verify session update
    # result = get_past_sesion_info(userid)
    # print("12. Updated session info:", result)

    # # 13. List users
    # result = list_users()
    # print("13. Users in DB:", result)

    # # 14. Delete user
    # result = delete_user(userid)
    # print("14. User deleted:", result)

    # # 15. Verify deletion
    # print("15. User exists after delete:", user_exists(userid))

    # print("===== DB TEST END =====")