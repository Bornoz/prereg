from datetime import timedelta

from prereg import record, score
from prereg.wire import Message

DID = "did:key:z6MkjkoU" + "1" * 40
OTHER = "did:key:z6MknZqU" + "2" * 40
DIGEST = "b" * 64
ROOM = "d-prereg"


def claim_line(claim_id, hours, confidence=0.8):
    return record.Claim(
        id=claim_id, domain="dex-liquidity", subject="0xabc", call="rug",
        confidence=confidence, deadline=record.now() + timedelta(hours=hours),
        evidence=DIGEST, text="because",
    ).line()


def settle_line(claim_id, outcome):
    return record.Settlement(
        id=claim_id, outcome=outcome, at=record.now(), proof="0xf00", text="settled",
    ).line()


def transcript(*pairs):
    return [
        Message(seq=i + 1, ts="2026-08-31T00:00:00Z", sender=sender, text=text)
        for i, (sender, text) in enumerate(pairs)
    ]


def test_a_settled_hit_counts_once():
    messages = transcript(
        (DID, claim_line("aaaaaaaaaaaa", 24)),
        (DID, settle_line("aaaaaaaaaaaa", "hit")),
    )
    report = score.build(messages, DID, ROOM)
    assert (report.hits, report.misses, report.open) == (1, 0, 0)
    assert report.accuracy == 1.0


def test_an_unsettled_claim_past_its_deadline_is_scored_against_us():
    # This is the rule that stops a forecaster from settling only the winners.
    messages = transcript((DID, claim_line("bbbbbbbbbbbb", -1)))
    report = score.build(messages, DID, ROOM)
    assert report.expired == 1
    assert report.settled == 1
    assert report.accuracy == 0.0


def test_an_open_claim_is_not_scored_yet():
    messages = transcript((DID, claim_line("cccccccccccc", 48)))
    report = score.build(messages, DID, ROOM)
    assert report.open == 1
    assert report.settled == 0
    assert report.accuracy is None


def test_somebody_elses_messages_are_ignored():
    messages = transcript(
        (OTHER, claim_line("dddddddddddd", 24)),
        (OTHER, settle_line("dddddddddddd", "hit")),
        (DID, "gm"),
    )
    assert score.build(messages, DID, ROOM).entries == []


def test_a_second_settlement_is_refused_and_reported():
    messages = transcript(
        (DID, claim_line("eeeeeeeeeeee", 24)),
        (DID, settle_line("eeeeeeeeeeee", "miss")),
        (DID, settle_line("eeeeeeeeeeee", "hit")),
    )
    report = score.build(messages, DID, ROOM)
    assert report.misses == 1
    assert report.hits == 0
    assert any("already settled" in a for a in report.anomalies)


def test_a_settlement_without_a_claim_is_reported():
    report = score.build(transcript((DID, settle_line("ffffffffffff", "hit"))), DID, ROOM)
    assert report.entries == []
    assert any("unknown claim" in a for a in report.anomalies)


def test_a_reused_claim_id_is_reported():
    messages = transcript(
        (DID, claim_line("aaaaaaaaaaaa", 24)),
        (DID, claim_line("aaaaaaaaaaaa", 24)),
    )
    report = score.build(messages, DID, ROOM)
    assert len(report.entries) == 1
    assert any("already used" in a for a in report.anomalies)


def test_brier_punishes_a_confident_miss_more_than_a_hesitant_one():
    confident = score.build(
        transcript(
            (DID, claim_line("aaaaaaaaaaaa", 24, confidence=0.99)),
            (DID, settle_line("aaaaaaaaaaaa", "miss")),
        ), DID, ROOM,
    )
    hesitant = score.build(
        transcript(
            (DID, claim_line("bbbbbbbbbbbb", 24, confidence=0.55)),
            (DID, settle_line("bbbbbbbbbbbb", "miss")),
        ), DID, ROOM,
    )
    assert confident.brier > hesitant.brier
    assert confident.accuracy == hesitant.accuracy == 0.0


def test_a_high_void_rate_is_surfaced_in_the_summary():
    messages = transcript(
        (DID, claim_line("aaaaaaaaaaaa", 24)),
        (DID, settle_line("aaaaaaaaaaaa", "void")),
        (DID, claim_line("bbbbbbbbbbbb", 24)),
        (DID, settle_line("bbbbbbbbbbbb", "hit")),
    )
    assert "void rate" in score.summary(score.build(messages, DID, ROOM))


def test_replay_does_not_depend_on_message_order():
    forward = transcript(
        (DID, claim_line("aaaaaaaaaaaa", 24)),
        (DID, settle_line("aaaaaaaaaaaa", "hit")),
    )
    assert score.build(list(reversed(forward)), DID, ROOM).hits == 1
