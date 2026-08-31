from datetime import timedelta

import pytest

from prereg import record

DIGEST = "a" * 64


def a_claim(**over):
    args = dict(
        chain="base",
        subject="0xdeadbeef",
        call="rug",
        confidence=0.8,
        deadline=record.now() + timedelta(hours=48),
        evidence=DIGEST,
        text="deployer funded by two settled ruggers",
    )
    args.update(over)
    return record.build_claim(**args)


def test_a_claim_round_trips_through_its_line():
    claim = a_claim()
    parsed = record.parse(claim.line())
    assert isinstance(parsed, record.Claim)
    assert parsed.id == claim.id
    assert parsed.subject == "0xdeadbeef"
    assert parsed.confidence == 0.8
    assert parsed.deadline == claim.deadline
    assert parsed.text == "deployer funded by two settled ruggers"


def test_a_settlement_round_trips():
    settlement = record.build_settlement(
        "0123456789ab", "hit", "0xabc", "LP pulled at block 41"
    )
    parsed = record.parse(settlement.line())
    assert isinstance(parsed, record.Settlement)
    assert parsed.outcome == "hit"
    assert parsed.proof == "0xabc"


def test_the_line_stays_inside_the_message_cap():
    assert len(a_claim().line()) <= record.MAX_TEXT


def test_a_deadline_in_the_past_is_refused():
    with pytest.raises(record.RecordError):
        a_claim(deadline=record.now() - timedelta(hours=1))


def test_confidence_outside_zero_to_one_is_refused():
    with pytest.raises(record.RecordError):
        a_claim(confidence=1.4)


def test_evidence_has_to_be_a_full_sha256():
    with pytest.raises(record.RecordError):
        a_claim(evidence="abc")


def test_a_void_settlement_has_to_explain_itself():
    with pytest.raises(record.RecordError):
        record.build_settlement("0123456789ab", "void", "", "")
    record.build_settlement("0123456789ab", "void", "", "chain reorg, subject vanished")


def test_free_text_cannot_smuggle_the_separator():
    with pytest.raises(record.RecordError):
        a_claim(text="something -- else")


def test_a_stranger_line_parses_to_none():
    for line in (
        "gm from a quiet node somewhere",
        "prereg/1",
        "prereg/2 claim id=0123456789ab",
        "prereg/1 claim id=nothex chain=base subject=x call=rug conf=0.5 "
        f"by=2030-01-01T00:00:00Z ev={DIGEST}",
        "",
    ):
        assert record.parse(line) is None


def test_multiline_text_is_collapsed_not_rejected():
    claim = a_claim(text="first line\n\nsecond   line")
    assert claim.text == "first line second line"


def test_evidence_digest_is_stable():
    assert record.evidence_digest(b"payload") == record.evidence_digest(b"payload")
    assert record.evidence_digest(b"a") != record.evidence_digest(b"b")
