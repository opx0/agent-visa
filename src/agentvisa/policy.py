"""Every authorization decision in Agent Visa, as pure functions.

Read this one file and you know the whole security model. Nothing here touches the
database, the clock or the log: the caller passes the current time as a unix
timestamp and the passport as a mapping, and gets a decision or a VisaError back.

    resolve_state(grant, now)                    -> GrantState
    require_active(grant, now)                   -> None
    validate_request(requested, known, duration) -> (list[str], Duration)
    authorize_read(grant, requested, now)        -> list[str]
    authorize_delegation(parent, subset, ttl_seconds, now, fields)
                                                 -> DelegationTerms
    project(fields)                              -> dict[str, object]
    grant_expiry(duration, issued_at)            -> float | None
    read_receipt_action(fields)                  -> ReceiptAction
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from agentvisa.models import (
    Duration,
    ErrorCode,
    Grant,
    GrantState,
    PassportField,
    ReceiptAction,
    VisaError,
)

# A sub-agent errand is short; anything longer should be its own pass, approved
# by the holder rather than handed down by another agent.
MAX_VISA_TTL = 600

# One working session, after which the holder is asked again instead of drifting
# into consent by default.
SESSION_TTL = 3600

_REFUSALS: dict[GrantState, tuple[ErrorCode, str]] = {
    GrantState.REVOKED: (
        ErrorCode.REVOKED,
        "this grant was revoked by the passport holder",
    ),
    GrantState.EXPIRED: (ErrorCode.EXPIRED, "this grant has expired"),
    GrantState.SPENT: (
        ErrorCode.SPENT,
        "this grant was good for a single read and has been used",
    ),
}


@dataclass(frozen=True, slots=True)
class DelegationTerms:
    """Terms a transit visa may be issued on: never wider or freer than its parent."""

    fields: tuple[str, ...]
    expires_at: float
    duration: Duration


def resolve_state(grant: Grant, now: float) -> GrantState:
    """The live state of a grant at `now`: revocation beats expiry beats spend."""
    if grant.revoked:
        return GrantState.REVOKED
    if grant.expires_at is not None and now >= grant.expires_at:
        return GrantState.EXPIRED
    if grant.duration is Duration.ONCE and grant.reads >= 1:
        return GrantState.SPENT
    return GrantState.ACTIVE


def require_active(grant: Grant, now: float) -> None:
    """Return only if the grant may be used at `now`; otherwise raise its refusal."""
    state = resolve_state(grant, now)
    if state is GrantState.ACTIVE:
        return
    code, message = _REFUSALS[state]
    raise VisaError(code, message)


def validate_request(
    requested: Sequence[str],
    known: Mapping[str, PassportField],
    duration: str,
) -> tuple[list[str], Duration]:
    """Accept only if every field is known and no special category rides along."""
    fields = _unique(requested)
    if not fields:
        raise VisaError(ErrorCode.EMPTY_REQUEST, "a request must name at least one field")
    try:
        parsed = Duration(duration)
    except ValueError:
        raise VisaError(
            ErrorCode.BAD_DURATION, f"duration must be one of: {', '.join(Duration)}"
        ) from None
    unknown = [path for path in fields if path not in known]
    if unknown:
        raise VisaError(ErrorCode.UNKNOWN_FIELDS, f"not in this passport: {', '.join(unknown)}")
    sensitive = [path for path in fields if known[path].sensitive]
    if sensitive and len(sensitive) != len(fields):
        raise VisaError(
            ErrorCode.MIXED_SENSITIVE,
            f"a special category needs its own pass: {', '.join(sensitive)}",
        )
    return fields, parsed


def authorize_read(grant: Grant, requested: Sequence[str] | None, now: float) -> list[str]:
    """The fields this grant may read at `now`; asking for nothing asks for all."""
    require_active(grant, now)
    fields = _unique(requested) if requested else list(grant.fields)
    outside = [path for path in fields if path not in grant.fields]
    if outside:
        raise VisaError(ErrorCode.OUT_OF_SCOPE, f"this grant does not cover: {', '.join(outside)}")
    return fields


def authorize_delegation(
    parent: Grant,
    subset: Sequence[str],
    ttl_seconds: float,
    now: float,
    fields: Mapping[str, PassportField],
) -> DelegationTerms:
    """Terms for a transit visa: a subset of an active pass, capped at MAX_VISA_TTL,
    never outliving its parent and never carrying a special category."""
    require_active(parent, now)
    if parent.is_transit:
        raise VisaError(ErrorCode.DEPTH_LIMIT, "a transit visa cannot issue a further visa")
    wanted = _unique(subset)
    if not wanted:
        raise VisaError(ErrorCode.EMPTY_REQUEST, "a visa must name at least one field")
    outside = [path for path in wanted if path not in parent.fields]
    if outside:
        raise VisaError(
            ErrorCode.NOT_SUBSET,
            f"the parent pass does not cover: {', '.join(outside)}",
        )
    unknown = [path for path in wanted if path not in fields]
    if unknown:
        raise VisaError(ErrorCode.UNKNOWN_FIELDS, f"not in this passport: {', '.join(unknown)}")
    special = [path for path in wanted if fields[path].sensitive]
    if special:
        raise VisaError(
            ErrorCode.NO_SENSITIVE_DELEGATION,
            f"a special category never travels on a visa: {', '.join(special)}",
        )
    if ttl_seconds <= 0:
        raise VisaError(ErrorCode.BAD_DURATION, "ttl_seconds must be positive")
    expires_at = now + min(float(ttl_seconds), MAX_VISA_TTL)
    if parent.expires_at is not None:
        expires_at = min(expires_at, parent.expires_at)
    # A once pass carries one read in total, so a visa cut from it must not multiply it.
    duration = Duration.ONCE if parent.duration is Duration.ONCE else Duration.SESSION
    return DelegationTerms(tuple(wanted), expires_at, duration)


def project(fields: Sequence[PassportField]) -> dict[str, object]:
    """The only route a value takes to an agent: attested yields proof, never value."""
    context: dict[str, object] = {}
    for field in fields:
        if not field.attested:
            context[field.path] = field.value
            continue
        prefix, _, _ = field.path.rpartition(".")
        context[f"{prefix}.approved" if prefix else "approved"] = True
    return context


def grant_expiry(duration: Duration, issued_at: float) -> float | None:
    """When a fresh grant stops working; None for once (dies on use) and persistent."""
    return issued_at + SESSION_TTL if duration is Duration.SESSION else None


def read_receipt_action(fields: Sequence[PassportField]) -> ReceiptAction:
    """Special category reads are stamped apart (Privacy Policy section 4)."""
    if any(field.sensitive for field in fields):
        return ReceiptAction.SENSITIVE_READ
    return ReceiptAction.READ


def _unique(paths: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(paths))
