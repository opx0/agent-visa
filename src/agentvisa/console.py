"""The holder console: the desk where a person approves a request, watches it live, and revokes it.

One page plus a server-sent-event stream of the current state. Every decision offered here is
carried out by the Store; this module parses the path, calls it, and renders the result.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from html import escape
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .models import ErrorCode, SuggestionStatus, VisaError
from .policy import resolve_state
from .store import Store, database_path

HOST = "127.0.0.1"
PORT = 8787

# The page runs its own countdown off the server clock, so the stream only has to be fast
# enough that an approval or a revocation looks instant to the person watching.
POLL_SECONDS = 0.5

# Enough ledger to show a whole demo run without shipping the archive on every frame.
LEDGER_LIMIT = 40

_STATUS = {
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.ALREADY_DECIDED: 409,
}

PAGE = (Path(__file__).parent / "templates" / "console.html").read_text(encoding="utf-8")

# A hosted copy is a shared desk: say so, rather than letting a visitor think the
# grants they are revoking are their own.
NOTICE = os.environ.get("AGENTVISA_NOTICE", "")
if NOTICE:
    PAGE = PAGE.replace("</header>", f'<p class="notice">{escape(NOTICE)}</p></header>', 1)

app = FastAPI(title="Agent Visa holder console")
store = Store(database_path())


@app.exception_handler(VisaError)
async def refusal(request: Request, exc: Exception) -> JSONResponse:
    """Render the closed set of refusals as JSON with a status code the browser can act on."""
    assert isinstance(exc, VisaError)
    return JSONResponse(
        {"error": exc.code.value, "message": exc.message},
        status_code=_STATUS.get(exc.code, 400),
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the console page, which fetches everything else over the event stream."""
    return HTMLResponse(PAGE)


@app.get("/events")
async def events() -> StreamingResponse:
    """Stream a full state snapshot whenever the state changes, as server-sent events."""

    async def stream() -> AsyncIterator[str]:
        last: dict[str, Any] | None = None
        while True:
            state = _state()
            if state != last:
                last = state
                yield f"data: {json.dumps(state | {'now': time.time()})}\n\n"
            await asyncio.sleep(POLL_SECONDS)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/requests/{request_id}/approve")
async def approve(request_id: str) -> dict[str, str]:
    """Approve a pending request and hand back the identifier of the pass it issued."""
    grant = store.approve_request(request_id, time.time())
    return {"pass_id": grant.id}


@app.post("/api/requests/{request_id}/deny")
async def deny(request_id: str) -> dict[str, str]:
    """Deny a pending request; the app is told no and the refusal is stamped."""
    store.deny_request(request_id, time.time())
    return {"request_id": request_id}


@app.post("/api/grants/{grant_id}/revoke")
async def revoke(grant_id: str) -> dict[str, int]:
    """Revoke a pass or visa, taking every visa issued from it down in the same transaction."""
    return {"revoked": store.revoke(grant_id, time.time())}


@app.post("/api/suggestions/{suggestion_id}/{decision}")
async def resolve(suggestion_id: int, decision: str) -> dict[str, str]:
    """Accept a proposed write into the passport, or reject it; no agent writes either way."""
    if decision not in ("accept", "reject"):
        raise VisaError(ErrorCode.NOT_FOUND, "a suggestion is either accepted or rejected")
    store.resolve_suggestion(suggestion_id, decision == "accept", time.time())
    return {"decision": decision}


def _state() -> dict[str, Any]:
    """The whole console view, without the clock, so two snapshots compare as equal."""
    now = time.time()
    return {
        "fields": {
            path: {"attested": field.attested, "sensitive": field.sensitive}
            for path, field in store.fields().items()
        },
        "requests": [
            {
                "id": request.id,
                "app": request.app,
                "fields": list(request.fields),
                "purpose": request.purpose,
                "duration": request.duration.value,
            }
            for request in store.pending_requests()
        ],
        "grants": [
            {
                "id": grant.id,
                "parent_id": grant.parent_id,
                "holder": grant.holder,
                "fields": list(grant.fields),
                "purpose": grant.purpose,
                "duration": grant.duration.value,
                "state": resolve_state(grant, now).value,
                "expires_at": grant.expires_at,
                "reads": grant.reads,
            }
            for grant in store.grants()
        ],
        "suggestions": [
            {
                "id": suggestion.id,
                "grant_id": suggestion.grant_id,
                "path": suggestion.path,
                "value": suggestion.value,
                "reason": suggestion.reason,
            }
            for suggestion in store.suggestions()
            if suggestion.status is SuggestionStatus.PENDING
        ],
        "receipts": [
            {
                "id": receipt.id,
                "grant_id": receipt.grant_id,
                "actor": receipt.actor,
                "action": receipt.action.value,
                "detail": receipt.detail,
                "at": receipt.created_at,
            }
            for receipt in store.receipts(LEDGER_LIMIT)
        ],
    }


def main() -> None:
    """Run the console on the loopback interface only; this page speaks for the passport holder."""
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
