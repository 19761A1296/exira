# engine_adapter.py
from text2sql import text2sql_tool as engine


HS2_DOMAIN_MAP = engine.HS2_DOMAIN_MAP
class Engine:
    """Direct in-process calls. To go HTTP later, replace the three bodies."""



    def set_hs(self, sid: str, code: str) -> None:
        """Force the engine's focus onto an HS code at any point in the session."""
        session = engine.SESSIONS.get(sid)
        if not session:
            return
        focus = session["active_focus"]
        focus["hs_code"] = code
        focus["product_name"] = None
        focus["selected_hs_codes"] = []
        focus["pending_domain_disambiguation"] = None
        focus["pending_query_text"] = None
        focus["pending_product_terms"] = []
        focus["pending_hs_code"] = None
        session["state"] = engine.STATE_IDLE

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

    def inject_memory(self, sid: str, memory: dict) -> None:
        """Push router-side memory into the engine session before every call."""
        if sid in engine.SESSIONS:
            engine.SESSIONS[sid]["active_focus"]["user_memory"] = memory

    def scope_pending(self, sid: str) -> bool:
        """True when the engine is waiting for an HS domain selection."""
        s = engine.SESSIONS.get(sid)
        return bool(s and s["active_focus"].get("pending_domain_disambiguation"))


    def current_scope(self, sid: str) -> dict:
        """What the engine is actually focused on right now."""
        s = engine.SESSIONS.get(sid)
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