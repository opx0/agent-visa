"""SQLite store for Agent Visa — scaffold until ego.ist/connect opens."""
import json
import sqlite3
import time
import uuid
from pathlib import Path

DB = Path(__file__).parent / "visa.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS passport (
  path TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  attest INTEGER NOT NULL DEFAULT 0,   -- 1: share only proof-of-fact, never the value
  sensitive INTEGER NOT NULL DEFAULT 0 -- 1: special category (Privacy Policy §4), own pass required
);
CREATE TABLE IF NOT EXISTS requests (
  id TEXT PRIMARY KEY,
  app TEXT NOT NULL,
  fields TEXT NOT NULL,          -- JSON list of dot-paths
  purpose TEXT NOT NULL,
  duration TEXT NOT NULL,        -- once | session | persistent
  status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | denied
  ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS visas (
  id TEXT PRIMARY KEY,
  parent_id TEXT,                -- NULL: pass (root grant). Set: transit visa
  holder TEXT NOT NULL,
  fields TEXT NOT NULL,          -- JSON list, always subset of parent for visas
  purpose TEXT NOT NULL,
  duration TEXT NOT NULL,
  expires REAL,                  -- NULL: persistent
  revoked INTEGER NOT NULL DEFAULT 0,
  used INTEGER NOT NULL DEFAULT 0, -- read count (duration=once expires after 1)
  request_id TEXT,
  ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  visa_id TEXT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL,
  ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS suggestions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  visa_id TEXT,
  path TEXT NOT NULL,
  value TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | rejected
  ts REAL NOT NULL
);
"""

SEED = [
    # path, value, attest, sensitive
    ("taste.summary", "warm minimalism; cobalt+coral; analog photography; handmade over mass-made", 0, 0),
    ("style.preferences", "classic silhouettes, natural fabrics, no logos", 0, 0),
    ("profile.sizes", "M tops, 42 EU shoes", 0, 0),
    ("commerce.budget", "180", 1, 0),          # attested: agents learn only budget.approved=true
    ("saved_facts.dietary", "vegetarian", 0, 0),
    ("availability.weekends", "free after 2pm", 0, 0),
    ("health.resting_hr", "58", 0, 1),          # special category — never rides along
]


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def init(reset: bool = False) -> None:
    if reset and DB.exists():
        DB.unlink()
    con = connect()
    with con:
        con.executemany(
            "INSERT OR IGNORE INTO passport(path, value, attest, sensitive) VALUES (?,?,?,?)",
            SEED,
        )
    con.close()


def receipt(con, visa_id, actor, action, detail):
    with con:
        con.execute(
            "INSERT INTO receipts(visa_id, actor, action, detail, ts) VALUES (?,?,?,?,?)",
            (visa_id, actor, action, detail, time.time()),
        )


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def visa_state(row) -> str:
    """Live state of a pass/visa row."""
    if row["revoked"]:
        return "revoked"
    if row["duration"] == "once" and row["used"] >= 1:
        return "expired"
    if row["expires"] is not None and time.time() > row["expires"]:
        return "expired"
    return "active"


def fields_of(row) -> list:
    return json.loads(row["fields"])


if __name__ == "__main__":
    init(reset=True)
    print(f"initialized {DB}")
