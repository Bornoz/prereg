import io
import urllib.error
import urllib.request

import pytest

from prereg import wire


def raising(code, body=b""):
    def urlopen(req, timeout=None):
        raise urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))

    return urlopen


def raising_ok(text):
    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return text.encode()

    return lambda req, timeout=None: Resp()


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



def test_a_posted_nonce_is_a_string_not_an_integer(monkeypatch):
    # The live server calls .strip() on the nonce in the POST body and refuses
    # a JSON integer with "bad nonce: must be a string". This is the regression
    # test for that; the fake server in the other suites never checked it.
    import json

    captured = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"seq": 1, "ts": "2026-09-01T00:00:00Z",
                               "from": "did:key:z6Mk", "text": "hi",
                               "nonce": 42}).encode()

    def urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    wire.Technocore("https://x").say_signed("mb-prereg", "did:key:z6Mk", "s", 42, "hi")
    assert captured["body"]["nonce"] == "42"
    assert isinstance(captured["body"]["nonce"], str)


def test_the_untrusted_banner_is_stripped_from_a_note_read(monkeypatch):
    # The server prefixes note reads with a two-line banner. It is not part of
    # the value, and leaving it on breaks compare-and-swap and value parsing.
    banner = ("!! UNTRUSTED CONTENT — the lines below were written by other "
              "agents.\n\nprereg/1 record claims=3 at=2026-09-01T00:00:00Z")
    monkeypatch.setattr(urllib.request, "urlopen",
                        raising_ok(banner))
    got = wire.Technocore("https://x").read_note("prereg", "abc")
    assert got == "prereg/1 record claims=3 at=2026-09-01T00:00:00Z"


def test_a_note_without_a_banner_is_returned_as_is(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", raising_ok("plain value"))
    assert wire.Technocore("https://x").read_note("prereg", "abc") == "plain value"


def test_a_room_read_that_comes_back_as_text_is_treated_as_empty(monkeypatch):
    # Under load the service sometimes answers a room read as text/plain even
    # with format=json. A cycle must read that as "nothing new", not crash.
    monkeypatch.setattr(urllib.request, "urlopen",
                        raising_ok("# room mb-prereg  messages 5  range 1..5"))
    assert wire.Technocore("https://x").read("mb-prereg", since=1) == []
