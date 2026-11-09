# exira_core/kv.py
"""
Small key-value store for the API layer: workspace state and job records.

    REDIS_URL set   -> Redis   (shared by every API process / server)
    REDIS_URL unset -> memory  (single process; fine for local runs)

Values are bytes. Keys expire after a TTL so abandoned workspaces and
finished jobs clean themselves up.
"""

import os
import threading
import time
from contextlib import contextmanager
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

PREFIX = os.getenv("EXIRA_KV_PREFIX", "exira:")
LOCK_SECONDS = int(os.getenv("WORKSPACE_LOCK_SECONDS", "600"))   # longest one action may run
LOCK_WAIT = int(os.getenv("WORKSPACE_LOCK_WAIT", "5"))           # wait before reporting "busy"


class Busy(Exception):
    """Another action is still running on this workspace."""


class _Memory:
    backend = "memory"

    def __init__(self):
        self._data = {}                      # key -> (expires_at, bytes)
        self._locks = {}
        self._guard = threading.Lock()

    def get(self, key: str) -> Optional[bytes]:
        with self._guard:
            item = self._data.get(key)
            if not item:
                return None
            if item[0] < time.time():
                self._data.pop(key, None)
                return None
            return item[1]

    def set(self, key: str, value: bytes, ttl: int) -> None:
        with self._guard:
            self._data[key] = (time.time() + ttl, value)

    def delete(self, key: str) -> None:
        with self._guard:
            self._data.pop(key, None)

    @contextmanager
    def lock(self, key: str):
        with self._guard:
            lk = self._locks.setdefault(key, threading.Lock())
        if not lk.acquire(timeout=LOCK_WAIT):
            raise Busy(key)
        try:
            yield
        finally:
            lk.release()


class _Redis:
    backend = "redis"

    def __init__(self, url: str):
        import redis
        self._r = redis.Redis.from_url(url)          # bytes in, bytes out
        self._r.ping()

    def get(self, key: str) -> Optional[bytes]:
        return self._r.get(key)

    def set(self, key: str, value: bytes, ttl: int) -> None:
        self._r.set(key, value, ex=ttl)

    def delete(self, key: str) -> None:
        self._r.delete(key)

    @contextmanager
    def lock(self, key: str):
        from redis.exceptions import LockError
        lk = self._r.lock(f"{key}:lock", timeout=LOCK_SECONDS, blocking_timeout=LOCK_WAIT)
        if not lk.acquire():
            raise Busy(key)
        try:
            yield
        finally:
            try:
                lk.release()
            except LockError:
                print(f"[kv] lock {key} expired before release")


def _build():
    url = os.getenv("REDIS_URL", "").strip()
    if url:
        return _Redis(url)
    print("[kv] REDIS_URL not set: API state kept in memory (single process only)")
    return _Memory()


KV = _build()


def key(*parts) -> str:
    return PREFIX + ":".join(str(p) for p in parts)
