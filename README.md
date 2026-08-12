# Agent Visa — issued by your AI Passport

**Your passport stays home. Your agent travels on a visa.**

The first third-party relying party for [AI Passport](https://ego.ist), built
entirely from Egoist's public docs and live MCP API. Agents never hold the
passport — they request exact fields with a purpose and a duration, the holder
approves once, every read leaves a receipt, and revocation works mid-task.

Egoist's privacy policy calls a grant *"an exact, revocable pass."* A pass
issued for travel has a name: a **visa**. This repo finishes the sentence.

*Ideathon entry — Track: Agents · Lane: Build. Not affiliated with Egoist
Machines, Inc.; built on their publicly documented MCP endpoint.*

## The loop (their published protocol, running)

```json
{
  "app": "gift-agent",
  "read": ["taste.summary", "commerce.budget"],
  "purpose": "choose a birthday gift Erin will actually like",
  "duration": "session"
}
```

request → inbox → approve → **scoped read** → receipt → **revoke (cascades)**

Plus one new primitive Egoist hasn't published yet — the **transit visa**:

```json
{
  "issue_visa": {
    "parent_pass": "pass_7f3a",
    "subset": ["taste.summary"],
    "holder": "gift-runner",
    "ttl_seconds": 600
  }
}
```

Always a subset of the parent pass. Max 10 minutes. Depth-capped at one hop.
Dies instantly when the parent is revoked. And `commerce.budget` is an
**attested field**: agents receive `commerce.approved: true` — proof a budget
exists — never the number.

## Six tools, ready when your agent is

| Tool | What it does |
|---|---|
| `request_pass` | ask for exact fields + purpose + duration (`once\|session\|persistent`) |
| `check_pass` | poll until the holder decides |
| `read_context` | scoped read; attested fields return proof, not values |
| `issue_visa` | delegate a subset to a sub-agent, TTL-bound, cascade-revocable |
| `suggest_write` | propose a memory update — lands in the holder's inbox, never writes directly |
| `read_public_passport` | **live** call to `ego.ist/api/cards/mcp` `read_public_profile` |

## Run it

```bash
uv run store.py            # fresh passport scaffold (SQLite)
uv run approval.py         # holder console → http://localhost:8787
uv run test_flow.py        # full gift-scenario self-check (needs console up)

# mount in Claude Code as an MCP server:
claude mcp add agent-visa -- uv run --directory "$PWD" server.py
```

Then ask Claude: *"Buy my friend Erin a birthday gift. Read her public
passport first, then request a session pass for my taste and budget."*
Approve on the console. Watch the receipts stamp. Hit **Revoke** mid-task —
the sub-agent's next read returns `VISA_REVOKED`.

## Real vs. scaffold

| Piece | Status |
|---|---|
| ego.ist MCP endpoint (`read_public_profile`) | **real, live calls** |
| Recipient passport (`ego.ist/i/erin`, EGO · 000002) | **real published data** |
| request → approve → read → receipt → revoke loop | **working code** (this repo, their published semantics) |
| Memory store behind the loop | SQLite scaffold — swaps for `ego.ist/connect` the day it opens |
| Payments | out of scope — that's ACP/AP2's leg of the stack |

## Why this is where AI Passport is used best

Agentic commerce already has two legs: Web Bot Auth proves **who the agent
is**, ACP/AP2 moves **the money**. Nobody owns the third — **what the agent
may know and decide on your behalf**. That leg is shaped exactly like AI
Passport: scoped fields, purpose, duration, receipts, revocation. The gift
scenario is the wedge, and it answers the cold-start question at serial
000002: why publish a passport? Because people who love you shop better when
you do.
