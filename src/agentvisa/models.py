"""The Agent Visa vocabulary as types: the passport, its grants, the ledger, the error.

Every value that crosses a module boundary is one of these. Statuses are enums rather
than strings so an illegal state cannot be written down, and the domain objects are
frozen so a decision made in policy.py cannot be edited by the adapter that acts on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Duration(StrEnum):
    """How long a pass may be used: one read, a working session, or until revoked."""

    ONCE = "once"
    SESSION = "session"
    PERSISTENT = "persistent"


class GrantState(StrEnum):
    """The live state of a grant; only ACTIVE may be used. SPENT is a used once."""

    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    SPENT = "spent"


class RequestStatus(StrEnum):
    """Where a request sits in the holder's inbox."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"


class SuggestionStatus(StrEnum):
    """Where a proposed memory write sits in the holder's inbox."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ReceiptAction(StrEnum):
    """What a ledger line records; special category reads are stamped apart."""

    REQUEST = "request"
    PASS_ISSUED = "pass_issued"
    VISA_ISSUED = "visa_issued"
    READ = "read"
    SENSITIVE_READ = "sensitive_read"
    REFUSED = "refused"
    REVOKED = "revoked"
    SUGGESTION = "suggestion"
    PUBLIC_READ = "public_read"


class ErrorCode(StrEnum):
    """The closed set of refusals; every VisaError carries exactly one of these."""

    UNKNOWN_FIELDS = "UNKNOWN_FIELDS"
    EMPTY_REQUEST = "EMPTY_REQUEST"
    BAD_DURATION = "BAD_DURATION"
    MIXED_SENSITIVE = "MIXED_SENSITIVE"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_DECIDED = "ALREADY_DECIDED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"
    SPENT = "SPENT"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    NOT_SUBSET = "NOT_SUBSET"
    DEPTH_LIMIT = "DEPTH_LIMIT"
    NO_SENSITIVE_DELEGATION = "NO_SENSITIVE_DELEGATION"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"


class VisaError(Exception):
    """A refusal carrying a code from the closed set and a message for the holder."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class PassportField:
    """One dot-path entry of the passport; attested fields yield proof, never value."""

    path: str
    value: str
    attested: bool = False
    sensitive: bool = False


@dataclass(frozen=True, slots=True)
class Grant:
    """A pass (parent_id is None) or a visa cut from it: the unit of authorization."""

    id: str
    parent_id: str | None
    holder: str
    fields: tuple[str, ...]
    purpose: str
    duration: Duration
    issued_at: float
    expires_at: float | None
    reads: int = 0
    revoked: bool = False
    request_id: str | None = None

    @property
    def is_transit(self) -> bool:
        """True for a transit visa, which may not delegate any further."""
        return self.parent_id is not None


@dataclass(frozen=True, slots=True)
class ContextRequest:
    """An app asking the holder for exact fields, with a purpose and a duration."""

    id: str
    app: str
    fields: tuple[str, ...]
    purpose: str
    duration: Duration
    status: RequestStatus
    created_at: float


@dataclass(frozen=True, slots=True)
class Receipt:
    """One immutable ledger line, written in the same transaction as its effect."""

    id: int
    grant_id: str | None
    actor: str
    action: ReceiptAction
    detail: str
    created_at: float


@dataclass(frozen=True, slots=True)
class Suggestion:
    """An agent's proposed memory write, queued for the holder, never applied by it."""

    id: int
    grant_id: str
    path: str
    value: str
    reason: str
    status: SuggestionStatus
    created_at: float
