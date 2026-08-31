import json
from datetime import timedelta

from prereg import record
from prereg.sources import Chain, Router
from prereg.sources.network import (
    BOT_AT,
    HUMAN_ABOVE,
    NetworkResolver,
    NetworkSource,
    measure,
)
from prereg.wire import Message


class FakeClient:
    """Serves a room listing and canned transcripts."""

    def __init__(self, rooms: dict[str, list[str]]) -> None:
        self.transcripts = rooms

    def rooms(self):
        return {"rooms": [{"room": name} for name in self.transcripts]}

    def read(self, room, since=None, limit=50, wait=0):
        texts = self.transcripts.get(room, [])
        return [
            Message(seq=i + 1, ts="2026-08-31T00:00:00Z",
                    sender=f"did:key:z6Mk{i % 7:044d}", text=text)
            for i, text in enumerate(texts[:limit])
        ]


def looping(n=200):
    """One template with the numbers moving: a bot."""
    return [f"[TOPLOC Trace #{i}] Validated weights (integrity: 99.4%)" for i in range(n)]


# Genuinely different sentences, not one sentence with the numbers moving --
# shape() collapses digits on purpose, so counted text has to actually differ.
WORDS = ("liquidity bridge relay ledger cipher quorum gossip epoch shard beacon "
         "anchor lattice prism vector cascade harbor kernel matrix nimbus onyx "
         "pulse quartz ripple summit tundra vertex willow zenith amber cobalt").split()


def varied(n=200):
    import random

    rng = random.Random(20260831)  # deterministic, but genuinely different lines
    out, seen = [], set()
    while len(out) < n:
        a, b, c = rng.sample(WORDS, 3)
        line = f"the {a} keeps its {b} while every {c} drifts apart"
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def middling(n=200):
    """A pool wide enough to clear the bot margin, too narrow to look human."""
    pool = [f"the {WORDS[j]} reports steady" for j in range(30)]
    return [pool[i % len(pool)] for i in range(n)]


def test_a_looping_room_measures_as_almost_no_shapes():
    client = FakeClient({"loop": looping()})
    m = measure(client, "loop")
    assert m is not None
    assert m.shape_diversity <= BOT_AT
    assert m.sampled == 200


def test_a_varied_room_measures_high():
    m = measure(FakeClient({"talk": varied()}), "talk")
    assert m.shape_diversity > HUMAN_ABOVE


def test_a_room_too_small_to_judge_is_not_measured():
    assert measure(FakeClient({"new": ["hi", "there"]}), "new") is None


def test_a_looping_room_becomes_a_bot_claim():
    source = NetworkSource(FakeClient({"loop": looping()}))
    drafts = source.pending()
    assert len(drafts) == 1
    assert drafts[0].domain == "network"
    assert drafts[0].call == "bot"
    assert drafts[0].subject == "room:loop"
    assert drafts[0].confidence >= 0.8
    assert json.loads(drafts[0].evidence)["room"] == "loop"


def test_a_varied_room_becomes_a_human_claim():
    drafts = NetworkSource(FakeClient({"talk": varied()})).pending()
    assert len(drafts) == 1
    assert drafts[0].call == "human"


def test_a_room_in_the_middle_produces_nothing():
    # Between the margins a day's drift could land it either side of the line,
    # so a claim would be a coin flip wearing a confidence number.
    drafts = NetworkSource(FakeClient({"mixed": middling()})).pending()
    assert drafts == []


def test_our_own_room_is_never_claimed_about():
    source = NetworkSource(FakeClient({"mb-prereg": looping()}), skip=("mb-prereg",))
    assert source.pending() == []


def test_a_room_is_only_claimed_about_once():
    source = NetworkSource(FakeClient({"loop": looping()}))
    assert len(source.pending()) == 1
    assert source.pending() == []


def claim(call="bot", hours=-1, room="loop"):
    return record.Claim(
        id="aaaaaaaaaaaa", domain="network", subject=f"room:{room}", call=call,
        confidence=0.9, deadline=record.now() + timedelta(hours=hours),
        evidence="a" * 64, text="measured",
    )


def test_a_claim_is_not_settled_before_its_deadline():
    resolver = NetworkResolver(FakeClient({"loop": looping()}))
    assert resolver.resolve(claim(hours=24)) is None


def test_a_bot_call_on_a_still_looping_room_is_a_hit():
    resolver = NetworkResolver(FakeClient({"loop": looping()}))
    outcome, proof, _detail = resolver.resolve(claim())
    assert outcome == "hit"
    assert proof.startswith("shape-")


def test_a_bot_call_on_a_room_that_became_varied_is_a_miss():
    resolver = NetworkResolver(FakeClient({"loop": varied()}))
    assert resolver.resolve(claim())[0] == "miss"


def test_a_human_call_needs_to_clear_the_higher_threshold():
    assert NetworkResolver(FakeClient({"loop": varied()})).resolve(
        claim(call="human"))[0] == "hit"
    assert NetworkResolver(FakeClient({"loop": looping()})).resolve(
        claim(call="human"))[0] == "miss"


def test_a_vanished_room_settles_void_rather_than_being_guessed():
    outcome, proof, _detail = NetworkResolver(FakeClient({})).resolve(claim())
    assert outcome == "void"
    assert proof == "unmeasurable"


def test_a_claim_from_another_domain_is_left_alone():
    other = record.Claim(
        id="bbbbbbbbbbbb", domain="dex-liquidity", subject="base:0xa", call="rug",
        confidence=0.8, deadline=record.now() - timedelta(hours=1),
        evidence="b" * 64, text="",
    )
    assert NetworkResolver(FakeClient({})).resolve(other) is None


# -- composition ----------------------------------------------------------


class Boom:
    def pending(self):
        raise RuntimeError("source is broken")

    def resolve(self, _claim):
        raise RuntimeError("resolver is broken")


def test_one_broken_source_does_not_silence_the_others():
    working = NetworkSource(FakeClient({"loop": looping()}))
    assert len(Chain(Boom(), working).pending()) == 1


def test_a_broken_resolver_leaves_the_claim_open():
    assert Router(network=Boom()).resolve(claim()) is None


def test_the_router_sends_a_claim_to_its_own_domain():
    router = Router(network=NetworkResolver(FakeClient({"loop": looping()})))
    assert router.resolve(claim())[0] == "hit"
    assert router.resolve(
        record.Claim(id="cccccccccccc", domain="unknown-thing", subject="x",
                    call="y", confidence=0.5,
                    deadline=record.now() - timedelta(hours=1),
                    evidence="c" * 64, text="")
    ) is None
