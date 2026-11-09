# EXIRA API

The whole app as an HTTP API, so any front end (Streamlit, React, PHP, mobile)
can use it. The logic lives in `exira_core/` (no Streamlit); this folder is the
HTTP layer.

## Run

    uvicorn api.main:app --port 8000

Open **http://localhost:8000/docs** to see and try every endpoint.

Uses the same `.env` as the app (`REDIS_URL`, `DATABASE_URL`, `OPENROUTER_API_KEY`,
Snowflake settings). Optional: `API_WORKERS` (parallel jobs, default 8),
`API_CORS_ORIGINS` (default `*`).

## How a UI uses it

1. **Sign in** — `POST /workspaces {"user_id": "..."}` → `workspace_id` + `view`.
   Keep the `workspace_id` for the browser tab.
2. **Render the view.** `view.stage` says which screen to show:

   | stage | screen | main actions |
   |---|---|---|
   | `onboarding` | add a company (wizard, see `view.onboarding.step`) | `POST /onboarding/{action}` |
   | `company_picker` | list `view.companies` | open / delete company, `onboarding/start` |
   | `session_picker` | list `view.sessions` | new / resume / delete session, navigate |
   | `scope` | choose HS code or product (`view.candidates`) | `POST /scope/start` |
   | `chat` | `view.chat.turns`, `options`, `clarification`, `selecting` | `POST /messages`, `/scope/change`, `/session/end` |
   | `done` | `view.session_notes` | navigate to companies, sign out |

3. **Every button is an action**, and actions run as **jobs**:

       POST /workspaces/{id}/messages {"text": "...", "source": "typed"}
         -> 202 {"job_id": "...", "status": "queued"}
       GET /jobs/{job_id}
         -> {"status": "running", "progress": [{"at": 1.2, "message": "Running trade analysis…"}]}
         -> {"status": "done", "view": {...}}        render this view

   `status` ends as `done`, `failed` (a user-facing problem, see `error`) or
   `error` (something crashed). Add `?wait=30` to any action to wait for the
   result in one call instead of polling.

4. `view.notices` carries info / warning / error messages from the last action.

## Chat message `source`

- `typed` — typed in the chat box
- `suggestion` — a suggested question or HS-domain button (`view.chat.options`)
- `clarification` — an option of `view.chat.clarification`

## Debug

Add `?debug=true` to any call to get full traces (SQL, prompts, flow, resolution)
in the view. Without it, assistant turns carry only what charts need
(`visualization`, `rows`, `charts`, `thinking`, `web_sources`).

## Not done yet

No authentication: the API trusts the `user_id` it is given.
