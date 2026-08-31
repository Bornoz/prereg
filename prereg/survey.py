"""Measure technocore.chat, so a claim about it can be checked rather than believed.

The service publishes `nick_diversity` per room: distinct writers over messages.
It is a good signal and it is already being defeated. A swarm that mints a fresh
`did:key` per message scores near 1.0 while posting one sentence with the numbers
changed, and on the live service the room with almost the highest score in the
network is exactly that. Automated scoring collapses at that point, which is why
whoever is reading submissions is reading them by hand.

So this measures the other axis. `nick_diversity` counts who is speaking;
`shape_diversity` here counts how many different things are being said, by
normalising every message into its shape -- digits, hex, addresses and URLs all
collapse -- and counting the distinct results.

    nick high + shape low   one script wearing many keys
    nick low  + shape low   one bot in a loop
    nick low  + shape high  a person, or an agent doing varied work alone
    nick high + shape high  a conversation

Nothing here writes. Everything is a documented GET from /llms.txt, so anyone can
re-run it and get their own numbers instead of taking ours.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from prereg.wire import Message, Technocore

HEX = re.compile(r"\b(0x)?[0-9a-fA-F]{8,}\b")
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
URL = re.compile(r"https?://\S+")
DID = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]+")
SPACE = re.compile(r"\s+")

# Emoji and other leading decoration, which is the cheapest way to make a fixed
# sentence pool look varied.
DECORATION = re.compile(r"^[^\w]{1,4}\s*")


def shape(text: str) -> str:
    """Reduce a message to the template it was produced from."""
    out = DECORATION.sub("", text.strip())
    out = URL.sub("<url>", out)
    out = DID.sub("<did>", out)
    out = HEX.sub("<hex>", out)
    out = NUMBER.sub("<n>", out)
    return SPACE.sub(" ", out).strip().lower()


@dataclass
class RoomSurvey:
    room: str
    topic: str | None
    sampled: int
    writers: int
    shapes: int
    top_shape: str
    top_shape_count: int
    listed_nick_diversity: float | None = None

    @property
    def nick_diversity(self) -> float:
        return self.writers / self.sampled if self.sampled else 0.0

    @property
    def shape_diversity(self) -> float:
        return self.shapes / self.sampled if self.sampled else 0.0

    @property
    def repetition(self) -> float:
        """Share of the sample taken by its single most common template."""
        return self.top_shape_count / self.sampled if self.sampled else 0.0

    @property
    def verdict(self) -> str:
        if self.sampled < 10:
            return "too small to judge"
        if self.shape_diversity < 0.15 and self.nick_diversity > 0.7:
            return "many keys, one script"
        if self.shape_diversity < 0.15:
            return "one bot in a loop"
        if self.shape_diversity < 0.4:
            return "heavily templated"
        return "varied"


@dataclass
class NetworkSurvey:
    rooms_total: int = 0
    rooms_capacity: int = 0
    notes_total: int = 0
    note_to_message_ratio: float | None = None
    listed: list[dict] = field(default_factory=list)
    clone_families: list[tuple[str, list[str]]] = field(default_factory=list)


def survey_room(client: Technocore, room: str, limit: int = 200) -> RoomSurvey | None:
    try:
        messages: list[Message] = client.read(room, limit=limit)
    except Exception:  # noqa: BLE001 - a room we cannot read is simply skipped
        return None
    if not messages:
        return None
    shapes = Counter(shape(m.text) for m in messages)
    common, count = shapes.most_common(1)[0]
    return RoomSurvey(
        room=room,
        topic=None,
        sampled=len(messages),
        writers=len({m.sender for m in messages}),
        shapes=len(shapes),
        top_shape=common[:120],
        top_shape_count=count,
    )


def clone_families(listed: list[dict], tolerance: float = 0.25) -> list[tuple[str, list[str]]]:
    """Rooms whose topics share a template and whose sizes barely differ.

    Ten independent operators do not land within a few percent of each other on
    both message count and bytes. One operator running ten copies does.
    """
    by_template: dict[str, list[dict]] = {}
    for room in listed:
        topic = room.get("topic")
        if not topic:
            continue
        key = shape(topic.replace(room.get("room", ""), "<name>"))
        by_template.setdefault(key, []).append(room)

    families = []
    for template, members in by_template.items():
        if len(members) < 3:
            continue
        sizes = [float(m.get("bytes") or 0) for m in members]
        if not sizes or min(sizes) <= 0:
            continue
        spread = (max(sizes) - min(sizes)) / max(sizes)
        if spread <= tolerance:
            families.append((template, sorted(m["room"] for m in members)))
    return families


def survey_network(client: Technocore, sample_rooms: int = 12) -> NetworkSurvey:
    listing = client.rooms()
    rooms = listing.get("rooms") or []
    engagement = listing.get("engagement") or {}
    notes = listing.get("notes") or {}

    return NetworkSurvey(
        rooms_total=int(listing.get("total") or 0),
        rooms_capacity=int(listing.get("capacity") or 0),
        notes_total=int(notes.get("total") or 0),
        note_to_message_ratio=engagement.get("windowed_note_to_message_ratio"),
        listed=rooms[:sample_rooms],
        clone_families=clone_families(rooms),
    )


def report(client: Technocore, sample_rooms: int = 12) -> str:
    network = survey_network(client, sample_rooms)
    lines = [
        "technocore.chat survey",
        f"  rooms            {network.rooms_total:,} of {network.rooms_capacity:,}",
        f"  notes            {network.notes_total:,}",
        f"  notes / message  {network.note_to_message_ratio}",
        "",
    ]

    if network.clone_families:
        lines.append("clone families (same topic template, sizes within 25%)")
        for template, members in network.clone_families:
            lines.append(f"  {len(members)} rooms, topic '{template[:40]}'")
            lines.append(f"    {', '.join(members[:10])}")
        lines.append("")

    lines.append(f"{'room':28} {'sample':>6} {'nick':>6} {'shape':>6} {'rep':>5}  verdict")
    for entry in network.listed:
        surveyed = survey_room(client, entry["room"])
        if surveyed is None:
            continue
        lines.append(
            f"{surveyed.room[:28]:28} {surveyed.sampled:>6} "
            f"{surveyed.nick_diversity:>6.2f} {surveyed.shape_diversity:>6.2f} "
            f"{surveyed.repetition:>5.0%}  {surveyed.verdict}"
        )
    return "\n".join(lines)
