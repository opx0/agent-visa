# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi", "uvicorn"]
# ///
"""Agent Visa — holder console. The approval inbox, passes, receipts, revoke.

Run:  uv run approval.py   ->  http://localhost:8787
"""
import asyncio
import json
import time

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

import store

app = FastAPI()


def snapshot() -> dict:
    con = store.connect()
    reqs = [dict(r) for r in con.execute(
        "SELECT * FROM requests WHERE status='pending' ORDER BY ts DESC")]
    visas = []
    for r in con.execute("SELECT * FROM visas ORDER BY ts DESC"):
        d = dict(r)
        d["state"] = store.visa_state(r)
        d["fields"] = store.fields_of(r)
        visas.append(d)
    sugg = [dict(r) for r in con.execute(
        "SELECT * FROM suggestions WHERE status='pending' ORDER BY ts DESC")]
    rec = [dict(r) for r in con.execute(
        "SELECT * FROM receipts ORDER BY id DESC LIMIT 40")]
    con.close()
    for r in reqs:
        r["fields"] = json.loads(r["fields"])
    return {"requests": reqs, "visas": visas, "suggestions": sugg, "receipts": rec, "now": time.time()}


@app.get("/events")
async def events():
    async def gen():
        last = None
        while True:
            snap = json.dumps(snapshot())
            if snap != last:
                yield f"data: {snap}\n\n"
                last = snap
            await asyncio.sleep(0.8)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/approve/{rid}")
def approve(rid: str):
    con = store.connect()
    req = con.execute("SELECT * FROM requests WHERE id=? AND status='pending'", (rid,)).fetchone()
    if req is None:
        con.close()
        return JSONResponse({"ok": False}, status_code=404)
    expires = time.time() + 60 * 60 if req["duration"] == "session" else None
    vid = store.new_id("pass")
    with con:
        con.execute("UPDATE requests SET status='approved' WHERE id=?", (rid,))
        con.execute(
            "INSERT INTO visas(id, parent_id, holder, fields, purpose, duration, expires, request_id, ts)"
            " VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?)",
            (vid, req["app"], req["fields"], req["purpose"], req["duration"], expires, rid, time.time()),
        )
    store.receipt(con, vid, "holder", "approved",
                  f"issued {vid} to {req['app']} ({req['duration']})")
    con.close()
    return {"ok": True, "pass_id": vid}


@app.post("/api/deny/{rid}")
def deny(rid: str):
    con = store.connect()
    with con:
        con.execute("UPDATE requests SET status='denied' WHERE id=?", (rid,))
    store.receipt(con, None, "holder", "denied", f"denied request {rid}")
    con.close()
    return {"ok": True}


@app.post("/api/revoke/{vid}")
def revoke(vid: str):
    con = store.connect()
    with con:
        n = con.execute(
            "UPDATE visas SET revoked=1 WHERE id=? OR parent_id=?", (vid, vid)).rowcount
    store.receipt(con, vid, "holder", "revoked",
                  f"revoked {vid} — cascade hit {n} grant(s)")
    con.close()
    return {"ok": True, "cascade": n}


@app.post("/api/suggestion/{sid}/{action}")
def suggestion(sid: int, action: str):
    if action not in ("accept", "reject"):
        return JSONResponse({"ok": False}, status_code=400)
    con = store.connect()
    s = con.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
    if s is None:
        con.close()
        return JSONResponse({"ok": False}, status_code=404)
    with con:
        con.execute("UPDATE suggestions SET status=? WHERE id=?",
                    ("accepted" if action == "accept" else "rejected", sid))
        if action == "accept":
            con.execute(
                "INSERT INTO passport(path, value) VALUES (?, ?)"
                " ON CONFLICT(path) DO UPDATE SET value=excluded.value",
                (s["path"], s["value"]))
    store.receipt(con, s["visa_id"], "holder", f"write-{action}ed",
                  f"{s['path']} = “{s['value']}”")
    con.close()
    return {"ok": True}


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Visa — holder console</title>
<style>
:root { --cobalt:#234C92; --coral:#F15A3A; --paper:#FAFAFA; --ink:#111318;
        --highlight:#F2E600; --ok:#1E9E6A; }
* { box-sizing:border-box; margin:0; }
body { background:repeating-linear-gradient(0deg,#f4f2ec,#f4f2ec 28px,#efede6 29px);
       font-family:ui-sans-serif,system-ui,'Helvetica Neue',sans-serif; color:var(--ink);
       display:flex; flex-direction:column; align-items:center; padding:28px 12px 60px; }
h1 { font-size:15px; letter-spacing:.28em; text-transform:uppercase; color:#666; margin-bottom:16px; }
#phone { width:410px; max-width:96vw; background:var(--paper); border-radius:34px;
         border:1px solid #ddd; box-shadow:0 24px 60px rgba(20,30,60,.18); overflow:hidden; }
#card { background:linear-gradient(135deg,var(--cobalt),#16305e 70%); color:#fff; padding:22px 22px 18px; }
#card .kicker { font-size:10px; letter-spacing:.3em; opacity:.75; text-transform:uppercase; }
#card .name { font-size:24px; font-weight:700; letter-spacing:.02em; margin-top:6px; }
#card .serial { font-family:ui-monospace,monospace; font-size:11px; margin-top:8px; opacity:.8; }
#card .chip { float:right; width:38px; height:28px; border-radius:6px;
              background:linear-gradient(135deg,var(--highlight),var(--coral)); }
section { padding:16px 18px 4px; }
section h2 { font-size:11px; letter-spacing:.22em; text-transform:uppercase; color:#8a8f99;
             border-bottom:1px solid #e8e8e8; padding-bottom:6px; margin-bottom:10px; }
.req, .visa, .sugg { border:1px solid #e3e3e3; border-radius:14px; padding:12px 14px; margin-bottom:10px;
                     background:#fff; }
.req { border-left:4px solid var(--coral); }
.app { font-weight:700; font-size:14px; }
.purpose { font-size:12.5px; color:#444; margin:4px 0; }
.fields { display:flex; flex-wrap:wrap; gap:5px; margin:7px 0; }
.f { font-family:ui-monospace,monospace; font-size:10.5px; background:#eef2fa; color:var(--cobalt);
     border:1px solid #d7e0f2; border-radius:6px; padding:2px 7px; }
.meta { font-size:10.5px; color:#8a8f99; font-family:ui-monospace,monospace; }
.row { display:flex; gap:8px; margin-top:9px; }
button { border:0; border-radius:9px; padding:8px 14px; font-size:12.5px; font-weight:600; cursor:pointer; }
.approve { background:var(--ink); color:#fff; flex:1; }
.denyb, .rej { background:#eee; color:#333; }
.revoke { background:var(--coral); color:#fff; }
.state { font-size:10px; font-weight:700; letter-spacing:.12em; text-transform:uppercase;
         padding:2px 8px; border-radius:20px; float:right; }
.state.active { background:#e2f5ec; color:var(--ok); }
.state.revoked { background:#fdeae5; color:var(--coral); }
.state.expired { background:#f0f0f0; color:#999; }
.ttl { font-family:ui-monospace,monospace; font-size:11px; color:var(--cobalt); }
.transit { margin-left:18px; border-left:4px solid var(--cobalt); }
#stamps .r { font-family:ui-monospace,monospace; font-size:10.5px; color:#555;
             border-bottom:1px dashed #e2e2e2; padding:6px 2px; }
#stamps .r b { color:var(--ink); }
#stamps .r.denied-read b, #stamps .r.revoked b { color:var(--coral); }
.empty { color:#aab; font-size:12px; padding:6px 0 10px; }
footer { font-size:10px; color:#99a; padding:14px 18px 20px; }
</style></head><body>
<h1>Agent Visa · Holder Console</h1>
<div id="phone">
  <div id="card"><div class="chip"></div>
    <div class="kicker">AI Passport · Holder</div>
    <div class="name">ABHISHEK</div>
    <div class="serial">PASSPORT STAYS HOME · VISAS TRAVEL</div>
  </div>
  <section><h2>Requests</h2><div id="requests"></div></section>
  <section><h2>Passes &amp; Visas</h2><div id="visas"></div></section>
  <section><h2>Suggested Writes</h2><div id="suggestions"></div></section>
  <section><h2>Receipts — every read leaves a stamp</h2><div id="stamps"></div></section>
  <footer>Reference implementation of Egoist's published pass model — built on their public docs.
  Not affiliated with Egoist Machines, Inc.</footer>
</div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const post=p=>fetch(p,{method:'POST'});
let now=Date.now()/1000;
function ttl(v){ if(v.state!=='active') return v.state;
  if(v.expires==null) return v.duration==='once'?'single read':'no expiry';
  const s=Math.max(0,Math.floor(v.expires-now));
  return `expires ${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`; }
function render(d){ now=d.now;
  $('requests').innerHTML=d.requests.map(r=>`<div class="req">
    <span class="app">${esc(r.app)}</span> <span class="meta">wants ${r.duration}</span>
    <div class="purpose">“${esc(r.purpose)}”</div>
    <div class="fields">${r.fields.map(f=>`<span class="f">${esc(f)}</span>`).join('')}</div>
    <div class="row"><button class="approve" onclick="post('/api/approve/${r.id}')">Approve</button>
    <button class="denyb" onclick="post('/api/deny/${r.id}')">Deny</button></div>
  </div>`).join('')||'<div class="empty">no pending requests</div>';
  $('visas').innerHTML=d.visas.map(v=>`<div class="visa ${v.parent_id?'transit':''}">
    <span class="state ${v.state}">${v.state}</span>
    <span class="app">${esc(v.holder)}</span>
    <span class="meta">${v.parent_id?'transit visa · parent '+v.parent_id:'pass'} · ${v.id}</span>
    <div class="fields">${v.fields.map(f=>`<span class="f">${esc(f)}</span>`).join('')}</div>
    <div class="meta">reads: ${v.used} · <span class="ttl">${ttl(v)}</span></div>
    ${v.state==='active'?`<div class="row"><button class="revoke" onclick="post('/api/revoke/${v.id}')">Revoke${v.parent_id?'':' (cascades)'}</button></div>`:''}
  </div>`).join('')||'<div class="empty">nothing issued yet</div>';
  $('suggestions').innerHTML=d.suggestions.map(s=>`<div class="sugg">
    <span class="f">${esc(s.path)}</span> → “${esc(s.value)}”
    <div class="purpose">${esc(s.reason)}</div>
    <div class="row"><button class="approve" onclick="post('/api/suggestion/${s.id}/accept')">Accept</button>
    <button class="rej" onclick="post('/api/suggestion/${s.id}/reject')">Reject</button></div>
  </div>`).join('')||'<div class="empty">inbox empty — agents suggest, you decide</div>';
  $('stamps').innerHTML=d.receipts.map(r=>`<div class="r ${r.action}">
    <b>${esc(r.action)}</b> · ${esc(r.actor)} — ${esc(r.detail)}</div>`).join('')
    ||'<div class="empty">no stamps yet</div>'; }
new EventSource('/events').onmessage=e=>render(JSON.parse(e.data));
setInterval(()=>{document.querySelectorAll('.ttl').forEach(()=>{});now+=1;},1000);
</script></body></html>"""


@app.get("/")
def index():
    return HTMLResponse(PAGE)


if __name__ == "__main__":
    store.init()
    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="warning")
