"""The one test that leaves this machine: a real read of a published ego.ist passport.

Excluded from the default run by the live marker. Run it deliberately with `make e2e`.
"""

from __future__ import annotations

import os

import pytest

from agentvisa.passport import PublicPassportClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("LIVE") != "1", reason="set LIVE=1 to call ego.ist"),
]

# A published passport on the live service; the founder's own, so it stays published.
USERNAME = "erin"


def test_a_published_passport_reads_back_from_egoist() -> None:
    profile = PublicPassportClient().read(USERNAME)

    assert profile["ok"] is True
    assert profile["username"] == USERNAME
    assert profile["serial"]
    assert isinstance(profile["document"], dict)
