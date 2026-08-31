"""Falsifiable claims about the rooms on technocore.chat itself.

This is the domain the room exists for. Flop Labs is building a network where
validators have to decide whether work was really done, and the only piece of it
running today already has the problem in miniature: 51,588 rooms, most of them
emitting a fixed sentence pool, and a writer-diversity metric that a swarm
minting one key per message defeats outright.

So an agent should be able to say "that room is a script, hold me to it", and be
held to it. That is a classification anybody can check by rerunning `prereg
survey`, it is wrong sometimes, and being wrong shows up permanently in the
record. None of the agents currently posting `Agent node reporting in` can be
wrong about anything, which is exactly why none of it is worth reading.

THE DEFINITION, FIXED
---------------------
Measured over the newest 200 messages of the room, using `survey.shape`, which
collapses a message to the template it came from -- digits, hex, addresses, URLs
and leading decoration all become placeholders.

  call=bot     at settlement, shape diversity is <= 0.15
  call=human   at settlement, shape diversity is  > 0.40

A room that has been deleted, or that no longer carries a big enough sample to
measure, settles `void` rather than being scored on a guess.

The gap between the two thresholds is deliberate. A claim has to survive a
margin, not a rounding error.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from prereg.agent import ClaimDraft
from prereg.record import Claim
from prereg.survey import RoomSurvey, shape, survey_room
from prereg.wire import Technocore, WireError

log = logging.getLogger("prereg.network")

DOMAIN = "network"

# Frozen. selfcheck.py fails if any of these move.
BOT_AT = 0.15
HUMAN_ABOVE = 0.40
SAMPLE = 200
MIN_SAMPLE_TO_JUDGE = 50
DEFAULT_HORIZON = timedelta(hours=24)

# The room has to be far enough from its threshold now for the call to mean
# something in a day's time.
BOT_MARGIN = 0.10
HUMAN_MARGIN = 0.55


@dataclass(frozen=True)
class Measurement:
    room: str
    sampled: int
    writers: int
    shapes: int
    shape_diversity: float
    nick_diversity: float
    top_shape: str

    def bundle(self) -> bytes:
        import json

        return json.dumps({
            "room": self.room,
            "sampled": self.sampled,
            "writers": self.writers,
            "shapes": self.shapes,
            "shape_diversity": round(self.shape_diversity, 4),
            "nick_diversity": round(self.nick_diversity, 4),
            "top_shape": self.top_shape[:200],
            "definition": (
                f"bot = shape diversity <= {BOT_AT} at settlement; "
                f"human = > {HUMAN_ABOVE}; measured over {SAMPLE} messages"
            ),
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")


def measure(client: Technocore, room: str) -> Measurement | None:
    surveyed: RoomSurvey | None = survey_room(client, room, limit=SAMPLE)
    if surveyed is None or surveyed.sampled < MIN_SAMPLE_TO_JUDGE:
        return None
    return Measurement(
        room=room,
        sampled=surveyed.sampled,
        writers=surveyed.writers,
        shapes=surveyed.shapes,
        shape_diversity=surveyed.shape_diversity,
        nick_diversity=surveyed.nick_diversity,
        top_shape=surveyed.top_shape,
    )


class NetworkSource:
    """Proposes a call about rooms whose behaviour is currently unambiguous."""

    def __init__(
        self,
        client: Technocore,
        horizon: timedelta = DEFAULT_HORIZON,
        limit: int = 2,
        skip: tuple[str, ...] = (),
    ) -> None:
        self.client = client
        self.horizon = horizon
        self.limit = limit
        self.skip = set(skip)
        self._seen: set[str] = set()

    def pending(self) -> list[ClaimDraft]:
        try:
            listing = self.client.rooms()
        except WireError as exc:
            log.info("room listing unavailable: %s", exc)
            return []

        drafts: list[ClaimDraft] = []
        for entry in listing.get("rooms") or []:
            if len(drafts) >= self.limit:
                break
            room = str(entry.get("room") or "")
            if not room or room in self._seen or room in self.skip:
                continue
            self._seen.add(room)
            draft = self.consider(room)
            if draft is not None:
                drafts.append(draft)
        return drafts

    def consider(self, room: str) -> ClaimDraft | None:
        measurement = measure(self.client, room)
        if measurement is None:
            return None

        diversity = measurement.shape_diversity
        if diversity <= BOT_MARGIN:
            call, confidence = "bot", 0.90 - diversity
        elif diversity >= HUMAN_MARGIN:
            call, confidence = "human", min(0.92, 0.55 + diversity / 2)
        else:
            # Between the margins the room could plausibly land either side of
            # its threshold by tomorrow, and a claim would be a coin flip
            # dressed up as a call.
            log.info("abstaining on %s: shape diversity %.2f is in the middle",
                     room, diversity)
            return None

        return ClaimDraft(
            domain=DOMAIN,
            subject=f"room:{room}"[:90],
            call=call,
            confidence=round(confidence, 2),
            horizon=self.horizon,
            evidence=measurement.bundle(),
            text=(
                f"shape {diversity:.2f} nick {measurement.nick_diversity:.2f} over "
                f"{measurement.sampled}; most common template: "
                f"{shape(measurement.top_shape)[:90]}"
            ),
        )


class NetworkResolver:
    """Settles by measuring the same room the same way, after the horizon."""

    def __init__(self, client: Technocore) -> None:
        self.client = client

    def resolve(self, claim: Claim) -> tuple[str, str, str] | None:
        from prereg.record import now

        if claim.domain != DOMAIN:
            return None
        if claim.deadline > now():
            return None  # a room can change; only the deadline decides

        room = claim.subject.removeprefix("room:")
        measurement = measure(self.client, room)
        if measurement is None:
            return ("void", "unmeasurable",
                    "the room is gone or too small to sample; scoring it either "
                    "way would be a guess")

        diversity = measurement.shape_diversity
        if claim.call == "bot":
            correct = diversity <= BOT_AT
        elif claim.call == "human":
            correct = diversity > HUMAN_ABOVE
        else:
            return ("void", "unknown-call", f"no rule for call={claim.call}")

        return (
            "hit" if correct else "miss",
            f"shape-{diversity:.3f}",
            f"shape diversity {diversity:.3f} over {measurement.sampled} messages "
            f"({measurement.writers} writers)",
        )
