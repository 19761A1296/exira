# text2sql/session_store.py
"""
Where the trade engine keeps per-session state.

Before: a module-level dict (SESSIONS) inside one Python process.
Now:    a shared store, so any worker / server can serve any session,
        state survives restarts, idle sessions expire, and two requests
        on the same session can't overwrite each other.

Backend is chosen once at import:
    REDIS_URL set   -> Redis   (production, multi-process / multi-server)
    REDIS_URL unset -> memory  (local dev only; same behaviour as before,
                                plus TTL and locking)

Usage:
    with STORE.lock(sid):
        state = STORE.load(sid)
        ...mutate state...
        STORE.save(sid, state)

Env:
    REDIS_URL                     e.g. redis://localhost:6379/0
    ENGINE_SESSION_TTL_SECONDS    idle expiry, default 7 days
    ENGINE_SESSION_LOCK_SECONDS   max time one request may hold a session, default 300
    ENGINE_SESSION_LOCK_WAIT      how long a second request waits for it, default 120
    ENGINE_SESSION_PREFIX         key prefix, default "exira:engine:"
"""

import datetime
import json
import os
import threading
import time
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Dict, Optional

from dotenv import load_dotenv

load_dotenv()

TTL_SECONDS = int(os.getenv("ENGINE_SESSION_TTL_SECONDS", str(7 * 24 * 3600)))
LOCK_SECONDS = int(os.getenv("ENGINE_SESSION_LOCK_SECONDS", "300"))
LOCK_WAIT = int(os.getenv("ENGINE_SESSION_LOCK_WAIT", "120"))
PREFIX = os.getenv("ENGINE_SESSION_PREFIX", "exira:engine:")


class SessionBusy(Exception):
    """Another request is still working on this session."""


def _default(obj: Any):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    return str(obj)


def _dumps(state: Dict[str, Any]) -> str:
    return json.dumps(state, default=_default, ensure_ascii=False)


def _loads(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    return json.loads(raw) if raw else None


# ───────────────────────── memory (dev) ─────────────────────────

class _MemoryStore:
    """Single-process fallback. Stores JSON text, like Redis, so
    serialisation problems show up in dev rather than in production."""

    backend = "memory"

    def __init__(self):
        self._data: Dict[str, tuple] = {}          # sid -> (expires_at, json)
        self._locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _sweep(self):
        now = time.time()
        for sid in [k for k, (exp, _) in self._data.items() if exp < now]:
            self._data.pop(sid, None)
            self._locks.pop(sid, None)

    def load(self, sid: str) -> Optional[Dict[str, Any]]:
        with self._guard:
            self._sweep()
            item = self._data.get(sid)
        return _loads(item[1]) if item else None

    def save(self, sid: str, state: Dict[str, Any]) -> None:
        raw = _dumps(state)
        with self._guard:
            self._data[sid] = (time.time() + TTL_SECONDS, raw)

    def exists(self, sid: str) -> bool:
        with self._guard:
            self._sweep()
            return sid in self._data

    def delete(self, sid: str) -> None:
        with self._guard:
            self._data.pop(sid, None)
            self._locks.pop(sid, None)

    @contextmanager
    def lock(self, sid: str):
        with self._guard:
            lk = self._locks.setdefault(sid, threading.Lock())
        if not lk.acquire(timeout=LOCK_WAIT):
            raise SessionBusy(sid)
        try:
            yield
        finally:
            lk.release()


# ───────────────────────── redis (prod) ─────────────────────────

class _RedisStore:
    backend = "redis"

    def __init__(self, url: str):
        import redis                                    # only needed in prod
        self._r = redis.Redis.from_url(url, decode_responses=True)
        self._r.ping()                                  # fail fast on bad config

    def _key(self, sid: str) -> str:
        return f"{PREFIX}{sid}"

    def load(self, sid: str) -> Optional[Dict[str, Any]]:
        return _loads(self._r.get(self._key(sid)))

    def save(self, sid: str, state: Dict[str, Any]) -> None:
        self._r.set(self._key(sid), _dumps(state), ex=TTL_SECONDS)

    def exists(self, sid: str) -> bool:
        return bool(self._r.exists(self._key(sid)))

    def delete(self, sid: str) -> None:
        self._r.delete(self._key(sid))

    @contextmanager
    def lock(self, sid: str):
        from redis.exceptions import LockError
        lk = self._r.lock(f"{PREFIX}lock:{sid}",
                          timeout=LOCK_SECONDS, blocking_timeout=LOCK_WAIT)
        if not lk.acquire():
            raise SessionBusy(sid)
        try:
            yield
        finally:
            try:
                lk.release()
            except LockError:
                # held longer than LOCK_SECONDS and already expired
                print(f"[session_store] lock for {sid} expired before release")


def _build():
    url = os.getenv("REDIS_URL", "").strip()
    if url:
        return _RedisStore(url)
    print("[session_store] REDIS_URL not set: using in-memory store "
          "(single process only, lost on restart)")
    return _MemoryStore()


STORE = _build()
