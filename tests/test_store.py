"""Repository behaviour against a temporary database: seeding, receipts, cascade, review."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentvisa import policy
from agentvisa.models import (
    Duration,
    ErrorCode,
    Grant,
    ReceiptAction,
    RequestStatus,
    SuggestionStatus,
    VisaError,
)
from agentvisa.store import Store

NOW = 1_770_000_000.0


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "visa.db")


def approved(store: Store, *fields: str, duration: Duration = Duration.SESSION) -> Grant:
    """A pass over the named fields, the shortest route from empty database to a grant."""
    request = store.create_request("gift-agent", fields, "choose a gift", duration, NOW)
    return store.approve_request(request.id, NOW)


def live(store: Store, grant_id: str) -> Grant:
    """The stored grant, asserting it exists so the assertions below read plainly."""
    stored = store.grant(grant_id)
    assert stored is not None
    return stored


def test_seeding_is_idempotent_and_keeps_holder_edits(tmp_path: Path) -> None:
    first = Store(tmp_path / "visa.db")
    assert set(first.fields()) == {
        "taste.summary",
        "style.preferences",
        "profile.sizes",
        "commerce.budget",
        "saved_facts.dietary",
        "availability.weekends",
        "health.resting_hr",
    }

    grant = approved(first, "taste.summary")
    edit = first.queue_suggestion(grant, "taste.summary", "Brutalist now.", "she said so", NOW)
    first.resolve_suggestion(edit.id, accept=True, now=NOW)

    second = Store(tmp_path / "visa.db")
    assert len(second.fields()) == 7
    assert second.fields()["taste.summary"].value == "Brutalist now."


def test_seed_marks_attested_and_sensitive_fields(store: Store) -> None:
    fields = store.fields()
    assert fields["commerce.budget"].attested
    assert not fields["commerce.budget"].sensitive
    assert fields["health.resting_hr"].sensitive


def test_approve_creates_a_pass_with_the_session_expiry(store: Store) -> None:
    request = store.create_request(
        "gift-agent", ["taste.summary"], "choose a gift", Duration.SESSION, NOW
    )
    assert [pending.id for pending in store.pending_requests()] == [request.id]

    grant = store.approve_request(request.id, NOW)
    decided = store.request(request.id)

    assert grant.parent_id is None
    assert grant.holder == "gift-agent"
    assert grant.fields == ("taste.summary",)
    assert grant.expires_at == NOW + policy.SESSION_TTL
    assert decided is not None and decided.status is RequestStatus.APPROVED
    assert store.grant_for_request(request.id) == grant
    assert store.pending_requests() == []


@pytest.mark.parametrize("duration", [Duration.ONCE, Duration.PERSISTENT])
def test_only_a_session_pass_carries_an_expiry(store: Store, duration: Duration) -> None:
    assert approved(store, "taste.summary", duration=duration).expires_at is None


def test_a_request_is_decided_once(store: Store) -> None:
    request = store.create_request(
        "gift-agent", ["taste.summary"], "choose a gift", Duration.SESSION, NOW
    )
    store.approve_request(request.id, NOW)

    with pytest.raises(VisaError) as refusal:
        store.deny_request(request.id, NOW)
    assert refusal.value.code is ErrorCode.ALREADY_DECIDED

    with pytest.raises(VisaError) as missing:
        store.approve_request("req_nothing", NOW)
    assert missing.value.code is ErrorCode.NOT_FOUND


def test_a_denied_request_issues_no_pass(store: Store) -> None:
    request = store.create_request(
        "gift-agent", ["taste.summary"], "choose a gift", Duration.SESSION, NOW
    )
    denied = store.deny_request(request.id, NOW)

    assert denied.app == "gift-agent"
    assert store.grant_for_request(request.id) is None
    assert store.grants() == []


def test_every_mutation_leaves_a_receipt(store: Store) -> None:
    request = store.create_request(
        "gift-agent", ["taste.summary"], "choose a gift", Duration.SESSION, NOW
    )
    grant = store.approve_request(request.id, NOW)
    store.record_read(grant, [store.fields()["taste.summary"]], NOW)
    terms = policy.authorize_delegation(grant, ["taste.summary"], 600, NOW, store.fields())
    store.create_visa(grant, terms, "gift-runner", NOW)
    store.queue_suggestion(grant, "taste.summary", "Loves linen.", "seen twice", NOW)
    store.log_refusal(grant.id, "gift-agent", VisaError(ErrorCode.OUT_OF_SCOPE, "not covered"), NOW)
    store.log_public_read("erin", "gift-agent", NOW)
    store.revoke(grant.id, NOW)

    assert {receipt.action for receipt in store.receipts()} == {
        ReceiptAction.REQUEST,
        ReceiptAction.PASS_ISSUED,
        ReceiptAction.READ,
        ReceiptAction.VISA_ISSUED,
        ReceiptAction.SUGGESTION,
        ReceiptAction.REFUSED,
        ReceiptAction.PUBLIC_READ,
        ReceiptAction.REVOKED,
    }


def test_a_read_of_a_special_category_is_stamped_apart(store: Store) -> None:
    grant = approved(store, "health.resting_hr")
    store.record_read(grant, [store.fields()["health.resting_hr"]], NOW)

    assert store.receipts(1)[0].action is ReceiptAction.SENSITIVE_READ


def test_reads_are_counted_against_the_grant(store: Store) -> None:
    grant = approved(store, "taste.summary")
    field = store.fields()["taste.summary"]

    store.record_read(grant, [field], NOW)
    store.record_read(grant, [field], NOW)

    assert live(store, grant.id).reads == 2


def test_revoke_cascades_to_transit_visas(store: Store) -> None:
    parent = approved(store, "taste.summary", "style.preferences")
    terms = policy.authorize_delegation(parent, ["taste.summary"], 600, NOW, store.fields())
    first = store.create_visa(parent, terms, "gift-runner", NOW)
    second = store.create_visa(parent, terms, "photo-runner", NOW)

    assert store.revoke(parent.id, NOW) == 3
    assert live(store, parent.id).revoked
    assert live(store, first.id).revoked
    assert live(store, second.id).revoked


def test_revoking_twice_reports_nothing_left_to_kill(store: Store) -> None:
    grant = approved(store, "taste.summary")

    assert store.revoke(grant.id, NOW) == 1
    assert store.revoke(grant.id, NOW) == 0

    with pytest.raises(VisaError) as refusal:
        store.revoke("pass_nothing", NOW)
    assert refusal.value.code is ErrorCode.NOT_FOUND


def test_revoking_a_visa_leaves_its_parent_alone(store: Store) -> None:
    parent = approved(store, "taste.summary")
    terms = policy.authorize_delegation(parent, ["taste.summary"], 600, NOW, store.fields())
    visa = store.create_visa(parent, terms, "gift-runner", NOW)

    assert store.revoke(visa.id, NOW) == 1
    assert not live(store, parent.id).revoked


def test_a_visa_is_stored_on_the_terms_policy_set(store: Store) -> None:
    parent = approved(store, "taste.summary", "style.preferences")
    terms = policy.authorize_delegation(parent, ["taste.summary"], 900, NOW, store.fields())
    visa = store.create_visa(parent, terms, "gift-runner", NOW)

    assert live(store, visa.id) == visa
    assert visa.parent_id == parent.id
    assert visa.fields == ("taste.summary",)
    assert visa.expires_at == NOW + policy.MAX_VISA_TTL
    assert visa.is_transit


def test_an_accepted_suggestion_writes_and_a_rejected_one_does_not(store: Store) -> None:
    grant = approved(store, "taste.summary")
    before = store.fields()["taste.summary"].value

    rejected = store.queue_suggestion(grant, "taste.summary", "Loves neon.", "one order", NOW)
    assert store.fields()["taste.summary"].value == before

    settled = store.resolve_suggestion(rejected.id, accept=False, now=NOW)
    assert settled.status is SuggestionStatus.REJECTED
    assert store.fields()["taste.summary"].value == before

    accepted = store.queue_suggestion(grant, "taste.summary", "Loves linen.", "seen twice", NOW)
    applied = store.resolve_suggestion(accepted.id, accept=True, now=NOW)

    assert applied.status is SuggestionStatus.ACCEPTED
    assert store.fields()["taste.summary"].value == "Loves linen."


def test_an_accepted_suggestion_keeps_the_flags_the_holder_set(store: Store) -> None:
    grant = approved(store, "taste.summary")
    raise_budget = store.queue_suggestion(grant, "commerce.budget", "240", "she raised it", NOW)
    store.resolve_suggestion(raise_budget.id, accept=True, now=NOW)

    budget = store.fields()["commerce.budget"]
    assert budget.value == "240"
    assert budget.attested


def test_a_suggestion_is_resolved_once(store: Store) -> None:
    grant = approved(store, "taste.summary")
    suggestion = store.queue_suggestion(grant, "taste.summary", "Loves linen.", "seen", NOW)
    store.resolve_suggestion(suggestion.id, accept=True, now=NOW)

    with pytest.raises(VisaError) as refusal:
        store.resolve_suggestion(suggestion.id, accept=False, now=NOW)
    assert refusal.value.code is ErrorCode.ALREADY_DECIDED

    with pytest.raises(VisaError) as missing:
        store.resolve_suggestion(9999, accept=True, now=NOW)
    assert missing.value.code is ErrorCode.NOT_FOUND


def test_unknown_ids_read_as_none(store: Store) -> None:
    assert store.grant("pass_nothing") is None
    assert store.request("req_nothing") is None
    assert store.grant_for_request("req_nothing") is None
    assert store.suggestions() == []
