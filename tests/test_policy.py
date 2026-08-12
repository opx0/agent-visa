"""The security model, written as tests: pure, instant, time travel by arithmetic."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from agentvisa.models import (
    Duration,
    ErrorCode,
    Grant,
    GrantState,
    PassportField,
    ReceiptAction,
    VisaError,
)
from agentvisa.policy import (
    MAX_VISA_TTL,
    SESSION_TTL,
    authorize_delegation,
    authorize_read,
    grant_expiry,
    project,
    read_receipt_action,
    resolve_state,
    validate_request,
)

T0 = 1_700_000_000.0
BUDGET_VALUE = "4200 GBP, no single item over 900"

TASTE = PassportField("taste.summary", "warm minimalism, cobalt and coral")
SIZES = PassportField("profile.sizes", "M tops, 42 EU shoes")
BUDGET = PassportField("commerce.budget", BUDGET_VALUE, attested=True)
HEART = PassportField("health.resting_hr", "58 bpm", sensitive=True)
PASSPORT = {field.path: field for field in (TASTE, SIZES, BUDGET, HEART)}


def a_pass(
    fields: tuple[str, ...] = ("taste.summary", "commerce.budget"),
    duration: Duration = Duration.SESSION,
    expires_at: float | None = T0 + SESSION_TTL,
    reads: int = 0,
    revoked: bool = False,
    parent_id: str | None = None,
) -> Grant:
    return Grant(
        id="pass_1",
        parent_id=parent_id,
        holder="gift-agent",
        fields=fields,
        purpose="choose a birthday gift",
        duration=duration,
        issued_at=T0,
        expires_at=expires_at,
        reads=reads,
        revoked=revoked,
        request_id="req_1",
    )


@contextmanager
def refuses(code: ErrorCode) -> Iterator[None]:
    with pytest.raises(VisaError) as caught:
        yield
    assert caught.value.code is code, f"expected {code}, got {caught.value.code}"


# (a) a grant returns only the fields it was granted


def test_read_returns_exactly_the_fields_asked_for_within_scope() -> None:
    assert authorize_read(a_pass(), ["taste.summary"], T0) == ["taste.summary"]


def test_read_of_a_field_outside_the_grant_is_refused() -> None:
    with refuses(ErrorCode.OUT_OF_SCOPE):
        authorize_read(a_pass(fields=("taste.summary",)), ["commerce.budget"], T0)


def test_read_without_a_field_list_returns_the_whole_granted_set() -> None:
    assert authorize_read(a_pass(), None, T0) == ["taste.summary", "commerce.budget"]


# (b) an attested field returns proof, never the value


def test_attested_field_is_projected_as_proof_and_the_value_never_appears() -> None:
    context = project([TASTE, BUDGET])
    assert context == {"taste.summary": TASTE.value, "commerce.approved": True}
    assert BUDGET_VALUE not in repr(context)


def test_attested_field_without_a_prefix_still_hides_its_value() -> None:
    assert project([PassportField("budget", BUDGET_VALUE, attested=True)]) == {"approved": True}


# (c) once is spent by one read, a session expires at its TTL, persistent has no clock


def test_once_grant_is_spent_after_a_single_read() -> None:
    spent = a_pass(duration=Duration.ONCE, expires_at=None, reads=1)
    assert resolve_state(spent, T0) is GrantState.SPENT
    with refuses(ErrorCode.SPENT):
        authorize_read(spent, None, T0)


def test_session_grant_dies_at_its_ttl_and_not_a_second_before() -> None:
    session = a_pass()
    assert resolve_state(session, T0 + SESSION_TTL - 1) is GrantState.ACTIVE
    with refuses(ErrorCode.EXPIRED):
        authorize_read(session, None, T0 + SESSION_TTL)


def test_persistent_grant_has_no_expiry_and_survives_far_into_the_future() -> None:
    assert grant_expiry(Duration.PERSISTENT, T0) is None
    assert grant_expiry(Duration.ONCE, T0) is None
    assert grant_expiry(Duration.SESSION, T0) == T0 + SESSION_TTL
    forever = a_pass(duration=Duration.PERSISTENT, expires_at=None, reads=9)
    assert resolve_state(forever, T0 + 10 * SESSION_TTL) is GrantState.ACTIVE


# (d) a transit visa is a subset, shorter-lived, and cannot delegate again


def test_visa_carries_a_subset_of_the_parent_and_the_capped_ttl() -> None:
    terms = authorize_delegation(
        a_pass(duration=Duration.PERSISTENT, expires_at=None),
        ["taste.summary"],
        MAX_VISA_TTL * 100,
        T0,
        PASSPORT,
    )
    assert terms.fields == ("taste.summary",)
    assert terms.expires_at == T0 + MAX_VISA_TTL


def test_visa_cannot_widen_scope_beyond_its_parent() -> None:
    with refuses(ErrorCode.NOT_SUBSET):
        authorize_delegation(
            a_pass(fields=("taste.summary",)),
            ["taste.summary", "profile.sizes"],
            60,
            T0,
            PASSPORT,
        )


def test_visa_cannot_outlive_its_parent() -> None:
    parent = a_pass(expires_at=T0 + 60)
    terms = authorize_delegation(parent, ["taste.summary"], MAX_VISA_TTL, T0, PASSPORT)
    assert terms.expires_at == T0 + 60


def test_visa_cut_from_a_once_pass_is_itself_good_for_one_read() -> None:
    parent = a_pass(duration=Duration.ONCE, expires_at=None)
    terms = authorize_delegation(parent, ["taste.summary"], MAX_VISA_TTL, T0, PASSPORT)
    assert terms.duration is Duration.ONCE


def test_visa_cannot_issue_a_further_visa() -> None:
    visa = a_pass(parent_id="pass_1", fields=("taste.summary",))
    with refuses(ErrorCode.DEPTH_LIMIT):
        authorize_delegation(visa, ["taste.summary"], 60, T0, PASSPORT)


def test_expired_visa_stops_reading() -> None:
    visa = a_pass(parent_id="pass_1", fields=("taste.summary",), expires_at=T0 + MAX_VISA_TTL)
    with refuses(ErrorCode.EXPIRED):
        authorize_read(visa, ["taste.summary"], T0 + MAX_VISA_TTL + 1)


# (e) revocation is immediate; the store cascades the flag from a pass to its visas


def test_revoked_grant_can_neither_read_nor_delegate() -> None:
    dead = a_pass(revoked=True)
    assert resolve_state(dead, T0) is GrantState.REVOKED
    with refuses(ErrorCode.REVOKED):
        authorize_read(dead, None, T0)
    with refuses(ErrorCode.REVOKED):
        authorize_delegation(dead, ["taste.summary"], 60, T0, PASSPORT)


# (f) special categories need their own pass, never ride along and never travel


def test_request_mixing_a_special_category_with_ordinary_fields_is_refused() -> None:
    with refuses(ErrorCode.MIXED_SENSITIVE):
        validate_request(["taste.summary", "health.resting_hr"], PASSPORT, "session")


def test_request_for_a_special_category_alone_is_allowed() -> None:
    fields, duration = validate_request(["health.resting_hr"], PASSPORT, "once")
    assert fields == ["health.resting_hr"]
    assert duration is Duration.ONCE


def test_special_category_never_travels_on_a_visa() -> None:
    parent = a_pass(fields=("health.resting_hr",))
    with refuses(ErrorCode.NO_SENSITIVE_DELEGATION):
        authorize_delegation(parent, ["health.resting_hr"], 60, T0, PASSPORT)


def test_read_of_a_special_category_is_stamped_apart_in_the_ledger() -> None:
    assert read_receipt_action([TASTE, BUDGET]) is ReceiptAction.READ
    assert read_receipt_action([HEART]) is ReceiptAction.SENSITIVE_READ


# adversarial input to validate_request and authorize_delegation


def test_duplicate_fields_are_collapsed_in_the_order_first_asked() -> None:
    fields, _ = validate_request(
        ["profile.sizes", "taste.summary", "profile.sizes"], PASSPORT, "session"
    )
    assert fields == ["profile.sizes", "taste.summary"]


def test_empty_request_is_refused() -> None:
    with refuses(ErrorCode.EMPTY_REQUEST):
        validate_request([], PASSPORT, "session")
    with refuses(ErrorCode.EMPTY_REQUEST):
        authorize_delegation(a_pass(), [], 60, T0, PASSPORT)


def test_unknown_path_is_refused_before_anything_is_granted() -> None:
    with refuses(ErrorCode.UNKNOWN_FIELDS):
        validate_request(["taste.summary", "bank.iban"], PASSPORT, "session")


def test_unrecognised_duration_is_refused() -> None:
    with refuses(ErrorCode.BAD_DURATION):
        validate_request(["taste.summary"], PASSPORT, "forever")


def test_visa_with_a_non_positive_ttl_is_refused() -> None:
    with refuses(ErrorCode.BAD_DURATION):
        authorize_delegation(a_pass(), ["taste.summary"], 0, T0, PASSPORT)
