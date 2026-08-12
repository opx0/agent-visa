"""Parsing the ego.ist reply, offline: the payload arrives as JSON nested inside an SSE frame."""

from __future__ import annotations

import json

import pytest

from agentvisa.models import ErrorCode, VisaError
from agentvisa.passport import PublicPassportClient, _parse


def envelope(profile: object) -> str:
    body = {"result": {"content": [{"type": "text", "text": json.dumps(profile)}]}}
    return f"event: message\ndata: {json.dumps(body)}\n\n"


def test_the_profile_is_unwrapped_from_the_event_and_the_json_inside_it() -> None:
    assert _parse(envelope({"ok": True, "username": "erin"})) == {"ok": True, "username": "erin"}


def test_a_plain_json_body_is_accepted_too() -> None:
    body = {"result": {"content": [{"text": json.dumps({"ok": True})}]}}
    assert _parse(json.dumps(body)) == {"ok": True}


@pytest.mark.parametrize(
    "body",
    [
        'data: {"error": {"code": -32602, "message": "unknown user"}}',
        "data: not json at all",
        "<html>bad gateway</html>",
        'data: {"result": {"content": []}}',
        f"data: {json.dumps({'result': {'content': [{'text': '[1, 2]'}]}})}",
    ],
)
def test_every_malformed_reply_becomes_one_upstream_error(body: str) -> None:
    with pytest.raises(VisaError) as refusal:
        _parse(body)
    assert refusal.value.code is ErrorCode.UPSTREAM_ERROR


def test_a_username_that_cannot_exist_never_opens_a_socket() -> None:
    with pytest.raises(VisaError) as refusal:
        PublicPassportClient().read("../etc/passwd")
    assert refusal.value.code is ErrorCode.UPSTREAM_ERROR
