"""Tool-level tests: the whole gift scenario over a temporary database, no network."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from agentvisa.server import build_server
from agentvisa.store import Store


class FakePassport:
    """Stands in for the live ego.ist client so tests never touch the network."""

    def read(self, username: str) -> dict[str, Any]:
        return {"username": username, "serial": "AP-000042"}


def call(server: FastMCP, tool: str, **arguments: Any) -> dict[str, Any]:
    """Call a tool the way a mounted agent would and return its payload."""

    async def once() -> dict[str, Any]:
        async with Client(server) as client:
            return dict((await client.call_tool(tool, arguments)).data)

    return asyncio.run(once())


def trail(store: Store) -> list[str]:
    """The ledger oldest first, as plain action names."""
    return [receipt.action.value for receipt in reversed(store.receipts())]


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "visa.db")


@pytest.fixture()
def server(store: Store) -> FastMCP:
    return build_server(store, FakePassport())


def test_gift_scenario(store: Store, server: FastMCP) -> None:
    asked = call(
        server,
        "request_pass",
        app="gift-agent",
        read=["taste.summary", "commerce.budget"],
        purpose="choose a birthday gift",
        duration="session",
    )
    assert asked["ok"] and asked["status"] == "pending"
    assert call(server, "check_pass", request_id=asked["request_id"])["status"] == "pending"

    granted = store.approve_request(asked["request_id"], time.time())
    checked = call(server, "check_pass", request_id=asked["request_id"])
    assert checked["status"] == "approved"
    assert checked["pass_id"] == granted.id
    assert checked["granted_fields"] == ["taste.summary", "commerce.budget"]

    context = call(server, "read_context", pass_id=granted.id)["context"]
    assert context["commerce.approved"] is True
    assert "commerce.budget" not in context
    assert "180" not in json.dumps(context)

    outside = call(server, "read_context", pass_id=granted.id, fields=["health.resting_hr"])
    assert outside["ok"] is False
    assert outside["error"] == "OUT_OF_SCOPE"

    visa = call(
        server,
        "issue_visa",
        pass_id=granted.id,
        subset=["taste.summary"],
        holder="gift-runner",
        ttl_seconds=600,
    )
    assert visa["fields"] == ["taste.summary"]
    assert visa["expires_at"] <= granted.expires_at
    runner = call(server, "read_context", pass_id=visa["visa_id"])
    assert runner["context"] == {"taste.summary": store.fields()["taste.summary"].value}

    deeper = call(
        server,
        "issue_visa",
        pass_id=visa["visa_id"],
        subset=["taste.summary"],
        holder="runner-of-the-runner",
    )
    assert deeper["error"] == "DEPTH_LIMIT"

    queued = call(
        server,
        "suggest_write",
        pass_id=granted.id,
        path="taste.gifts",
        value="film cameras over gadgets",
        reason="learned while shortlisting gifts",
    )
    assert queued["status"] == "pending_review"
    assert "taste.gifts" not in store.fields()

    assert store.revoke(granted.id, time.time()) == 2
    dead = call(server, "read_context", pass_id=visa["visa_id"])
    assert dead["error"] == "REVOKED"

    assert trail(store) == [
        "request",
        "pass_issued",
        "read",
        "refused",
        "visa_issued",
        "read",
        "refused",
        "suggestion",
        "revoked",
        "refused",
    ]


def test_a_sensitive_field_needs_its_own_pass(server: FastMCP) -> None:
    mixed = call(
        server,
        "request_pass",
        app="gift-agent",
        read=["taste.summary", "health.resting_hr"],
        purpose="pick a gift and a workout",
    )
    assert mixed["error"] == "MIXED_SENSITIVE"


def test_a_sensitive_pass_reads_apart_and_never_delegates(store: Store, server: FastMCP) -> None:
    asked = call(
        server,
        "request_pass",
        app="coach-agent",
        read=["health.resting_hr"],
        purpose="plan recovery days",
    )
    granted = store.approve_request(asked["request_id"], time.time())

    assert call(server, "read_context", pass_id=granted.id)["ok"] is True
    assert trail(store)[-1] == "sensitive_read"

    visa = call(
        server,
        "issue_visa",
        pass_id=granted.id,
        subset=["health.resting_hr"],
        holder="coach-runner",
    )
    assert visa["error"] == "NO_SENSITIVE_DELEGATION"


def test_an_unknown_pass_is_refused(server: FastMCP) -> None:
    assert call(server, "read_context", pass_id="pass_nope")["error"] == "NOT_FOUND"


def test_the_public_read_uses_the_injected_source(store: Store, server: FastMCP) -> None:
    result = call(server, "read_public_passport", username="erin")
    assert result["profile"]["serial"] == "AP-000042"
    assert trail(store) == ["public_read"]
