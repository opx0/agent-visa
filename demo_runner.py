# /// script
# requires-python = ">=3.11"
# dependencies = ["fastmcp>=2", "httpx"]
# ///
"""gift-runner — the sub-agent pane for the split-screen demo.

Reads with its transit visa every 3s until the holder revokes. Then it dies,
on camera. Run: uv run demo_runner.py <visa_id>"""
import sys
import time

import server

visa_id = sys.argv[1]
print(f"gift-runner online · visa {visa_id} · reading every 3s\n")
while True:
    r = server.read_context(visa_id)
    ts = time.strftime("%H:%M:%S")
    if r.get("ok"):
        print(f"[{ts}] read ok → {r['context']}")
    else:
        print(f"\n[{ts}] ✗ {r['error']} — {r['message']}")
        if r["error"] in ("VISA_REVOKED", "VISA_EXPIRED"):
            print("gift-runner terminated. The holder said no. That's the product.")
            sys.exit(1)
    time.sleep(3)
