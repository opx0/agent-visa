"""Rebuild the demo database in one command: agentvisa-demo [--revoked].

Every agent action here goes through the real MCP tools over an in-memory client, so the
seeded ledger is exactly the ledger a live session leaves. The console need not be running.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from typing import Any

from fastmcp import Client

from agentvisa.passport import PublicPassportClient
from agentvisa.server import CONSOLE_URL, build_server
from agentvisa.store import Store, database_path

GIFT_AGENT = "gift-agent"
TRIP_AGENT = "weekender.app"
RUNNER = "gift-runner"


async def seed(store: Store, revoked: bool) -> None:
    """Seed one waiting request, one live pass, one transit visa and one suggestion."""
    async with Client(build_server(store, PublicPassportClient())) as agent:

        async def call(tool: str, **arguments: Any) -> dict[str, Any]:
            return dict((await agent.call_tool(tool, arguments)).data)

        waiting = await call(
            "request_pass",
            app=TRIP_AGENT,
            read=["availability.weekends", "taste.summary"],
            purpose="suggest a weekend she would enjoy",
            duration="once",
        )
        asked = await call(
            "request_pass",
            app=GIFT_AGENT,
            read=["taste.summary", "commerce.budget"],
            purpose="choose a birthday gift she will actually like",
            duration="session",
        )
        granted = store.approve_request(asked["request_id"], time.time())

        context = await call("read_context", pass_id=granted.id)
        refused = await call("read_context", pass_id=granted.id, fields=["health.resting_hr"])
        visa = await call(
            "issue_visa",
            pass_id=granted.id,
            subset=["taste.summary"],
            holder=RUNNER,
            ttl_seconds=600,
        )
        await call("read_context", pass_id=visa["visa_id"])
        await call(
            "suggest_write",
            pass_id=granted.id,
            path="taste.gifts",
            value="film cameras over gadgets",
            reason="learned while shortlisting gifts",
        )

        print(f"database    {store.path}")
        print(f"console     {CONSOLE_URL}")
        print(f"waiting     {waiting['request_id']}  {TRIP_AGENT}, needs the holder")
        print(f"pass        {granted.id}  {GIFT_AGENT}, {', '.join(granted.fields)}")
        print(f"context     {context['context']}")
        print(f"refused     {refused['error']}, {refused['message']}")
        print(
            f"visa        {visa['visa_id']}  {RUNNER}, {', '.join(visa['fields'])},"
            f" {visa['expires_at'] - time.time():.0f}s left"
        )
        print("suggestion  taste.gifts, waiting for review")

        if revoked:
            killed = store.revoke(granted.id, time.time())
            dead = await call("read_context", pass_id=visa["visa_id"])
            print(f"revoked     {granted.id}, cascade killed {killed} live grants")
            print(f"runner      {dead['error']}, {dead['message']}")


def main() -> None:
    """Delete the demo database and seed it again from scratch."""
    parser = argparse.ArgumentParser(description="Seed the Agent Visa demo state.")
    parser.add_argument("--revoked", action="store_true", help="also revoke the pass in cascade")
    revoked = parser.parse_args().revoked
    path = database_path()
    path.unlink(missing_ok=True)
    asyncio.run(seed(Store(path), revoked))


if __name__ == "__main__":
    main()
