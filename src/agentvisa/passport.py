"""Client for ego.ist's public passport API, the one outbound call this server makes."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

import httpx

from .models import ErrorCode, VisaError

EGOIST_MCP_URL = "https://ego.ist/api/cards/mcp"
EGOIST_TOOL = "read_public_profile"

# Egoist's published username rule; checked before any request leaves this process.
USERNAME_PATTERN = re.compile(r"^[a-z0-9_]{4,20}$")

# One outbound hop, no retries: a slow public read must not stall an agent's turn.
TIMEOUT_SECONDS = 15.0


class PassportSource(Protocol):
    """Anything that can resolve a username to a published passport document."""

    def read(self, username: str) -> dict[str, Any]:
        """Return the published passport, or raise VisaError(UPSTREAM_ERROR)."""


class PublicPassportClient:
    """Reads live published passports from ego.ist. No cache, no retry."""

    def read(self, username: str) -> dict[str, Any]:
        """Return the published passport for username, or raise VisaError(UPSTREAM_ERROR)."""
        if not USERNAME_PATTERN.fullmatch(username):
            raise VisaError(
                ErrorCode.UPSTREAM_ERROR,
                f"username {username!r} does not match {USERNAME_PATTERN.pattern}",
            )
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": EGOIST_TOOL, "arguments": {"username": username}},
        }
        try:
            response = httpx.post(
                EGOIST_MCP_URL,
                json=body,
                headers={"Accept": "application/json, text/event-stream"},
                timeout=TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException as exc:
            raise VisaError(
                ErrorCode.UPSTREAM_ERROR, f"ego.ist did not answer within {TIMEOUT_SECONDS}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise VisaError(ErrorCode.UPSTREAM_ERROR, f"ego.ist unreachable: {exc}") from exc

        if response.status_code != 200:
            raise VisaError(
                ErrorCode.UPSTREAM_ERROR, f"ego.ist returned HTTP {response.status_code}"
            )
        return _parse(response.text)


def _parse(body: str) -> dict[str, Any]:
    """Unwrap the server-sent-event envelope and the JSON string nested inside it."""
    envelope = _first_event(body)
    if "error" in envelope:
        raise VisaError(ErrorCode.UPSTREAM_ERROR, f"ego.ist error: {envelope['error']}")
    try:
        text = envelope["result"]["content"][0]["text"]
        profile = json.loads(text)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise VisaError(ErrorCode.UPSTREAM_ERROR, f"unexpected reply from ego.ist: {exc}") from exc
    if not isinstance(profile, dict):
        raise VisaError(ErrorCode.UPSTREAM_ERROR, "ego.ist returned a non-object profile")
    return profile


def _first_event(body: str) -> dict[str, Any]:
    """Return the first SSE data frame, falling back to a plain JSON body."""
    for line in body.splitlines():
        if line.startswith("data: "):
            payload = line[len("data: ") :]
            break
    else:
        payload = body
    try:
        envelope = json.loads(payload)
    except ValueError as exc:
        raise VisaError(ErrorCode.UPSTREAM_ERROR, f"ego.ist reply was not JSON: {exc}") from exc
    if not isinstance(envelope, dict):
        raise VisaError(ErrorCode.UPSTREAM_ERROR, "ego.ist reply was not a JSON-RPC object")
    return envelope
