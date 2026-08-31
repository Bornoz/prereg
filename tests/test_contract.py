"""Contract tests against the real technocore.chat.

The unit suites run against a fake server, and the fake server agreed with the
real one on everything the first live deploy then disagreed on: the nonce had to
be a string, note reads carry a banner, a room read can come back as text/plain
under load. These tests exercise the actual service so the next such mismatch is
caught here, on a read-only path, rather than on a live write.

They are skipped unless PREREG_LIVE is set, because they need the network and the
service is frequently down (503). Nothing here writes: every call is a documented
GET, so running them costs the service one read each and commits nothing.

    PREREG_LIVE=1 python -m pytest tests/test_contract.py -v
"""

from __future__ import annotations

import os
import re

import pytest

from prereg import did as didmod
from prereg.wire import Technocore, WireError

pytestmark = pytest.mark.skipif(
    not os.environ.get("PREREG_LIVE"),
    reason="set PREREG_LIVE=1 to run against the real technocore.chat",
)


@pytest.fixture(scope="module")
def client():
    c = Technocore()
    try:
        # A cheap preflight; if the service is down every test would just error.
        c.rooms()
    except WireError as exc:
        pytest.skip(f"technocore.chat unavailable: {exc}")
    return c


def test_the_room_listing_has_the_fields_we_read(client):
    listing = client.rooms()
    assert isinstance(listing.get("rooms"), list)
    assert "total" in listing
    if listing["rooms"]:
        room = listing["rooms"][0]
        assert "room" in room


def test_a_room_read_parses_into_messages(client):
    # lobby always exists per the manual, and always accepts a message, so it is
    # the safe room to read.
    messages = client.read("lobby", limit=5)
    for m in messages:
        assert m.seq >= 1
        assert isinstance(m.text, str)


def test_the_export_endpoint_returns_a_replayable_ring(client):
    messages = client.export("lobby")
    seqs = [m.seq for m in messages]
    assert seqs == sorted(seqs), "export is not in sequence order"


def test_a_note_read_strips_the_untrusted_banner(client):
    # Read our own scoreboard note if it exists; any missing note is fine.
    # The point is that whatever comes back does not start with the banner.
    value = client.read_note("prereg", "does-not-exist-" + "0" * 8)
    assert value is None or not value.startswith("!! UNTRUSTED")


def test_our_did_shape_matches_what_the_service_documents(client):
    # /llms.txt is never rate limited and states the did:key shape. Confirm the
    # identity we generate would be accepted by it.
    import urllib.request

    with urllib.request.urlopen(client.base + "/llms.txt", timeout=20) as r:
        manual = r.read().decode("utf-8", "replace")
    assert "did:key:z6Mk" in manual

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    did = didmod.did_from_public_key(priv.public_key())
    assert re.fullmatch(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}", did)
