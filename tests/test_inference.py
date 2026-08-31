from datetime import timedelta

from prereg import record, score
from prereg.sources.inference import (
    MAX_WINDOW,
    MIN_WINDOW,
    OPS,
    InferenceVerifier,
    Spec,
    compute,
    parse_spec,
    result_digest,
)
from prereg.wire import Message

VERIFIER_DID = "did:key:z6Mk" + "9" * 44
ATTESTER_DID = "did:key:z6Mk" + "8" * 44
ROOM = "mb-prereg"


class FakeClient:
    def __init__(self, messages):
        self.messages = messages

    def read(self, room, since=None, limit=50, wait=0):
        out = [m for m in self.messages if since is None or m.seq > since]
        return out[:limit]

    def export(self, room):
        return list(self.messages)


def transcript(n=60, start=100, sender="did:key:z6Mkabc", text=None):
    return [
        Message(seq=start + i, ts="2026-09-01T00:00:00Z", sender=sender,
                text=text(i) if text else f"line about {chr(97 + i % 26)} and {i}")
        for i in range(n)
    ]


def spec(op="writer_count", start=100, end=150):
    return Spec(op=op, room="lobby", start=start, end=end)


# -- the spec format ------------------------------------------------------


def test_a_spec_round_trips_through_its_line():
    parsed = parse_spec(spec().line())
    assert parsed == spec()


def test_a_spec_is_found_inside_surrounding_prose():
    assert parse_spec(f"attesting: {spec().line()} , recomputed locally") == spec()


def test_an_unknown_operation_is_refused():
    assert parse_spec("inference/1 op=make_it_up room=lobby from=1 to=50") is None


def test_a_window_outside_the_bounds_is_refused():
    assert parse_spec(f"inference/1 op=writer_count room=lobby from=1 to={MIN_WINDOW}") is None
    assert parse_spec(
        f"inference/1 op=writer_count room=lobby from=1 to={2 + MAX_WINDOW}"
    ) is None


def test_junk_is_not_a_spec():
    for line in ("", "gm", "inference/2 op=writer_count room=lobby from=1 to=50"):
        assert parse_spec(line) is None


# -- the operations -------------------------------------------------------


def test_every_operation_is_a_pure_function_of_the_window():
    messages = transcript()
    for name, op in OPS.items():
        first, second = op(messages), op(list(reversed(messages)))
        assert first == second, f"{name} depends on ordering of its input"


def test_the_digest_binds_the_answer_to_the_question():
    assert result_digest(spec(), "7") != result_digest(spec(op="signed_share"), "7")
    assert result_digest(spec(), "7") != result_digest(spec(), "8")
    assert result_digest(spec(), "7") == result_digest(spec(), "7")


def test_computing_reads_only_the_requested_window():
    client = FakeClient(transcript(n=120, start=100))
    assert compute(client, spec(start=100, end=150)) == "1"


def test_a_window_the_ring_has_dropped_cannot_be_computed():
    client = FakeClient(transcript(n=5, start=100))
    assert compute(client, spec(start=100, end=150)) is None


# -- verification ---------------------------------------------------------


def attestation(digest, call="reproduces", hours=-1, text=None):
    return record.Claim(
        id="aaaaaaaaaaaa", domain="inference", subject=digest, call=call,
        confidence=0.95, deadline=record.now() + timedelta(hours=hours),
        evidence="a" * 64, text=text if text is not None else spec().line(),
    )


def verifier(messages=None):
    return InferenceVerifier(FakeClient(messages or transcript(n=120)), VERIFIER_DID)


def test_an_honest_attestation_is_confirmed():
    client = FakeClient(transcript(n=120))
    truth = result_digest(spec(), compute(client, spec()))
    outcome, proof, _detail = InferenceVerifier(client, VERIFIER_DID).resolve(
        attestation(truth)
    )
    assert outcome == "hit"
    assert proof.startswith("digest-")


def test_a_wrong_digest_is_caught():
    assert verifier().resolve(attestation("f" * 64))[0] == "miss"


def test_a_diverges_call_is_scored_the_other_way_round():
    client = FakeClient(transcript(n=120))
    truth = result_digest(spec(), compute(client, spec()))
    v = InferenceVerifier(client, VERIFIER_DID)
    assert v.resolve(attestation(truth, call="diverges"))[0] == "miss"
    assert v.resolve(attestation("f" * 64, call="diverges"))[0] == "hit"


def test_an_attestation_without_a_readable_spec_is_void():
    outcome, proof, _ = verifier().resolve(attestation("f" * 64, text="trust me"))
    assert (outcome, proof) == ("void", "unreadable-spec")


def test_an_unfetchable_window_is_void_rather_than_guessed():
    outcome, proof, _ = InferenceVerifier(
        FakeClient(transcript(n=3)), VERIFIER_DID
    ).resolve(attestation("f" * 64))
    assert (outcome, proof) == ("void", "window-gone")


def test_claims_from_other_domains_are_left_alone():
    other = record.Claim(
        id="bbbbbbbbbbbb", domain="network", subject="room:x", call="bot",
        confidence=0.9, deadline=record.now() - timedelta(hours=1),
        evidence="b" * 64, text="",
    )
    assert verifier().resolve(other) is None


# -- the independence rule ------------------------------------------------


def messages_for(claim, settler):
    settlement = record.build_settlement(claim.id, "hit", "digest-abc", "recomputed")
    return [
        Message(seq=1, ts="t", sender=ATTESTER_DID, text=claim.line()),
        Message(seq=2, ts="t", sender=settler, text=settlement.line()),
    ]


def test_an_attester_cannot_settle_its_own_attestation():
    """The rule that stops the inference domain being a conversation with itself."""
    claim = attestation("f" * 64, hours=24)
    report = score.build(messages_for(claim, ATTESTER_DID), ATTESTER_DID, ROOM)
    assert report.entries[0].settlement is None
    assert report.entries[0].state == "open"
    assert any("cannot be settled by the key that made it" in a for a in report.anomalies)


def test_another_key_can_settle_it():
    claim = attestation("f" * 64, hours=24)
    report = score.build(messages_for(claim, VERIFIER_DID), ATTESTER_DID, ROOM)
    entry = report.entries[0]
    assert entry.state == "hit"
    assert entry.settled_by == VERIFIER_DID
    assert entry.independently_settled


def test_a_self_settled_network_claim_is_still_fine():
    """Mechanical settlement against outside data does not need a second party."""
    claim = record.Claim(
        id="cccccccccccc", domain="network", subject="room:x", call="bot",
        confidence=0.9, deadline=record.now() + timedelta(hours=24),
        evidence="c" * 64, text="",
    )
    report = score.build(messages_for(claim, ATTESTER_DID), ATTESTER_DID, ROOM)
    assert report.entries[0].state == "hit"
    assert not report.entries[0].independently_settled
