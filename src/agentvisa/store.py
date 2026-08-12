"""The SQLite repository: the only module in Agent Visa that touches a database.

The store records what policy has already permitted; it makes no authorization decisions.
Every mutation writes its receipt inside the same transaction as the effect it records, so
a read that reaches an agent without a ledger line is not a state this code can reach.
Callers own the clock: each mutation takes the current time as an explicit argument.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from agentvisa import policy
from agentvisa.models import (
    ContextRequest,
    Duration,
    ErrorCode,
    Grant,
    PassportField,
    Receipt,
    ReceiptAction,
    RequestStatus,
    Suggestion,
    SuggestionStatus,
    VisaError,
)

HOLDER = "holder"

# The MCP server and the console are two processes over one file, so both resolve it here.
DB_ENV = "AGENTVISA_DB"
DEFAULT_DB = "agentvisa.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS passport (
  path      TEXT PRIMARY KEY,
  value     TEXT    NOT NULL,
  attested  INTEGER NOT NULL DEFAULT 0,
  sensitive INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS requests (
  id         TEXT PRIMARY KEY,
  app        TEXT NOT NULL,
  fields     TEXT NOT NULL,
  purpose    TEXT NOT NULL,
  duration   TEXT NOT NULL,
  status     TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
  id         TEXT PRIMARY KEY,
  parent_id  TEXT REFERENCES grants(id),
  holder     TEXT NOT NULL,
  fields     TEXT NOT NULL,
  purpose    TEXT NOT NULL,
  duration   TEXT NOT NULL,
  issued_at  REAL NOT NULL,
  expires_at REAL,
  reads      INTEGER NOT NULL DEFAULT 0,
  revoked    INTEGER NOT NULL DEFAULT 0,
  request_id TEXT REFERENCES requests(id)
);
CREATE TABLE IF NOT EXISTS receipts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  grant_id   TEXT,
  actor      TEXT NOT NULL,
  action     TEXT NOT NULL,
  detail     TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS suggestions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  grant_id   TEXT NOT NULL REFERENCES grants(id),
  path       TEXT NOT NULL,
  value      TEXT NOT NULL,
  reason     TEXT NOT NULL,
  status     TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS grants_by_parent ON grants(parent_id);
"""

SEED: tuple[PassportField, ...] = (
    PassportField(
        "taste.summary",
        "Warm minimalism. Cobalt and coral. Film photography, handmade over mass-made.",
    ),
    PassportField(
        "style.preferences",
        "Classic silhouettes, natural fabrics, nothing with a visible logo.",
    ),
    PassportField("profile.sizes", "Medium tops, 32 waist, EU 42 shoes."),
    PassportField("commerce.budget", "180", attested=True),
    PassportField("saved_facts.dietary", "Vegetarian, and allergic to shellfish."),
    PassportField("availability.weekends", "Free after 2pm on Saturday and Sunday."),
    PassportField("health.resting_hr", "58 bpm", sensitive=True),
)


def database_path() -> Path:
    """The file the server and the console share; AGENTVISA_DB overrides the default."""
    return Path(os.environ.get(DB_ENV, DEFAULT_DB))


class Store:
    """The passport and its ledger on disk. Constructing one creates and seeds the schema."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(SCHEMA)
            con.executemany(
                "INSERT OR IGNORE INTO passport(path, value, attested, sensitive)"
                " VALUES (?, ?, ?, ?)",
                [(f.path, f.value, f.attested, f.sensitive) for f in SEED],
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside one transaction: committed on success, rolled back on error."""
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            with con:
                yield con
        finally:
            con.close()

    def fields(self) -> dict[str, PassportField]:
        """The whole passport by dot-path, in the order the holder sees it."""
        with self._connect() as con:
            rows = con.execute("SELECT * FROM passport ORDER BY rowid").fetchall()
        return {str(row["path"]): _field(row) for row in rows}

    def request(self, request_id: str) -> ContextRequest | None:
        """The request with this id, or None."""
        with self._connect() as con:
            row = con.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        return None if row is None else _request(row)

    def pending_requests(self) -> list[ContextRequest]:
        """Every request still waiting on the holder, oldest first."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM requests WHERE status = ? ORDER BY created_at, rowid",
                (RequestStatus.PENDING,),
            ).fetchall()
        return [_request(row) for row in rows]

    def grant(self, grant_id: str) -> Grant | None:
        """The pass or visa with this id, or None."""
        with self._connect() as con:
            row = con.execute("SELECT * FROM grants WHERE id = ?", (grant_id,)).fetchone()
        return None if row is None else _grant(row)

    def grant_for_request(self, request_id: str) -> Grant | None:
        """The pass issued when this request was approved, or None while it is undecided."""
        with self._connect() as con:
            row = con.execute("SELECT * FROM grants WHERE request_id = ?", (request_id,)).fetchone()
        return None if row is None else _grant(row)

    def grants(self) -> list[Grant]:
        """Every pass and visa ever issued, newest first."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM grants ORDER BY issued_at DESC, rowid DESC"
            ).fetchall()
        return [_grant(row) for row in rows]

    def receipts(self, limit: int = 50) -> list[Receipt]:
        """The most recent ledger lines, newest first."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM receipts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_receipt(row) for row in rows]

    def suggestions(self) -> list[Suggestion]:
        """Every proposed memory write, newest first."""
        with self._connect() as con:
            rows = con.execute("SELECT * FROM suggestions ORDER BY id DESC").fetchall()
        return [_suggestion(row) for row in rows]

    def create_request(
        self,
        app: str,
        fields: Sequence[str],
        purpose: str,
        duration: Duration,
        now: float,
    ) -> ContextRequest:
        """Queue a validated request for the holder and stamp it in the ledger."""
        record = ContextRequest(
            id=_new_id("req"),
            app=app,
            fields=tuple(fields),
            purpose=purpose,
            duration=duration,
            status=RequestStatus.PENDING,
            created_at=now,
        )
        with self._connect() as con:
            con.execute(
                "INSERT INTO requests(id, app, fields, purpose, duration, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    record.app,
                    json.dumps(list(record.fields)),
                    record.purpose,
                    record.duration,
                    record.status,
                    record.created_at,
                ),
            )
            _write_receipt(
                con,
                None,
                app,
                ReceiptAction.REQUEST,
                f"{app} asked for {', '.join(record.fields)} for {purpose} ({duration})",
                now,
            )
        return record

    def approve_request(self, request_id: str, now: float) -> Grant:
        """Turn a pending request into a pass on the terms the app asked for."""
        with self._connect() as con:
            record = _decide(con, request_id, RequestStatus.APPROVED)
            issued = Grant(
                id=_new_id("pass"),
                parent_id=None,
                holder=record.app,
                fields=record.fields,
                purpose=record.purpose,
                duration=record.duration,
                issued_at=now,
                expires_at=policy.grant_expiry(record.duration, now),
                request_id=record.id,
            )
            _insert_grant(con, issued)
            _write_receipt(
                con,
                issued.id,
                HOLDER,
                ReceiptAction.PASS_ISSUED,
                f"approved {record.app} for {', '.join(issued.fields)} ({issued.duration})",
                now,
            )
        return issued

    def deny_request(self, request_id: str, now: float) -> ContextRequest:
        """Refuse a pending request; no pass is ever created for it."""
        with self._connect() as con:
            record = _decide(con, request_id, RequestStatus.DENIED)
            _write_receipt(
                con,
                None,
                HOLDER,
                ReceiptAction.REFUSED,
                f"denied {record.app}: {', '.join(record.fields)}",
                now,
            )
        return record

    def record_read(self, grant: Grant, fields: Sequence[PassportField], now: float) -> None:
        """Count a read against its grant and stamp it, special categories marked apart."""
        paths = [field.path for field in fields]
        with self._connect() as con:
            con.execute("UPDATE grants SET reads = reads + 1 WHERE id = ?", (grant.id,))
            _write_receipt(
                con,
                grant.id,
                grant.holder,
                policy.read_receipt_action(fields),
                f"{grant.holder} read {', '.join(paths)}",
                now,
            )

    def create_visa(
        self,
        parent: Grant,
        terms: policy.DelegationTerms,
        holder: str,
        now: float,
    ) -> Grant:
        """Issue a transit visa on terms policy has already narrowed to fit its parent."""
        visa = Grant(
            id=_new_id("visa"),
            parent_id=parent.id,
            holder=holder,
            fields=terms.fields,
            purpose=parent.purpose,
            duration=terms.duration,
            issued_at=now,
            expires_at=terms.expires_at,
            request_id=parent.request_id,
        )
        with self._connect() as con:
            _insert_grant(con, visa)
            _write_receipt(
                con,
                visa.id,
                holder,
                ReceiptAction.VISA_ISSUED,
                f"{parent.holder} sent {holder} on {', '.join(visa.fields)}"
                f" for {terms.expires_at - now:.0f}s",
                now,
            )
        return visa

    def revoke(self, grant_id: str, now: float) -> int:
        """Revoke a grant and every visa cut from it; returns how many were still live."""
        with self._connect() as con:
            if con.execute("SELECT 1 FROM grants WHERE id = ?", (grant_id,)).fetchone() is None:
                raise VisaError(ErrorCode.NOT_FOUND, f"no grant with id {grant_id}")
            # Delegation is capped at one hop, so a grant and its children are the whole tree.
            affected = con.execute(
                "UPDATE grants SET revoked = 1 WHERE revoked = 0 AND (id = ? OR parent_id = ?)",
                (grant_id, grant_id),
            ).rowcount
            _write_receipt(
                con,
                grant_id,
                HOLDER,
                ReceiptAction.REVOKED,
                f"revoked {affected} live grant(s) in the cascade from {grant_id}",
                now,
            )
        return affected

    def queue_suggestion(
        self, grant: Grant, path: str, value: str, reason: str, now: float
    ) -> Suggestion:
        """Park an agent's proposed write in the holder's inbox; the passport is untouched."""
        with self._connect() as con:
            cursor = con.execute(
                "INSERT INTO suggestions(grant_id, path, value, reason, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (grant.id, path, value, reason, SuggestionStatus.PENDING, now),
            )
            suggestion_id = int(cursor.lastrowid or 0)
            _write_receipt(
                con,
                grant.id,
                grant.holder,
                ReceiptAction.SUGGESTION,
                f"{grant.holder} proposed {path} = {value!r} because {reason}",
                now,
            )
        return Suggestion(
            id=suggestion_id,
            grant_id=grant.id,
            path=path,
            value=value,
            reason=reason,
            status=SuggestionStatus.PENDING,
            created_at=now,
        )

    def resolve_suggestion(self, suggestion_id: int, accept: bool, now: float) -> Suggestion:
        """Settle a proposed write; only the holder accepting it ever changes the passport."""
        status = SuggestionStatus.ACCEPTED if accept else SuggestionStatus.REJECTED
        with self._connect() as con:
            row = con.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
            if row is None:
                raise VisaError(ErrorCode.NOT_FOUND, f"no suggestion with id {suggestion_id}")
            record = _suggestion(row)
            if record.status is not SuggestionStatus.PENDING:
                raise VisaError(
                    ErrorCode.ALREADY_DECIDED,
                    f"suggestion {suggestion_id} was already {record.status}",
                )
            con.execute("UPDATE suggestions SET status = ? WHERE id = ?", (status, suggestion_id))
            if accept:
                # A new path arrives ordinary; an existing one keeps the flags the holder set.
                con.execute(
                    "INSERT INTO passport(path, value) VALUES (?, ?)"
                    " ON CONFLICT(path) DO UPDATE SET value = excluded.value",
                    (record.path, record.value),
                )
            _write_receipt(
                con,
                record.grant_id,
                HOLDER,
                ReceiptAction.SUGGESTION,
                f"{status} {record.path} = {record.value!r}",
                now,
            )
        return replace(record, status=status)

    def log_refusal(self, grant_id: str | None, actor: str, error: VisaError, now: float) -> None:
        """Stamp an attempt that policy turned down, so refusals are as visible as reads."""
        with self._connect() as con:
            _write_receipt(
                con, grant_id, actor, ReceiptAction.REFUSED, f"{error.code}: {error.message}", now
            )

    def log_public_read(self, username: str, actor: str, now: float) -> None:
        """Stamp a live read of somebody's published ego.ist passport."""
        with self._connect() as con:
            _write_receipt(
                con,
                None,
                actor,
                ReceiptAction.PUBLIC_READ,
                f"read the public passport of {username} from ego.ist",
                now,
            )


def _decide(con: sqlite3.Connection, request_id: str, status: RequestStatus) -> ContextRequest:
    """Move a request out of pending exactly once, inside the caller's transaction."""
    row = con.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    if row is None:
        raise VisaError(ErrorCode.NOT_FOUND, f"no request with id {request_id}")
    record = _request(row)
    if record.status is not RequestStatus.PENDING:
        raise VisaError(
            ErrorCode.ALREADY_DECIDED, f"request {request_id} was already {record.status}"
        )
    con.execute("UPDATE requests SET status = ? WHERE id = ?", (status, request_id))
    return record


def _insert_grant(con: sqlite3.Connection, grant: Grant) -> None:
    con.execute(
        "INSERT INTO grants(id, parent_id, holder, fields, purpose, duration, issued_at,"
        " expires_at, reads, revoked, request_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            grant.id,
            grant.parent_id,
            grant.holder,
            json.dumps(list(grant.fields)),
            grant.purpose,
            grant.duration,
            grant.issued_at,
            grant.expires_at,
            grant.reads,
            grant.revoked,
            grant.request_id,
        ),
    )


def _write_receipt(
    con: sqlite3.Connection,
    grant_id: str | None,
    actor: str,
    action: ReceiptAction,
    detail: str,
    now: float,
) -> None:
    con.execute(
        "INSERT INTO receipts(grant_id, actor, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (grant_id, actor, action, detail, now),
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _paths(raw: object) -> tuple[str, ...]:
    return tuple(str(path) for path in json.loads(str(raw)))


def _field(row: sqlite3.Row) -> PassportField:
    return PassportField(
        path=str(row["path"]),
        value=str(row["value"]),
        attested=bool(row["attested"]),
        sensitive=bool(row["sensitive"]),
    )


def _request(row: sqlite3.Row) -> ContextRequest:
    return ContextRequest(
        id=str(row["id"]),
        app=str(row["app"]),
        fields=_paths(row["fields"]),
        purpose=str(row["purpose"]),
        duration=Duration(row["duration"]),
        status=RequestStatus(row["status"]),
        created_at=float(row["created_at"]),
    )


def _grant(row: sqlite3.Row) -> Grant:
    expires_at = row["expires_at"]
    return Grant(
        id=str(row["id"]),
        parent_id=None if row["parent_id"] is None else str(row["parent_id"]),
        holder=str(row["holder"]),
        fields=_paths(row["fields"]),
        purpose=str(row["purpose"]),
        duration=Duration(row["duration"]),
        issued_at=float(row["issued_at"]),
        expires_at=None if expires_at is None else float(expires_at),
        reads=int(row["reads"]),
        revoked=bool(row["revoked"]),
        request_id=None if row["request_id"] is None else str(row["request_id"]),
    )


def _receipt(row: sqlite3.Row) -> Receipt:
    return Receipt(
        id=int(row["id"]),
        grant_id=None if row["grant_id"] is None else str(row["grant_id"]),
        actor=str(row["actor"]),
        action=ReceiptAction(row["action"]),
        detail=str(row["detail"]),
        created_at=float(row["created_at"]),
    )


def _suggestion(row: sqlite3.Row) -> Suggestion:
    return Suggestion(
        id=int(row["id"]),
        grant_id=str(row["grant_id"]),
        path=str(row["path"]),
        value=str(row["value"]),
        reason=str(row["reason"]),
        status=SuggestionStatus(row["status"]),
        created_at=float(row["created_at"]),
    )
