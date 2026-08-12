# /// script
# requires-python = ">=3.11"
# dependencies = ["fastmcp>=2", "httpx"]
# ///
"""Agent Visa — MCP server. The first third-party relying party for AI Passport.

Your passport stays home. Your agent travels on a visa.

Implements Egoist's published request -> approve -> scoped read -> receipt -> revoke
loop (their B2B docs) against a local SQLite scaffold, plus ONE new primitive:
issue_visa — a sub-agent grant that is always a subset, always shorter-lived,
and dies when the parent pass is revoked.

Identity anchor is real: read_public_passport() calls ego.ist's live MCP API.
"""
import json
import time

import httpx
from fastmcp import FastMCP

import store

APPROVAL_URL = "http://localhost:8787"
EGOIST_MCP = "https://ego.ist/api/cards/mcp"
SESSION_SECONDS = 60 * 60
MAX_VISA_TTL = 600  # transit visas: 10 minutes max

mcp = FastMCP("agent-visa")


def _err(code: str, message: str) -> dict:
    return {"ok": False, "error": code, "message": message}


@mcp.tool
def request_pass(app: str, read: list[str], purpose: str, duration: str = "session") -> dict:
    """Ask the passport holder for a pass: exact fields, a purpose, a duration
    (once | session | persistent). Returns a pending request_id — the holder
    approves or denies it in their inbox. Poll check_pass."""
    if duration not in ("once", "session", "persistent"):
        return _err("BAD_DURATION", "duration must be once | session | persistent")
    con = store.connect()
    known = {r["path"] for r in con.execute("SELECT path FROM passport")}
    unknown = [f for f in read if f not in known]
    if unknown:
        con.close()
        return _err("UNKNOWN_FIELDS", f"not in this passport: {unknown}")
    rid = store.new_id("req")
    with con:
        con.execute(
            "INSERT INTO requests(id, app, fields, purpose, duration, status, ts) VALUES (?,?,?,?,?, 'pending', ?)",
            (rid, app, json.dumps(read), purpose, duration, time.time()),
        )
    store.receipt(con, None, app, "request", f"{app} asked for {read} — “{purpose}” ({duration})")
    con.close()
    return {"ok": True, "request_id": rid, "status": "pending", "approve_at": APPROVAL_URL}


@mcp.tool
def check_pass(request_id: str) -> dict:
    """Poll a pass request. Returns status; when approved, the pass_id to read with."""
    con = store.connect()
    req = con.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
    if req is None:
        con.close()
        return _err("NOT_FOUND", "unknown request_id")
    if req["status"] != "approved":
        con.close()
        return {"ok": True, "status": req["status"]}
    visa = con.execute("SELECT * FROM visas WHERE request_id=?", (request_id,)).fetchone()
    con.close()
    return {
        "ok": True,
        "status": "approved",
        "pass_id": visa["id"],
        "granted_fields": store.fields_of(visa),
        "expires": visa["expires"],
    }


def _live_row(con, visa_id):
    row = con.execute("SELECT * FROM visas WHERE id=?", (visa_id,)).fetchone()
    if row is None:
        return None, _err("NOT_FOUND", "unknown pass/visa id")
    state = store.visa_state(row)
    if state == "revoked":
        return None, _err("VISA_REVOKED", "this pass was revoked by the passport holder")
    if state == "expired":
        return None, _err("VISA_EXPIRED", "this pass has expired")
    return row, None


@mcp.tool
def read_context(pass_id: str, fields: list[str] | None = None) -> dict:
    """Read scoped context with an approved pass or visa. Attested fields return
    proof (true/false), never the value. Every read leaves a receipt."""
    con = store.connect()
    row, err = _live_row(con, pass_id)
    if err:
        holder = "unknown"
        if (r := con.execute("SELECT holder FROM visas WHERE id=?", (pass_id,)).fetchone()):
            holder = r["holder"]
        store.receipt(con, pass_id, holder, "denied-read", f"read refused: {err['error']}")
        con.close()
        return err
    granted = store.fields_of(row)
    want = fields or granted
    outside = [f for f in want if f not in granted]
    if outside:
        store.receipt(con, pass_id, row["holder"], "denied-read", f"asked outside scope: {outside}")
        con.close()
        return _err("OUT_OF_SCOPE", f"pass does not cover: {outside}")
    out = {}
    for f in want:
        p = con.execute("SELECT * FROM passport WHERE path=?", (f,)).fetchone()
        out[f.rsplit(".", 1)[0] + ".approved" if p["attest"] else f] = (True if p["attest"] else p["value"])
    with con:
        con.execute("UPDATE visas SET used = used + 1 WHERE id=?", (row["id"],))
    store.receipt(con, pass_id, row["holder"], "read", f"read {want}")
    con.close()
    return {"ok": True, "context": out, "receipt": "logged"}


@mcp.tool
def issue_visa(pass_id: str, subset: list[str], holder: str, ttl_seconds: int = 600) -> dict:
    """Issue a transit visa to a sub-agent: always a subset of the parent pass,
    max 10 minutes, revocation cascades from the parent."""
    con = store.connect()
    parent, err = _live_row(con, pass_id)
    if err:
        con.close()
        return err
    if parent["parent_id"] is not None:
        con.close()
        return _err("DEPTH_LIMIT", "a transit visa cannot issue further visas")
    granted = store.fields_of(parent)
    outside = [f for f in subset if f not in granted]
    if outside:
        con.close()
        return _err("NOT_SUBSET", f"parent pass does not cover: {outside}")
    ttl = min(int(ttl_seconds), MAX_VISA_TTL)
    expires = time.time() + ttl
    if parent["expires"] is not None:
        expires = min(expires, parent["expires"])
    vid = store.new_id("visa")
    with con:
        con.execute(
            "INSERT INTO visas(id, parent_id, holder, fields, purpose, duration, expires, ts)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (vid, parent["id"], holder, json.dumps(subset), parent["purpose"], "session", expires, time.time()),
        )
    store.receipt(con, vid, holder, "visa-issued",
                  f"transit visa for {holder}: {subset}, TTL {ttl}s, parent {parent['id']}")
    con.close()
    return {"ok": True, "visa_id": vid, "fields": subset, "expires": expires, "revoke": "cascades from parent"}


@mcp.tool
def suggest_write(pass_id: str, path: str, value: str, reason: str) -> dict:
    """Propose a durable memory update. Never writes directly — lands in the
    holder's inbox for review (Egoist's suggested-writes model)."""
    con = store.connect()
    row, err = _live_row(con, pass_id)
    if err:
        con.close()
        return err
    with con:
        con.execute(
            "INSERT INTO suggestions(visa_id, path, value, reason, status, ts) VALUES (?,?,?,?, 'pending', ?)",
            (row["id"], path, value, reason, time.time()),
        )
    store.receipt(con, pass_id, row["holder"], "suggest-write", f"proposed {path} = “{value}” — {reason}")
    con.close()
    return {"ok": True, "status": "pending review in holder inbox"}


@mcp.tool
def read_public_passport(username: str) -> dict:
    """LIVE call to ego.ist's public MCP API: read a published passport by
    username. Public data — no pass needed, but the read is still stamped."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "read_public_profile", "arguments": {"username": username}}}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    r = httpx.post(EGOIST_MCP, json=body, headers=headers, timeout=20)
    r.raise_for_status()
    text = r.text
    payload = None
    for line in text.splitlines():  # endpoint replies as SSE event lines
        if line.startswith("data: "):
            payload = json.loads(line[6:])
            break
    if payload is None:
        payload = json.loads(text)
    inner = json.loads(payload["result"]["content"][0]["text"])
    con = store.connect()
    store.receipt(con, None, "agent", "public-read", f"live ego.ist read_public_profile(“{username}”)")
    con.close()
    return inner


if __name__ == "__main__":
    store.init()
    mcp.run()
