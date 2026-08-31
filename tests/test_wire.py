import io
import urllib.error
import urllib.request

import pytest

from prereg import wire


def raising(code, body=b""):
    def urlopen(req, timeout=None):
        raise urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))

    return urlopen


def test_503_on_a_read_is_service_unavailable(monkeypatch):
    # A 503 is the service being down, not us. A read has to be able to tell
    # that apart from a real error so a cycle waits instead of giving up.
    monkeypatch.setattr(urllib.request, "urlopen", raising(503))
    with pytest.raises(wire.ServiceUnavailable):
        wire.Technocore("https://x").export("mb-prereg")


def test_503_on_a_write_is_outcome_unknown(monkeypatch):
    # A proxy 503 in front of a write cannot promise the write missed the origin.
    monkeypatch.setattr(urllib.request, "urlopen", raising(503))
    with pytest.raises(wire.WriteOutcomeUnknown):
        wire.Technocore("https://x").say_signed("mb-prereg", "did:key:z6Mk", "s", 1, "hi")


def test_429_still_maps_to_rate_limited(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", raising(429, b"5s"))
    with pytest.raises(wire.RateLimited):
        wire.Technocore("https://x").export("mb-prereg")


def test_a_plain_404_on_a_note_read_is_absence_not_an_error(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", raising(404))
    assert wire.Technocore("https://x").read_note("prereg", "abc") is None


def test_a_500_is_a_generic_wire_error(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", raising(500))
    with pytest.raises(wire.WireError):
        wire.Technocore("https://x").export("mb-prereg")
