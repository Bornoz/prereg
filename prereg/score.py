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

# Domains where the claimant may settle its own claim, because settlement is
# mechanical against data outside the room and anybody can recheck it.
SELF_SETTLING = ("network", "dex-liquidity")

# Domains where a settlement only counts from a different key. An agent that
# both attests and settles is agreeing with itself, and a record built out of
# that is worth nothing.
INDEPENDENT_ONLY = ("inference",)


@dataclass
class Entry:
    claim: Claim
    claim_seq: int
    claim_ts: str
    claim_did: str = ""
    settlement: Settlement | None = None
    settlement_seq: int | None = None
    settled_by: str | None = None

    @property
    def independently_settled(self) -> bool:
        """Settled by a key other than the one that made the claim."""
        return self.settled_by is not None and self.settled_by != self.claim_did

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
    """Replay a transcript in sequence order and rebuild one key's record.

    Claims count only from `did`. Settlements count from anybody, which is what
    makes this a ledger rather than a set of private diaries -- and for the
    domains in INDEPENDENT_ONLY, a settlement from the claimant is refused
    outright and recorded as an anomaly.
    """
    report = Report(did=did, room=room)
    by_id: dict[str, Entry] = {}

    for message in sorted(messages, key=lambda m: m.seq):
        record = parse(message.text)
        if record is None:
            continue
        # Claims are only this key's. Settlements may come from anybody: that is
        # what turns the room from a set of private diaries into one ledger.
        if isinstance(record, Claim) and message.sender != did:
            continue

        if isinstance(record, Claim):
            if record.id in by_id:
                report.anomalies.append(
                    f"seq {message.seq}: claim id {record.id} was already used at "
                    f"seq {by_id[record.id].claim_seq}"
                )
                continue
            by_id[record.id] = Entry(
                claim=record, claim_seq=message.seq, claim_ts=message.ts,
                claim_did=did,
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
            if entry.claim.domain in INDEPENDENT_ONLY and message.sender == did:
                report.anomalies.append(
                    f"seq {message.seq}: {entry.claim.domain} claim {record.id} "
                    f"cannot be settled by the key that made it"
                )
                continue
            entry.settlement = record
            entry.settlement_seq = message.seq
            entry.settled_by = message.sender

    report.entries = sorted(by_id.values(), key=lambda e: e.claim_seq)
    return report


def build_all(messages: list[Message], room: str) -> dict[str, Report]:
    """Score every key that has published a record, not just our own.

    This is what makes the room worth having. A single agent keeping its own
    score needs no shared substrate; it could publish anywhere. A room where any
    key can commit to a claim and every key is scored under the same replay
    needs exactly one ordered, signed, append-only log that nobody owns.
    """
    senders = {
        message.sender
        for message in messages
        if message.sender.startswith("did:key:") and parse(message.text) is not None
    }
    reports = {did: build(messages, did, room) for did in senders}
    return {did: r for did, r in reports.items() if r.entries}


def leaderboard(reports: dict[str, Report], min_scored: int = 5) -> list[Report]:
    """Ranked by Brier, which is the only one of these numbers that cannot be
    gamed by picking easy calls: it charges for confidence as well as direction.

    Keys with too little settled history are listed but not ranked. A record of
    one lucky call is not a record.
    """
    ranked = [r for r in reports.values() if r.settled >= min_scored and r.brier is not None]
    ranked.sort(key=lambda r: (r.brier, -r.settled))
    return ranked


def leaderboard_table(reports: dict[str, Report], min_scored: int = 5) -> str:
    ranked = leaderboard(reports, min_scored)
    unranked = [r for r in reports.values() if r not in ranked]

    lines = [f"{'#':>2}  {'identity':22} {'brier':>7} {'acc':>6} {'scored':>6} {'open':>5}"]
    for position, report in enumerate(ranked, 1):
        lines.append(
            f"{position:>2}  {_short(report.did):22} {report.brier:>7.4f} "
            f"{report.accuracy:>6.3f} {report.settled:>6} {report.open:>5}"
        )
    if not ranked:
        lines.append("    (nobody has settled enough claims to rank yet)")
    for report in sorted(unranked, key=lambda r: -len(r.entries)):
        lines.append(
            f" -  {_short(report.did):22} {'-':>7} {'-':>6} {report.settled:>6} "
            f"{report.open:>5}  below the {min_scored}-claim floor"
        )
    return "\n".join(lines)


def _short(did: str) -> str:
    body = did.removeprefix("did:key:")
    return f"{body[:8]}...{body[-6:]}"


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
