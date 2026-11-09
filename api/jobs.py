# api/jobs.py
"""
Background jobs. Every action that changes a workspace runs as a job:

    POST an action      -> {"job_id", "status": "queued"}      (202)
    GET  /jobs/{job_id} -> status, progress lines, and when done the new view

Fast actions finish in milliseconds; a chat turn can take a minute or two.
Clients can also pass ?wait=N to block up to N seconds for the result.

Job records live in the key-value store (Redis when REDIS_URL is set), so any
API process can answer GET /jobs/{id}. The work itself runs in this process's
thread pool. One workspace runs one action at a time (workspace lock).
"""

import os
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

import pickle

from exira_core.kv import KV, Busy, key
from exira_core.state import UserError, WorkspaceNotFound, progress_to, workspace

JOB_TTL = 24 * 3600
EXECUTOR = ThreadPoolExecutor(max_workers=int(os.getenv("API_WORKERS", "8")),
                              thread_name_prefix="exira-job")

FINISHED = ("done", "failed", "error")


def _save(job: dict) -> None:
    KV.set(key("job", job["job_id"]), pickle.dumps(job), JOB_TTL)


def get(job_id: str):
    raw = KV.get(key("job", job_id))
    return pickle.loads(raw) if raw else None


def submit(workspace_id: str, action: str, fn, *args, debug: bool = False, **kwargs) -> dict:
    job = {
        "job_id": uuid.uuid4().hex,
        "workspace_id": workspace_id,
        "action": action,
        "status": "queued",
        "progress": [],
        "result": None,
        "error": None,
        "view": None,
        "created_at": time.time(),
        "finished_at": None,
    }
    _save(job)
    EXECUTOR.submit(_run, job, fn, args, kwargs, debug)
    return job


def _run(job: dict, fn, args, kwargs, debug: bool) -> None:
    from exira_core.workspace import view           # avoid an import cycle

    job["status"] = "running"
    job["started_at"] = time.time()
    _save(job)

    def on_progress(message: str):
        job["progress"].append({"at": round(time.time() - job["started_at"], 1), "message": message})
        _save(job)

    try:
        with workspace(job["workspace_id"]):
            with progress_to(on_progress):
                try:
                    job["result"] = fn(*args, **kwargs)
                    job["status"] = "done"
                except UserError as exc:
                    job["status"] = "failed"
                    job["error"] = str(exc)
            job["view"] = view(debug=debug)
    except Busy:
        job["status"] = "failed"
        job["error"] = "This workspace is still working on a previous request."
    except WorkspaceNotFound:
        job["status"] = "failed"
        job["error"] = "Workspace not found or expired. Sign in again."
    except Exception as exc:
        traceback.print_exc()
        job["status"] = "error"
        job["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        job["finished_at"] = time.time()
        _save(job)


def wait(job_id: str, seconds: float) -> dict:
    """Poll until the job finishes or `seconds` pass."""
    deadline = time.time() + max(0.0, seconds)
    delay = 0.05
    while True:
        job = get(job_id)
        if job is None or job["status"] in FINISHED or time.time() >= deadline:
            return job
        time.sleep(delay)
        delay = min(delay * 2, 0.5)
