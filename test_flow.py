# /// script
# requires-python = ">=3.11"
# dependencies = ["fastmcp>=2", "httpx"]
# ///
"""Self-check: full gift-scenario loop. Fails loudly if any step breaks.
Requires approval.py running on :8787. Run: uv run test_flow.py"""
import time

import httpx

import store
import server

A = "http://localhost:8787"


def main():
    store.init(reset=True)

    # 0. live ego.ist read
    erin = server.read_public_passport("erin")
    assert erin["ok"] and erin["username"] == "erin", erin
    print("LIVE ego.ist read ok:", erin["profileUrl"], erin["serial"])

    # 1. request a pass
    r = server.request_pass("gift-agent", ["taste.summary", "commerce.budget"],
                               "choose a birthday gift Erin will actually like", "session")
    assert r["ok"], r
    rid = r["request_id"]
    assert server.check_pass(rid)["status"] == "pending"

    # 2. holder approves in console
    assert httpx.post(f"{A}/api/approve/{rid}").json()["ok"]
    chk = server.check_pass(rid)
    assert chk["status"] == "approved", chk
    pid = chk["pass_id"]

    # 3. scoped read — budget comes back as attestation, never the number
    ctx = server.read_context(pid)["context"]
    assert ctx["commerce.approved"] is True and "180" not in str(ctx), ctx
    print("scoped read ok:", ctx)

    # 4. out-of-scope read refused
    assert server.read_context(pid, ["health.resting_hr"])["error"] == "OUT_OF_SCOPE"

    # 5. transit visa: subset only, depth 1
    v = server.issue_visa(pid, ["taste.summary"], "gift-runner", 600)
    assert v["ok"], v
    vid = v["visa_id"]
    assert server.issue_visa(pid, ["health.resting_hr"], "x")["error"] == "NOT_SUBSET"
    assert server.issue_visa(vid, ["taste.summary"], "x")["error"] == "DEPTH_LIMIT"
    assert server.read_context(vid)["context"]["taste.summary"]
    print("transit visa ok:", vid)

    # 6. suggested write lands in inbox, holder accepts
    server.suggest_write(pid, "taste.gifts", "loved the film camera idea", "learned during gift search")
    con = store.connect()
    sid = con.execute("SELECT id FROM suggestions").fetchone()["id"]
    con.close()
    assert httpx.post(f"{A}/api/suggestion/{sid}/accept").json()["ok"]

    # 7. THE CLIMAX — revoke parent, cascade kills the sub-agent
    assert httpx.post(f"{A}/api/revoke/{pid}").json()["cascade"] == 2
    dead = server.read_context(vid)
    assert dead["error"] == "VISA_REVOKED", dead
    print("cascade revoke ok: sub-agent read →", dead["error"])

    print("\nALL CHECKS PASSED — the loop is real.")


if __name__ == "__main__":
    main()
