# exira_core/state.py
"""
Workspace state — what st.session_state used to hold, now stored server-side.

A *workspace* is one signed-in user's working area (one per browser tab /
client). Its state is a plain object saved in the key-value store between
requests, so any UI (Streamlit, React, PHP, mobile) gets the same behaviour.

The app logic was written against `S.something`. That still works: `S` is a
proxy to the workspace being handled by the current request/job.

    with workspace(wid):       # lock, load, run, save
        S.stage = "chat"
"""

import copy
import pickle
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Optional

from exira_core.kv import KV, key

WORKSPACE_TTL = 7 * 24 * 3600          # idle workspaces expire after 7 days


# ───────────────────────── the state object ─────────────────────────

class State(dict):
    """dict with attribute access, like st.session_state."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        self[name] = value

    def __delattr__(self, name):
        self.pop(name, None)


DEFAULTS = {
    "stage": "login",
    "userid": "",
    "user_product_info": "",
    "old_session": "",
    "report": None,
    "persona": None,
    "persona_turns": [],
    "append_queries": [],
    "sid": None,
    "turns": [],
    "options": [],
    "selecting": False,
    "pending_confirm": None,
    "scope_label": "",
    "scope_history": [],
    "hs_code": None,
    "candidates": [],
    "show_hs_panel": False,
    "session_notes": "",
    "last_resolution": None,
    "last_route": "",
    "pending_input": None,
    "flow_traces": [],
    "flow_run": None,
    "pending_clarify": None,
    "last_flow": None,
    "flow_text": "",
    "flow_sent": "",
    "last_trade_trace": None,
    "last_trade_execution": None,
    "thinking_results": [],
    "last_web": None,
    "company_id": None,
    "session_id": None,
    "company_slot": None,
    "session_slot": None,
    "company_name": "",
    "companies": [],
    "sessions": [],
    "answers": [],
    # onboarding wizard
    "onb_step": "input",
    "onb_text": "",
    "onb_urls": "",
    "onb_attempt": 0,
    "onb_scrape_attempt": 0,
    "onb_matches": [],
    "onb_company": "",
    "onb_scraped_info": "",
    "onb_scraped_name": "",
    "onb_pending": {},
    "onb_after_urls": "",
    "onb_search_name": "",
    "onb_country": "",
    "onb_hs2": "",
    "onb_next_after_pick": "",
    "onb_failure": None,          # {"reason", "exhausted"} when a retry is needed
    # API only
    "notices": [],                # messages for the UI from the last action
}


def new_state() -> State:
    return State(copy.deepcopy(DEFAULTS))


# ───────────────────────── S: the current workspace ─────────────────────────

_current: ContextVar[Optional[State]] = ContextVar("exira_state", default=None)


class _Proxy:
    """Forwards to the state of the workspace being handled right now."""

    def _state(self) -> State:
        state = _current.get()
        if state is None:
            raise RuntimeError("No workspace is active (use `with workspace(wid):`)")
        return state

    def __getattr__(self, name):
        return getattr(self._state(), name)

    def __setattr__(self, name, value):
        setattr(self._state(), name, value)

    def __contains__(self, name):
        return name in self._state()

    def get(self, name, default=None):
        return self._state().get(name, default)

    def setdefault(self, name, default=None):
        return self._state().setdefault(name, default)


S = _Proxy()


@contextmanager
def use_state(state: State):
    token = _current.set(state)
    try:
        yield state
    finally:
        _current.reset(token)


# ───────────────────────── messages back to the UI ─────────────────────────

class UserError(Exception):
    """A problem the user should see (shown as an error, nothing crashed)."""


def notify(level: str, message: str) -> None:
    """Replaces st.info / st.warning / st.error inside the logic."""
    S.setdefault("notices", []).append({"level": level, "message": str(message)})
    print(f"[{level}] {message}")


def warn(message: str) -> None:
    notify("warning", message)


def error(message: str) -> None:
    notify("error", message)


# progress lines for long actions ("Running trade analysis…")
_progress: ContextVar[Optional[Callable[[str], None]]] = ContextVar("exira_progress", default=None)


def progress(message: str) -> None:
    fn = _progress.get()
    if fn:
        try:
            fn(message)
        except Exception:
            pass


@contextmanager
def progress_to(fn: Callable[[str], None]):
    token = _progress.set(fn)
    try:
        yield
    finally:
        _progress.reset(token)


@contextmanager
def spinner(message: str):
    """Replaces `with st.spinner(...)`: reports the step as progress."""
    progress(message)
    yield


# ───────────────────────── load / save workspaces ─────────────────────────

class WorkspaceNotFound(Exception):
    pass


def _ws_key(wid: str) -> str:
    return key("ws", wid)


def _dump(state: State) -> bytes:
    flow = state.get("flow_run")
    handlers = getattr(flow, "handlers", None) if flow is not None else None
    if flow is not None:
        flow.handlers = None                  # functions are re-attached on load
    try:
        return pickle.dumps(dict(state), protocol=pickle.HIGHEST_PROTOCOL)
    finally:
        if flow is not None:
            flow.handlers = handlers


def _load(raw: bytes) -> State:
    state = State(pickle.loads(raw))
    for name, value in DEFAULTS.items():      # fields added after it was saved
        state.setdefault(name, copy.deepcopy(value))
    flow = state.get("flow_run")
    if flow is not None:
        from exira_core.chat import FLOW_HANDLERS
        flow.handlers = FLOW_HANDLERS
    return state


def create_workspace() -> str:
    wid = uuid.uuid4().hex
    save_state(wid, new_state())
    return wid


def load_state(wid: str) -> State:
    raw = KV.get(_ws_key(wid))
    if raw is None:
        raise WorkspaceNotFound(wid)
    return _load(raw)


def save_state(wid: str, state: State) -> None:
    state["_saved_at"] = time.time()
    KV.set(_ws_key(wid), _dump(state), WORKSPACE_TTL)


def delete_workspace(wid: str) -> None:
    KV.delete(_ws_key(wid))


@contextmanager
def workspace(wid: str, clear_notices: bool = True):
    """Lock the workspace, load it, make it `S`, and save it afterwards."""
    with KV.lock(key("wslock", wid)):
        state = load_state(wid)
        if clear_notices:
            state["notices"] = []
        with use_state(state):
            try:
                yield state
            finally:
                save_state(wid, state)


def read_only(wid: str) -> State:
    """Current state without locking (for GET requests)."""
    return load_state(wid)
