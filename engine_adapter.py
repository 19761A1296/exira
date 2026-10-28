# engine_adapter.py
from text2sql import text2sql_tool as engine
from text2sql.session_store import STORE, SessionBusy


HS2_DOMAIN_MAP = engine.HS2_DOMAIN_MAP


class Engine:
    """Direct in-process calls. To go HTTP later, replace the method bodies.

    Engine session state lives in the shared store (text2sql/session_store.py),
    so reads here go to the store and writes take the session lock.
    """

    # ── writes ───────────────────────────────────────────────

    def _update(self, sid: str, fn) -> bool:
        """Lock, load, apply fn(state), save. False if the session is gone."""
        try:
            with STORE.lock(sid):
                state = STORE.load(sid)
                if not state:
                    return False
                fn(state)
                STORE.save(sid, state)
                return True
        except SessionBusy:
            print(f"[engine_adapter] session {sid} busy, update skipped")
            return False

    def set_hs(self, sid: str, code: str) -> None:
        """Force the engine's focus onto an HS code at any point in the session."""
        def apply(session):
            focus = session["active_focus"]
            focus["hs_code"] = code
            focus["product_name"] = None
            focus["selected_hs_codes"] = []
            focus["pending_domain_disambiguation"] = None
            focus["pending_query_text"] = None
            focus["pending_product_terms"] = []
            focus["pending_hs_code"] = None
            session["state"] = engine.STATE_IDLE
        self._update(sid, apply)

    def inject_memory(self, sid: str, memory: dict) -> None:
        """Push router-side memory into the engine session before every call."""
        def apply(session):
            session["active_focus"]["user_memory"] = memory
        self._update(sid, apply)

    # ── engine calls ─────────────────────────────────────────

    def start(self) -> str:
        return engine.start_session()["session_id"]

    def send(self, sid: str, message: str, source: str | None = None) -> dict:
        return self._unwrap(engine.chat(
            engine.ChatRequest(session_id=sid, message=message, source=source)
        ))

    def confirm(self, sid: str, decision: str) -> dict:
        return self._unwrap(engine.confirm(
            engine.ConfirmRequest(session_id=sid, decision=decision)
        ))

    # ── reads ────────────────────────────────────────────────

    def exists(self, sid: str | None) -> bool:
        """True when the engine still has this session (not expired / never made)."""
        return bool(sid) and STORE.exists(sid)

    def scope_pending(self, sid: str) -> bool:
        """True when the engine is waiting for an HS domain selection."""
        s = STORE.load(sid)
        return bool(s and s["active_focus"].get("pending_domain_disambiguation"))

    def clarification_pending(self, sid: str) -> bool:
        """True when the Trade Agent asked a question — not the HS domain pick."""
        s = STORE.load(sid)
        if not s:
            return False
        if s["active_focus"].get("pending_domain_disambiguation"):
            return False                  # that is the HS pick, handled elsewhere
        return s.get("state") == "AWAITING_CLARIFICATION"

    def current_scope(self, sid: str) -> dict:
        """What the engine is actually focused on right now."""
        s = STORE.load(sid)
        if not s:
            return {"label": "", "hs_code": None, "product": None, "codes": []}

        focus = s["active_focus"]
        hs_code = focus.get("hs_code")
        product = focus.get("product_name")
        codes = list(focus.get("selected_hs_codes") or [])

        if product:
            label = f"{product} (HS {', '.join(codes)})" if codes else product
        elif hs_code:
            label = f"HS {hs_code}"
        else:
            label = ""

        return {"label": label, "hs_code": hs_code, "product": product, "codes": codes}

    @staticmethod
    def _unwrap(resp):
        # chat()/confirm() return JSONResponse on error paths
        if hasattr(resp, "body"):
            import json
            return json.loads(resp.body)
        return resp


ENGINE = Engine()
