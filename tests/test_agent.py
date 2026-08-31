from datetime import timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from prereg import did as didmod
from prereg import record
from prereg.agent import Agent, ClaimDraft
from prereg.store import Store
from prereg.wire import Message

ROOM = "d-test"


def identity() -> didmod.Identity:
    private = Ed25519PrivateKey.generate()
    return didmod.Identity(private, didmod.did_from_public_key(private.public_key()))


class FakeTechnocore:
    """Enough of the wire to drive the loop without touching the network."""

    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.notes: dict[tuple[str, str], str] = {}
        self.seq = 0
        self.refused: list[str] = []

    def read(self, room, since=None, limit=50, wait=0):
        return [m for m in self.messages if since is None or m.seq > since][:limit]

    def export(self, room):
        return list(self.messages)

    def read_note(self, ns, key):
        return self.notes.get((ns, key))

    def set_note(self, ns, key, value, if_value=None, if_absent=False):
        self.notes[(ns, key)] = value
        return "ok"

    def say_signed(self, room, did, sig, nonce, text):
        payload = didmod.room_payload(room, nonce, text)
        if not didmod.verify(did, sig, payload):
            self.refused.append(text)
            raise AssertionError("the fake server refused an invalid signature")
        self.seq += 1
        message = Message(seq=self.seq, ts="2026-08-31T12:00:00Z", sender=did,
                          text=didmod.sweep(text))
        self.messages.append(message)
        return message


class OneDraft:
    def __init__(self, *drafts):
        self.drafts = list(drafts)

    def pending(self):
        out, self.drafts = self.drafts, []
        return out


class AlwaysHit:
    def resolve(self, claim):
        return ("hit", "0xproof", "resolved by the fake chain")


class NeverResolves:
    def resolve(self, claim):
        return None


def draft(subject="0xabc", confidence=0.8):
    return ClaimDraft(
        chain="base", subject=subject, call="rug", confidence=confidence,
        horizon=timedelta(hours=24), evidence=b"bundle", text="because",
    )


def make(tmp_path, source=None, resolver=None, **kw):
    ident = identity()
    client = FakeTechnocore()
    agent = Agent(ident, client, Store(tmp_path), ROOM,
                  source=source, resolver=resolver, **kw)
    return ident, client, agent


def test_a_cycle_with_nothing_to_say_writes_no_room_message(tmp_path):
    _ident, client, agent = make(tmp_path)
    first = agent.cycle(wait=0)
    # The opening scoreboard write is a real event: it is what `status` reads to
    # answer "is this agent alive" before there is any claim to look at.
    assert first.scoreboard_written
    assert client.messages == []
    # With the record unchanged, the next pass has nothing to do at all.
    assert agent.cycle(wait=0).quiet


def test_a_draft_becomes_a_signed_claim_in_the_room(tmp_path):
    ident, client, agent = make(tmp_path, source=OneDraft(draft()))
    result = agent.cycle(wait=0)
    assert len(result.claimed) == 1
    assert len(client.messages) == 1
    parsed = record.parse(client.messages[0].text)
    assert isinstance(parsed, record.Claim)
    assert parsed.subject == "0xabc"
    assert client.messages[0].sender == ident.did


def test_the_same_subject_is_not_claimed_twice(tmp_path):
    _ident, client, agent = make(tmp_path, source=OneDraft(draft(), draft()))
    agent.cycle(wait=0)
    assert len(client.messages) == 1


def test_a_resolved_claim_gets_settled_on_the_next_cycle(tmp_path):
    _ident, client, agent = make(tmp_path, source=OneDraft(draft()),
                                 resolver=NeverResolves())
    agent.cycle(wait=0)
    assert len(client.messages) == 1

    agent.resolver = AlwaysHit()
    result = agent.cycle(wait=0)
    assert len(result.settled) == 1
    settlement = record.parse(client.messages[-1].text)
    assert isinstance(settlement, record.Settlement)
    assert settlement.outcome == "hit"


def test_a_claim_is_never_settled_twice(tmp_path):
    _ident, client, agent = make(tmp_path, source=OneDraft(draft()),
                                 resolver=AlwaysHit())
    agent.cycle(wait=0)          # claims
    agent.cycle(wait=0)          # settles
    assert len(client.messages) == 2
    agent.cycle(wait=0)          # has nothing left to do
    assert len(client.messages) == 2


def test_the_open_claim_ceiling_is_respected(tmp_path):
    drafts = [draft(subject=f"0x{i:03d}") for i in range(10)]
    _ident, client, agent = make(
        tmp_path, source=OneDraft(*drafts), resolver=NeverResolves(),
        max_open=2, max_claims_per_cycle=10,
    )
    agent.cycle(wait=0)
    assert len(client.messages) == 2


def test_at_most_three_claims_leave_in_one_cycle_by_default(tmp_path):
    drafts = [draft(subject=f"0x{i:03d}") for i in range(9)]
    _ident, client, agent = make(tmp_path, source=OneDraft(*drafts),
                                 resolver=NeverResolves())
    agent.cycle(wait=0)
    assert len(client.messages) == 3


def test_the_scoreboard_goes_to_a_note_not_the_room(tmp_path):
    _ident, client, agent = make(tmp_path, source=OneDraft(draft()),
                                 resolver=NeverResolves())
    agent.cycle(wait=0)
    assert len(client.notes) == 1
    ((ns, _key), value), = client.notes.items()
    assert ns == "prereg"
    assert value.startswith("prereg/1 record ")
    assert "open=1" in value
    # One claim in the room, and no scoreboard line anywhere in it.
    assert len(client.messages) == 1
    assert not any("record claims=" in m.text for m in client.messages)


def test_the_scoreboard_is_not_rewritten_when_nothing_changed(tmp_path):
    _ident, client, agent = make(tmp_path, resolver=NeverResolves())
    first = agent.cycle(wait=0)
    second = agent.cycle(wait=0)
    assert first.scoreboard_written
    assert not second.scoreboard_written


def test_every_published_line_is_kept_in_the_signature_log(tmp_path):
    _ident, client, agent = make(tmp_path, source=OneDraft(draft()),
                                 resolver=AlwaysHit())
    agent.cycle(wait=0)
    agent.cycle(wait=0)
    logged = Store(tmp_path).signatures()
    assert {e.text for e in logged} == {m.text for m in client.messages}
    for entry in logged:
        assert didmod.verify(
            entry.did, entry.sig, didmod.room_payload(ROOM, entry.nonce, entry.text)
        )


def test_nonces_increase_across_cycles(tmp_path):
    _ident, _client, agent = make(tmp_path, source=OneDraft(draft(), draft("0xdef")),
                                  resolver=NeverResolves())
    agent.cycle(wait=0)
    nonces = [e.nonce for e in Store(tmp_path).signatures()]
    assert nonces == sorted(set(nonces))


def test_run_stops_after_the_requested_cycles(tmp_path):
    _ident, client, agent = make(tmp_path, source=OneDraft(draft()),
                                 resolver=NeverResolves())
    agent.run(interval=0, cycles=2)
    assert len(client.messages) == 1


def test_the_server_timestamp_parser_takes_what_the_server_actually_sends():
    from prereg.agent import _server_time

    # Microseconds are what the live service stamps; the strict record parser
    # rejects them, which silently made every liveness check report STALE.
    for raw in (
        "2026-08-31T18:29:15.202540Z",
        "2026-08-31T18:29:15Z",
        "2026-08-31T18:29:15+00:00",
        "2026-08-31T18:29:15.202540+00:00",
    ):
        parsed = _server_time(raw)
        assert parsed is not None, raw
        assert parsed.tzinfo is not None
        assert parsed.year == 2026 and parsed.hour == 18

    for bad in ("", "   ", "not a time", None):
        assert _server_time(bad) is None


def test_dry_run_touches_neither_the_room_nor_the_notes(tmp_path):
    _ident, client, agent = make(tmp_path, source=OneDraft(draft()),
                                 resolver=AlwaysHit(), dry_run=True)
    agent.cycle(wait=0)
    agent.cycle(wait=0)
    assert client.messages == []
    assert client.notes == {}
    assert Store(tmp_path).signatures() == []


def test_an_identity_round_trips_through_the_environment(tmp_path, monkeypatch):
    import base64

    from prereg import did as d

    path = tmp_path / "k.pem"
    original = d.create(path, "a-long-enough-passphrase")
    monkeypatch.setenv(
        "PREREG_IDENTITY_PEM", base64.b64encode(path.read_bytes()).decode()
    )
    loaded = d.load_from_env("a-long-enough-passphrase")
    assert loaded is not None and loaded.did == original.did

    monkeypatch.delenv("PREREG_IDENTITY_PEM")
    assert d.load_from_env("a-long-enough-passphrase") is None
