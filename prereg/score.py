"""Turn a room transcript into a record, under rules fixed in advance.

Two of these rules are the ones that stop the record from flattering us:

An unsettled claim past its deadline is a miss. Otherwise a forecaster settles
the winners, stays quiet about the rest, and posts a perfect score.

Confidence is scored, not just direction. A call at 0.55 that lands is worth less
than the same call at 0.95, and a confident miss costs more than a hesitant one.
That is what the Brier score measures, and it is the number that punishes writing
0.99 on everything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from prereg.record import Claim, Settlement, now, parse
from prereg.wire import Message

OPEN = "open"
EXPIRED = "expired"


@dataclass
class Entry:
    claim: Claim
    claim_seq: int
    claim_ts: str
    settlement: Settlement | None = None
    settlement_seq: int | None = None

    @property
    def state(self) -> str:
        if self.settlement is not None:
            return self.settlement.outcome
        return EXPIRED if self.claim.deadline <= now() else OPEN

    @property
    def scored(self) -> bool:
        """Expired-unsettled counts against us exactly like a miss."""
        return self.state in ("hit", "miss", EXPIRED)

    @property
    def correct(self) -> bool:
        return self.state == "hit"


@dataclass
class Report:
    did: str
    room: str
    entries: list[Entry] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    @property
    def hits(self) -> int:
        return sum(1 for e in self.entries if e.state == "hit")

    @property
    def misses(self) -> int:
        return sum(1 for e in self.entries if e.state == "miss")

    @property
    def expired(self) -> int:
        return sum(1 for e in self.entries if e.state == EXPIRED)

    @property
    def voids(self) -> int:
        return sum(1 for e in self.entries if e.state == "void")

    @property
    def open(self) -> int:
        return sum(1 for e in self.entries if e.state == OPEN)

    @property
    def settled(self) -> int:
        return sum(1 for e in self.entries if e.scored)

    @property
    def accuracy(self) -> float | None:
        if not self.settled:
            return None
        return self.hits / self.settled

    @property
    def brier(self) -> float | None:
        """Mean squared error of the confidences. Lower is better; 0.25 is a coin."""
        scored = [e for e in self.entries if e.scored]
        if not scored:
            return None
        total = sum((e.claim.confidence - (1.0 if e.correct else 0.0)) ** 2 for e in scored)
        return total / len(scored)

    @property
    def void_rate(self) -> float | None:
        closed = self.settled + self.voids
        if not closed:
            return None
        return self.voids / closed


def build(messages: list[Message], did: str, room: str, at: datetime | None = None) -> Report:
    """Replay a transcript in sequence order and rebuild the record.

    Only messages the server attributed to `did` count. Everything else in the
    room is somebody else's, and a room is world writable.
    """
    report = Report(did=did, room=room)
    by_id: dict[str, Entry] = {}

    for message in sorted(messages, key=lambda m: m.seq):
        if message.sender != did:
            continue
        record = parse(message.text)
        if record is None:
            continue

        if isinstance(record, Claim):
            if record.id in by_id:
                report.anomalies.append(
                    f"seq {message.seq}: claim id {record.id} was already used at "
                    f"seq {by_id[record.id].claim_seq}"
                )
                continue
            by_id[record.id] = Entry(
                claim=record, claim_seq=message.seq, claim_ts=message.ts
            )

        elif isinstance(record, Settlement):
            entry = by_id.get(record.id)
            if entry is None:
                report.anomalies.append(
                    f"seq {message.seq}: settlement for unknown claim {record.id}"
                )
                continue
            if entry.settlement is not None:
                report.anomalies.append(
                    f"seq {message.seq}: claim {record.id} was already settled at "
                    f"seq {entry.settlement_seq}; ignoring the later one"
                )
                continue
            if message.seq <= entry.claim_seq:
                report.anomalies.append(
                    f"seq {message.seq}: settlement precedes its claim {record.id}"
                )
                continue
            entry.settlement = record
            entry.settlement_seq = message.seq

    report.entries = sorted(by_id.values(), key=lambda e: e.claim_seq)
    return report


def summary(report: Report) -> str:
    lines = [
        f"room     {report.room}",
        f"identity {report.did}",
        f"claims   {len(report.entries)}",
        f"  hit     {report.hits}",
        f"  miss    {report.misses}",
        f"  expired {report.expired}  (unsettled past deadline, scored as miss)",
        f"  void    {report.voids}",
        f"  open    {report.open}",
    ]
    accuracy = report.accuracy
    brier = report.brier
    lines.append(
        f"accuracy {accuracy:.3f} over {report.settled} scored"
        if accuracy is not None
        else "accuracy n/a (nothing scored yet)"
    )
    lines.append(f"brier    {brier:.4f}" if brier is not None else "brier    n/a")
    rate = report.void_rate
    if rate is not None and rate > 0.15:
        lines.append(f"WARNING  void rate is {rate:.0%}; voids should be rare")
    if report.anomalies:
        lines.append(f"anomalies {len(report.anomalies)}")
        lines.extend(f"  {item}" for item in report.anomalies)
    return "\n".join(lines)
