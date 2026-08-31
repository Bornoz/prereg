"""The loop the agent actually lives in.

It runs continuously, but it does not talk continuously. Those are different
things, and conflating them is what produced the rooms full of `gm from a quiet
node` this project exists to not be.

Every cycle the agent does five things:

  reads    the room, from its last cursor, with a long poll
  settles  any of its own claims the resolver can now decide
  verifies open claims belonging to other keys, by recomputing them
  claims   whatever the source has produced since the last pass
  posts    the scoreboard to a note, under compare-and-swap

The verify step is the one that makes this a room rather than a diary. Without
somebody checking other people's work, an attestation is a statement nobody is
obliged to test.

Only claims and settlements reach the room, and both are events that actually
happened. The scoreboard lives in the key-value lane instead, where it can be
rewritten as often as the record changes without adding a line to anybody's
transcript. That is where the continuous part of the work becomes visible.

A cycle that has nothing to say publishes nothing and says so in the local log.
Silence here is the correct output, not a failure.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from prereg import did as didmod
from prereg import record, score
from prereg.store import SignedLine, Store
from prereg.wire import RateLimited, Technocore, WireError, WriteOutcomeUnknown

log = logging.getLogger("prereg.agent")

SCOREBOARD_NS = "prereg"


@dataclass(frozen=True)
class ClaimDraft:
    """What a detector hands over. The agent owns ids, deadlines and signing."""

    domain: str
    subject: str
    call: str
    confidence: float
    horizon: timedelta
    evidence: bytes
    text: str


class ClaimSource(Protocol):
    def pending(self) -> list[ClaimDraft]:
        """Drafts produced since the last call. Must not repeat a subject."""


class OutcomeResolver(Protocol):
    def resolve(self, claim: record.Claim) -> tuple[str, str, str] | None:
        """Return (outcome, proof, note) once the chain has decided, else None."""


@dataclass
class CycleResult:
    read: int = 0
    claimed: list[str] = field(default_factory=list)
    settled: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)
    scoreboard_written: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def quiet(self) -> bool:
        return not (self.claimed or self.settled or self.verified
                    or self.scoreboard_written)


class Agent:
    def __init__(
        self,
        identity: didmod.Identity,
        client: Technocore,
        store: Store,
        room: str,
        source: ClaimSource | None = None,
        resolver: OutcomeResolver | None = None,
        max_open: int = 40,
        max_claims_per_cycle: int = 3,
        verifier: OutcomeResolver | None = None,
        max_verifications_per_cycle: int = 3,
        dry_run: bool = False,
    ) -> None:
        self.identity = identity
        self.client = client
        self.store = store
        self.room = room
        self.source = source
        self.resolver = resolver
        self.max_open = max_open
        self.max_claims_per_cycle = max_claims_per_cycle
        # Settles claims made by *other* keys. This is the validator half of the
        # room: without somebody checking other people's work, an attestation
        # domain has nobody to be accountable to.
        self.verifier = verifier
        self.max_verifications_per_cycle = max_verifications_per_cycle
        # Everything except the writes. The first runs of a new deployment use
        # this to prove the whole path works before anything reaches the room,
        # because a claim cannot be unpublished.
        self.dry_run = dry_run
        self._scoreboard_cache: str | None = None

    # -- one pass ----------------------------------------------------------

    def cycle(self, wait: int = 10) -> CycleResult:
        result = CycleResult()

        transcript = self._read(result, wait)
        if transcript is None:
            return result

        report = score.build(transcript, self.identity.did, self.room)
        self._settle(report, result)
        self._verify(transcript, result)
        self._claim(report, result)

        # Anything published above changed the record, and a scoreboard built
        # from the pre-publish report would be one cycle behind for as long as
        # the agent keeps working. Re-read before writing it.
        if result.claimed or result.settled or result.verified:
            report = score.build(
                self.client.export(self.room), self.identity.did, self.room
            )
        self._scoreboard(report, result)
        return result

    def run(self, interval: int = 60, cycles: int | None = None) -> None:
        """Loop until stopped. `cycles` bounds it for tests and dry runs."""
        done = 0
        while cycles is None or done < cycles:
            started = time.monotonic()
            try:
                result = self.cycle()
            except RateLimited as exc:
                # Back off and stay silent. Publishing the fact that we were
                # throttled is how a bot turns a rate limit into 95,991 messages.
                log.warning("rate limited, sleeping %ss", exc.retry_after)
                time.sleep(exc.retry_after)
                continue
            except WireError as exc:
                log.warning("transport: %s", exc)
                time.sleep(min(interval, 60))
                continue

            log.info(
                "cycle read=%d claimed=%d settled=%d verified=%d scoreboard=%s%s",
                result.read, len(result.claimed), len(result.settled),
                len(result.verified),
                result.scoreboard_written,
                " quiet" if result.quiet else "",
            )
            for problem in result.errors:
                log.warning("  %s", problem)

            done += 1
            elapsed = time.monotonic() - started
            if cycles is None or done < cycles:
                time.sleep(max(0.0, interval - elapsed))

    # -- steps -------------------------------------------------------------

    def _read(self, result: CycleResult, wait: int) -> list | None:
        """Long-poll first so the cycle is driven by the room, not the clock."""
        cursor = self.store.cursor(self.room)
        if cursor:
            fresh = self.client.read(self.room, since=cursor, wait=wait)
            result.read = len(fresh)
            for message in fresh:
                self.store.set_cursor(self.room, message.seq)
        # The score has to come from the whole retained transcript, not the tail.
        transcript = self.client.export(self.room)
        if transcript:
            self.store.set_cursor(self.room, max(m.seq for m in transcript))
        return transcript

    def _settle(self, report: score.Report, result: CycleResult) -> None:
        if self.resolver is None:
            return
        for entry in report.entries:
            if entry.settlement is not None:
                continue
            decided = self.resolver.resolve(entry.claim)
            if decided is None:
                continue
            outcome, proof, note = decided
            try:
                settlement = record.build_settlement(
                    entry.claim.id, outcome, proof, note
                )
            except record.RecordError as exc:
                result.errors.append(f"settlement {entry.claim.id}: {exc}")
                continue
            if self._publish(settlement.line(), result):
                result.settled.append(entry.claim.id)

    def _verify(self, transcript: list, result: CycleResult) -> None:
        """Settle open claims belonging to other keys."""
        if self.verifier is None:
            return
        for did, other in score.build_all(transcript, self.room).items():
            if did == self.identity.did:
                continue
            for entry in other.entries:
                if len(result.verified) >= self.max_verifications_per_cycle:
                    return
                if entry.settlement is not None:
                    continue
                decided = self.verifier.resolve(entry.claim)
                if decided is None:
                    continue
                outcome, proof, note = decided
                try:
                    settlement = record.build_settlement(
                        entry.claim.id, outcome, proof, note
                    )
                except record.RecordError as exc:
                    result.errors.append(f"verification {entry.claim.id}: {exc}")
                    continue
                if self._publish(settlement.line(), result):
                    result.verified.append(entry.claim.id)

    def _claim(self, report: score.Report, result: CycleResult) -> None:
        if self.source is None:
            return
        room_for_more = self.max_open - report.open
        if room_for_more <= 0:
            log.info("holding: %d open claims already", report.open)
            return

        drafts = self.source.pending()[: min(self.max_claims_per_cycle, room_for_more)]
        seen = {e.claim.subject for e in report.entries}
        for draft in drafts:
            if draft.subject in seen:
                continue
            try:
                claim = record.build_claim(
                    domain=draft.domain,
                    subject=draft.subject,
                    call=draft.call,
                    confidence=draft.confidence,
                    deadline=record.now() + draft.horizon,
                    evidence=record.evidence_digest(draft.evidence),
                    text=draft.text,
                )
            except record.RecordError as exc:
                result.errors.append(f"draft {draft.subject}: {exc}")
                continue
            # Stored before the write, so that a publish whose response is lost
            # still has the bundle behind its digest. A dry run writes nothing
            # anywhere, disk included.
            if not self.dry_run:
                self.store.save_evidence(claim.id, draft.evidence)
            if self._publish(claim.line(), result):
                result.claimed.append(claim.id)
                seen.add(draft.subject)

    def _scoreboard(self, report: score.Report, result: CycleResult) -> None:
        """Publish the current record to a note, with compare-and-swap.

        The note carries no authority on its own. It is a convenience so a reader
        does not have to replay the room, and verify.py ignores it entirely.
        """
        line = _scoreboard_line(report)
        if self.dry_run:
            log.info("would set scoreboard note: %s", line)
            return
        if line == self._scoreboard_cache:
            return
        key = _fingerprint(self.identity.did)
        try:
            current = self.client.read_note(SCOREBOARD_NS, key)
            if current is not None and current.strip() == line:
                self._scoreboard_cache = line
                return
            self.client.set_note(
                SCOREBOARD_NS, key, line,
                if_value=current if current is not None else None,
                if_absent=current is None,
            )
        except WireError as exc:
            # A 409 means somebody else moved it, which for our own key means the
            # previous write landed after all. Re-read next cycle rather than
            # forcing it.
            result.errors.append(f"scoreboard: {exc}")
            self._scoreboard_cache = None
            return
        self._scoreboard_cache = line
        result.scoreboard_written = True

    def _publish(self, text: str, result: CycleResult) -> bool:
        if self.dry_run:
            log.info("would publish (%d chars): %s", len(text), text)
            return False
        nonce = self.store.allocate_nonce(self.identity.did, self.room)
        signature = self.identity.sign_room(self.room, nonce, text)
        self.store.record(SignedLine(
            room=self.room, nonce=nonce, did=self.identity.did,
            sig=signature, text=text,
        ))
        try:
            posted = self.client.say_signed(
                self.room, self.identity.did, signature, nonce, text
            )
        except WriteOutcomeUnknown as exc:
            result.errors.append(f"outcome unknown, will reconcile on read: {exc}")
            return False
        except (RateLimited, WireError) as exc:
            result.errors.append(f"refused: {exc}")
            return False
        self.store.set_cursor(self.room, posted.seq)
        return True


def _scoreboard_line(report: score.Report) -> str:
    accuracy = "n/a" if report.accuracy is None else f"{report.accuracy:.3f}"
    brier = "n/a" if report.brier is None else f"{report.brier:.4f}"
    return (
        f"prereg/1 record claims={len(report.entries)} hit={report.hits} "
        f"miss={report.misses} expired={report.expired} void={report.voids} "
        f"open={report.open} acc={accuracy} brier={brier} "
        f"at={record.format_time(record.now())}"
    )


def _fingerprint(did: str) -> str:
    import hashlib

    return hashlib.sha256(did.encode("utf-8")).hexdigest()[:16]


def _server_time(raw: str) -> datetime | None:
    """Parse the `ts` the server stamps on a message.

    Deliberately tolerant, unlike record.parse_time, which pins the format we
    write ourselves. The server's own stamp carries microseconds -- reading it
    with the strict parser silently produced "no timestamp", which made every
    liveness check answer STALE no matter how recently the agent had posted.
    """
    from datetime import timezone

    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def liveness(client: Technocore, did: str, room: str, stale_after_minutes: int = 90) -> dict:
    """Ask Technocore whether this agent is alive, from the outside.

    Deliberately reads nothing local. After a deploy this is the only honest way
    to answer "is it running": not whether the process is up on some box, but
    whether the network can see it working.
    """
    transcript = client.export(room)
    ours = [m for m in transcript if m.sender == did]
    report = score.build(transcript, did, room)

    last_ts = _server_time(ours[-1].ts) if ours else None

    age = None if last_ts is None else (record.now() - last_ts).total_seconds() / 60
    note = client.read_note(SCOREBOARD_NS, _fingerprint(did))

    return {
        "room": room,
        "did": did,
        "messages_in_room": len(transcript),
        "ours": len(ours),
        "last_seq": ours[-1].seq if ours else None,
        "last_ts": ours[-1].ts if ours else None,
        "minutes_since_last": None if age is None else round(age, 1),
        "stale": age is None or age > stale_after_minutes,
        "claims": len(report.entries),
        "open": report.open,
        "scoreboard_note": (note or "").strip() or None,
    }
