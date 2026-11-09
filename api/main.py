# api/main.py
"""
EXIRA HTTP API — the whole app, usable from any UI (Streamlit, React, PHP…).

Run:
    uvicorn api.main:app --port 8000
Docs (try every endpoint in the browser):
    http://localhost:8000/docs

How a UI talks to it
    1. POST /workspaces {"user_id": "..."}      -> workspace_id + view
    2. Render the view. Its "stage" says which screen to show:
         onboarding | company_picker | session_picker | scope | chat | done
    3. A button / input = one action endpoint. Actions run as jobs:
         POST ...            -> 202 {"job_id", "status": "queued"}
         GET  /jobs/{job_id} -> {"status", "progress": [...], "view": {...}}
       Poll until status is done / failed / error, then render job["view"].
       Add ?wait=30 to an action to block up to 30 s instead of polling.
    4. GET /workspaces/{id} returns the current view at any time.
Add ?debug=true to get full traces (SQL, prompts, flow) in views.

No authentication yet: the API trusts the user_id it is given.
"""

import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pathlib import Path
from pydantic import BaseModel, Field

from database import info_database as db
from exira_core import onboarding, workspace as ws
from exira_core.kv import KV
from exira_core.state import (
    UserError, WorkspaceNotFound, create_workspace, delete_workspace, read_only, use_state, workspace,
)
from api import jobs

@asynccontextmanager
async def _lifespan(app):
    db.init_db()                       # create / migrate tables once at start
    yield


app = FastAPI(
    lifespan=_lifespan,
    title="EXIRA API",
    version="1.0",
    description="Trade-intelligence assistant. Start with POST /workspaces.",
)

_origins = [o.strip() for o in os.getenv("API_CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["*"], allow_headers=["*"])


# ───────────────────────── request bodies ─────────────────────────

class LoginBody(BaseModel):
    user_id: str = Field(..., examples=["baxter_user_001"])


class NavigateBody(BaseModel):
    to: str = Field(..., description="companies | sessions")


class ScopeBody(BaseModel):
    choice: str = Field(..., description="HS code (e.g. 0901) or product name", examples=["0901"])


class MessageBody(BaseModel):
    text: str
    source: str = Field("typed", description="typed | suggestion | clarification")


class OnboardingBody(BaseModel):
    text: Optional[str] = None
    urls: Optional[str] = None
    country: Optional[str] = None
    hs2: Optional[str] = None
    index: Optional[int] = None
    take_name: Optional[bool] = None
    take_info: Optional[bool] = None
    extra: Optional[str] = None


# fields each onboarding action accepts
_ONB_ARGS = {
    "input": ("text", "urls"),
    "narrow": ("country", "hs2"),
    "pick": ("index",),
    "give_urls": ("urls",),
    "retry_scrape": ("urls",),
    "review_continue": ("take_name", "take_info"),
    "retry": ("extra",),
}


# ───────────────────────── helpers ─────────────────────────

def _exists(wid: str):
    try:
        read_only(wid)
    except WorkspaceNotFound:
        raise HTTPException(404, "Workspace not found or expired. Sign in again.")


def _job_response(job: dict) -> JSONResponse:
    code = 200 if job["status"] in jobs.FINISHED else 202
    return JSONResponse(job, status_code=code)


def _action(wid: str, name: str, fn, *args, wait: float = 0, debug: bool = False, **kwargs):
    _exists(wid)
    job = jobs.submit(wid, name, fn, *args, debug=debug, **kwargs)
    if wait > 0:
        job = jobs.wait(job["job_id"], min(wait, 300))
    return _job_response(job)


Wait = Query(0, ge=0, le=300, description="Seconds to wait for the result before returning")
Debug = Query(False, description="Include full traces in the returned view")


# ───────────────────────── service ─────────────────────────

_UI = Path(__file__).parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/ui")


@app.get("/ui", include_in_schema=False)
def test_ui():
    """Browser test console that drives every endpoint like the Streamlit app."""
    return FileResponse(_UI)


@app.get("/health", tags=["service"])
def health():
    return {"ok": True, "state_store": KV.backend, "database": "postgres" if db.IS_PG else "sqlite"}


@app.get("/jobs/{job_id}", tags=["service"])
def get_job(job_id: str, wait: float = Wait):
    job = jobs.wait(job_id, wait) if wait else jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found or expired.")
    return _job_response(job)


# ───────────────────────── workspaces (sign in / out) ─────────────────────────

@app.post("/workspaces", tags=["workspace"], summary="Sign in: create a workspace for a user")
def sign_in(body: LoginBody, debug: bool = Debug):
    wid = create_workspace()
    try:
        with workspace(wid):
            ws.login(body.user_id)
            view = ws.view(debug=debug)
    except UserError as exc:
        delete_workspace(wid)
        raise HTTPException(400, str(exc))
    return {"workspace_id": wid, "view": view}


@app.get("/workspaces/{wid}", tags=["workspace"], summary="Current view of the workspace")
def get_view(wid: str, debug: bool = Debug):
    try:
        state = read_only(wid)
    except WorkspaceNotFound:
        raise HTTPException(404, "Workspace not found or expired. Sign in again.")
    with use_state(state):
        return ws.view(debug=debug)


@app.delete("/workspaces/{wid}", tags=["workspace"], summary="Sign out")
def sign_out(wid: str):
    delete_workspace(wid)
    return {"ok": True}


@app.post("/workspaces/{wid}/navigate", tags=["workspace"], summary="Go to the company or session list")
def navigate(wid: str, body: NavigateBody, wait: float = Wait, debug: bool = Debug):
    targets = {"companies": ws.go_companies, "sessions": ws.go_sessions}
    if body.to not in targets:
        raise HTTPException(400, "to must be 'companies' or 'sessions'")
    return _action(wid, f"navigate:{body.to}", targets[body.to], wait=wait, debug=debug)


# ───────────────────────── companies ─────────────────────────

@app.post("/workspaces/{wid}/companies/{company_id}/open", tags=["companies"])
def open_company(wid: str, company_id: int, wait: float = Wait, debug: bool = Debug):
    return _action(wid, "open_company", ws.open_company, company_id, wait=wait, debug=debug)


@app.delete("/workspaces/{wid}/companies/{company_id}", tags=["companies"])
def delete_company(wid: str, company_id: int, wait: float = Wait, debug: bool = Debug):
    return _action(wid, "delete_company", ws.delete_company, company_id, wait=wait, debug=debug)


@app.get("/workspaces/{wid}/companies/{company_id}/report", tags=["companies"],
         summary="Onboarding report and profile of a company")
def company_report(wid: str, company_id: int):
    try:
        state = read_only(wid)
    except WorkspaceNotFound:
        raise HTTPException(404, "Workspace not found or expired. Sign in again.")
    with use_state(state):
        try:
            return ws._jsonable(ws.company_report(company_id))
        except UserError as exc:
            raise HTTPException(404, str(exc))


# ───────────────────────── onboarding (add a company) ─────────────────────────

@app.post("/workspaces/{wid}/onboarding/{action}", tags=["onboarding"],
          summary="One onboarding step action",
          description=(
              "Actions by step — start: (any) · input: text, urls · narrow: country, hs2 · show_all · "
              "open_narrow · pick: index · none_of_these · give_urls: urls · skip_urls · accept_scrape · "
              "skip_links · retry_scrape: urls · review_continue: take_name, take_info · retry: extra · give_up"))
def onboarding_action(wid: str, action: str, body: OnboardingBody = OnboardingBody(),
                      wait: float = Wait, debug: bool = Debug):
    if action not in onboarding.ACTIONS:
        raise HTTPException(400, f"Unknown action. Use one of: {', '.join(onboarding.ACTIONS)}")
    args = {name: getattr(body, name) for name in _ONB_ARGS.get(action, ())
            if getattr(body, name) is not None}
    return _action(wid, f"onboarding:{action}", onboarding.run, action, wait=wait, debug=debug, **args)


# ───────────────────────── sessions ─────────────────────────

@app.post("/workspaces/{wid}/sessions", tags=["sessions"], summary="New session (then choose a scope)")
def new_session(wid: str, wait: float = Wait, debug: bool = Debug):
    return _action(wid, "new_session", ws.new_session, wait=wait, debug=debug)


@app.post("/workspaces/{wid}/sessions/{session_id}/resume", tags=["sessions"])
def resume_session(wid: str, session_id: int, wait: float = Wait, debug: bool = Debug):
    return _action(wid, "resume_session", ws.resume_session, session_id, wait=wait, debug=debug)


@app.delete("/workspaces/{wid}/sessions/{session_id}", tags=["sessions"])
def delete_session(wid: str, session_id: int, wait: float = Wait, debug: bool = Debug):
    return _action(wid, "delete_session", ws.delete_session, session_id, wait=wait, debug=debug)


@app.post("/workspaces/{wid}/session/end", tags=["sessions"], summary="End & save the current session")
def end_session(wid: str, wait: float = Wait, debug: bool = Debug):
    return _action(wid, "end_session", ws.end_session, wait=wait, debug=debug)


# ───────────────────────── scope ─────────────────────────

@app.post("/workspaces/{wid}/scope/start", tags=["scope"], summary="Start the session on an HS code or product")
def start_scope(wid: str, body: ScopeBody, wait: float = Wait, debug: bool = Debug):
    return _action(wid, "start_scope", ws.begin_with, body.choice, wait=wait, debug=debug)


@app.post("/workspaces/{wid}/scope/change", tags=["scope"], summary="Change scope during a chat")
def change_scope(wid: str, body: ScopeBody, wait: float = Wait, debug: bool = Debug):
    return _action(wid, "change_scope", ws.change_scope, body.choice, wait=wait, debug=debug)


# ───────────────────────── chat ─────────────────────────

@app.post("/workspaces/{wid}/messages", tags=["chat"], summary="Send a message (runs the full pipeline)")
def send_message(wid: str, body: MessageBody, wait: float = Wait, debug: bool = Debug):
    if body.source not in ("typed", "suggestion", "clarification"):
        raise HTTPException(400, "source must be typed, suggestion or clarification")
    return _action(wid, "message", ws.send_message, body.text, body.source, wait=wait, debug=debug)
