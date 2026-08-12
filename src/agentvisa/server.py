"""The six MCP tools: parse the input, ask policy, touch the store, return a payload.

Payloads are written for a language model, since the caller is one: every message says
what the agent should do next, and every refusal names a code from the closed set.
"""

from __future__ import annotations

import functools
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, ParamSpec, TypeVar

from fastmcp import FastMCP

from agentvisa import policy
from agentvisa.models import ErrorCode, RequestStatus, VisaError
from agentvisa.passport import PassportSource, PublicPassportClient, ThrottledPassportSource
from agentvisa.store import Store, database_path

# Where the agent is told to send the person; console.py serves this address.
CONSOLE_URL = os.environ.get("AGENTVISA_CONSOLE_URL", "http://127.0.0.1:8787")

# The ledger names the person "holder", so the other side of the desk is the agent.
AGENT = "agent"

P = ParamSpec("P")
T = TypeVar("T")


def _reply(tool: Callable[P, dict[str, Any]]) -> Callable[P, dict[str, Any]]:
    """Turn any VisaError into the one error payload, so no tool repeats try/except."""

    @functools.wraps(tool)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> dict[str, Any]:
        try:
            return tool(*args, **kwargs)
        except VisaError as error:
            return {"ok": False, "error": error.code.value, "message": error.message}

    return wrapper


def _found(record: T | None, what: str) -> T:
    """Return the record, or refuse with NOT_FOUND rather than handing back None."""
    if record is None:
        raise VisaError(ErrorCode.NOT_FOUND, f"no {what} with that id")
    return record


@contextmanager
def _audit(store: Store, grant_id: str) -> Iterator[None]:
    """Stamp any refusal raised inside the block, then let it travel on to _reply."""
    try:
        yield
    except VisaError as error:
        grant = store.grant(grant_id)
        actor = grant.holder if grant is not None else AGENT
        store.log_refusal(grant_id, actor, error, time.time())
        raise


def build_server(store: Store, source: PassportSource) -> FastMCP:
    """Mount the six tools over one store and one passport source; tests pass fakes."""
    mcp: FastMCP = FastMCP("agent-visa")

    @mcp.tool
    @_reply
    def request_pass(
        app: str,
        read: list[str],
        purpose: str,
        duration: str = "session",
    ) -> dict[str, Any]:
        """Ask the passport holder for a pass over exact dot-path fields, with a purpose and a
        duration of once, session or persistent. Nothing is readable until the holder approves in
        their console, so poll check_pass with the request id rather than asking again.
        """
        fields, parsed = policy.validate_request(read, store.fields(), duration)
        request = store.create_request(app, fields, purpose, parsed, time.time())
        return {
            "ok": True,
            "request_id": request.id,
            "status": "pending",
            "approve_at": CONSOLE_URL,
            "message": f"Ask the person to approve this at {CONSOLE_URL}, then poll check_pass.",
        }

    @mcp.tool
    @_reply
    def check_pass(request_id: str) -> dict[str, Any]:
        """Report whether a request is pending, denied, or approved. When approved it returns the
        pass id and the exact fields granted; the holder can revoke that pass at any moment.
        """
        request = _found(store.request(request_id), "request")
        if request.status is RequestStatus.DENIED:
            return {
                "ok": True,
                "status": "denied",
                "message": "The holder refused. Do not ask again for these fields.",
            }
        if request.status is not RequestStatus.APPROVED:
            return {
                "ok": True,
                "status": "pending",
                "approve_at": CONSOLE_URL,
                "message": "Not approved yet. Wait, then poll check_pass again.",
            }
        grant = _found(store.grant_for_request(request_id), "pass")
        return {
            "ok": True,
            "status": "approved",
            "pass_id": grant.id,
            "granted_fields": list(grant.fields),
            "duration": grant.duration.value,
            "expires_at": grant.expires_at,
            "message": "Read these fields with read_context(pass_id). Nothing else is covered.",
        }

    @mcp.tool
    @_reply
    def read_context(pass_id: str, fields: list[str] | None = None) -> dict[str, Any]:
        """Read granted context with a pass or a transit visa; omit fields to read all of them.
        An attested field returns proof instead of its value (commerce.budget arrives as
        commerce.approved), anything outside the grant is refused, and every read and every
        refusal leaves a receipt the holder can see.
        """
        now = time.time()
        with _audit(store, pass_id):
            grant = _found(store.grant(pass_id), "pass or visa")
            paths = policy.authorize_read(grant, fields, now)
        passport = store.fields()
        granted = [passport[path] for path in paths]
        store.record_read(grant, granted, now)
        return {"ok": True, "context": policy.project(granted), "receipt": "recorded"}

    @mcp.tool
    @_reply
    def issue_visa(
        pass_id: str,
        subset: list[str],
        holder: str,
        ttl_seconds: int = 600,
    ) -> dict[str, Any]:
        """Send a sub-agent out on a transit visa: always a subset of your pass, never a special
        category, capped at 600 seconds, and unable to issue a visa of its own. It stops working
        the moment the parent pass expires or the holder revokes it.
        """
        now = time.time()
        with _audit(store, pass_id):
            parent = _found(store.grant(pass_id), "pass")
            terms = policy.authorize_delegation(parent, subset, ttl_seconds, now, store.fields())
        visa = store.create_visa(parent, terms, holder, now)
        return {
            "ok": True,
            "visa_id": visa.id,
            "fields": list(visa.fields),
            "expires_at": visa.expires_at,
            "message": f"Hand this visa id to {holder}. It cannot widen its scope or outlive you.",
        }

    @mcp.tool
    @_reply
    def suggest_write(pass_id: str, path: str, value: str, reason: str) -> dict[str, Any]:
        """Propose a change to the holder's passport. Agents never write: the suggestion is queued
        for the holder to accept or reject, and the proposal itself leaves a receipt.
        """
        now = time.time()
        with _audit(store, pass_id):
            grant = _found(store.grant(pass_id), "pass or visa")
            policy.require_active(grant, now)
        suggestion = store.queue_suggestion(grant, path, value, reason, now)
        return {
            "ok": True,
            "suggestion_id": suggestion.id,
            "status": "pending_review",
            "message": "Queued for the holder. The passport is unchanged until they accept it.",
        }

    @mcp.tool
    @_reply
    def read_public_passport(username: str) -> dict[str, Any]:
        """Read somebody's published ego.ist passport by username. This is public data and needs
        no pass, but the lookup is still recorded as a receipt.
        """
        profile = source.read(username)
        store.log_public_read(username, AGENT, time.time())
        return {"ok": True, "username": username, "profile": profile}

    return mcp


def main() -> None:
    """Serve the real database and the live ego.ist client over stdio."""
    build_server(Store(database_path()), PublicPassportClient()).run()


def main_http() -> None:
    """Same server over HTTP, so a remote agent can mount it like any hosted MCP server.

    Stateless, because a hosted demo should answer a bare tools/list the way ego.ist's own
    endpoint does, rather than making every client open a session first.
    """
    source = ThrottledPassportSource(PublicPassportClient())
    server = build_server(Store(database_path()), source)
    hosts = os.environ.get("AGENTVISA_ALLOWED_HOSTS", "")
    server.run(
        transport="http",
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8788")),
        path="/mcp",
        stateless_http=True,
        **({"allowed_hosts": hosts.split(",")} if hosts else {}),
    )


if __name__ == "__main__":
    main()
