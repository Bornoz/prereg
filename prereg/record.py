"""The line format for a pre-registered claim and its settlement.

A claim is published before the outcome is known and names a deadline. A
settlement is published afterwards and says what happened. Both are one line,
signed, and fit inside the 4096-character message cap.

The deadline is what makes the record honest. Anyone can publish predictions and
quietly settle only the ones that came good; a claim that passes its deadline
with no settlement is scored as a miss, so there is no silence to hide in.

Machine fields come first and the human sentence last, after a `--` marker, so a
parser never has to guess where free text begins.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

VERSION = "prereg/1"
CLAIM = "claim"
SETTLE = "settle"

OUTCOMES = ("hit", "miss", "void")

ID_RE = re.compile(r"[0-9a-f]{12}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
WORD_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")
SUBJECT_RE = re.compile(r"[A-Za-z0-9:._-]{1,90}")

MAX_TEXT = 4096


class RecordError(ValueError):
    pass


def new_id() -> str:
    return secrets.token_hex(6)


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(raw: str) -> datetime:
    if not TIME_RE.fullmatch(raw):
        raise RecordError(f"not an ISO-8601 UTC timestamp: {raw!r}")
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def evidence_digest(payload: bytes) -> str:
    """SHA-256 of the private evidence bundle behind a claim.

    The bundle stays local. Publishing its digest at claim time means we can hand
    it over later and anyone can check it is the same bundle we already committed
    to, without us having to give away how the call was made up front.
    """
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class Claim:
    id: str
    chain: str
    subject: str
    call: str
    confidence: float
    deadline: datetime
    evidence: str
    text: str

    def line(self) -> str:
        return _join(
            [
                VERSION,
                CLAIM,
                f"id={self.id}",
                f"chain={self.chain}",
                f"subject={self.subject}",
                f"call={self.call}",
                f"conf={self.confidence:.2f}",
                f"by={format_time(self.deadline)}",
                f"ev={self.evidence}",
            ],
            self.text,
        )


@dataclass(frozen=True)
class Settlement:
    id: str
    outcome: str
    at: datetime
    proof: str
    text: str

    def line(self) -> str:
        return _join(
            [
                VERSION,
                SETTLE,
                f"id={self.id}",
                f"outcome={self.outcome}",
                f"at={format_time(self.at)}",
                f"proof={self.proof}",
            ],
            self.text,
        )


def build_claim(
    chain: str, subject: str, call: str, confidence: float,
    deadline: datetime, evidence: str, text: str, claim_id: str | None = None,
) -> Claim:
    claim = Claim(
        id=claim_id or new_id(),
        chain=_word("chain", chain),
        subject=_subject(subject),
        call=_word("call", call),
        confidence=_confidence(confidence),
        deadline=deadline,
        evidence=_digest(evidence),
        text=_text(text),
    )
    if deadline <= now():
        raise RecordError("a claim deadline must be in the future")
    _check_length(claim.line())
    return claim


def build_settlement(
    claim_id: str, outcome: str, proof: str, text: str, at: datetime | None = None,
) -> Settlement:
    if not ID_RE.fullmatch(claim_id):
        raise RecordError(f"not a claim id: {claim_id!r}")
    if outcome not in OUTCOMES:
        raise RecordError(f"outcome must be one of {', '.join(OUTCOMES)}")
    settlement = Settlement(
        id=claim_id,
        outcome=outcome,
        at=at or now(),
        proof=_word("proof", proof) if proof else "none",
        text=_text(text),
    )
    if outcome == "void" and len(settlement.text) < 12:
        raise RecordError("a void settlement has to say why, in the free text")
    _check_length(settlement.line())
    return settlement


def parse(line: str) -> Claim | Settlement | None:
    """Return the record a line carries, or None if it is not one of ours.

    Anything malformed is None rather than an exception: the room is world
    writable, so most lines will not be ours and a stranger's junk must not stop
    a replay.
    """
    head, _, text = line.partition(" -- ")
    parts = head.split()
    if len(parts) < 3 or parts[0] != VERSION:
        return None
    kind = parts[1]
    fields: dict[str, str] = {}
    for token in parts[2:]:
        name, sep, value = token.partition("=")
        if not sep:
            return None
        fields[name] = value
    try:
        if kind == CLAIM:
            return Claim(
                id=_required(fields, "id", ID_RE),
                chain=_required(fields, "chain", WORD_RE),
                subject=_required(fields, "subject", SUBJECT_RE),
                call=_required(fields, "call", WORD_RE),
                confidence=_confidence(float(fields["conf"])),
                deadline=parse_time(fields["by"]),
                evidence=_required(fields, "ev", DIGEST_RE),
                text=text.strip(),
            )
        if kind == SETTLE:
            outcome = fields.get("outcome", "")
            if outcome not in OUTCOMES:
                return None
            return Settlement(
                id=_required(fields, "id", ID_RE),
                outcome=outcome,
                at=parse_time(fields["at"]),
                proof=fields.get("proof", "none"),
                text=text.strip(),
            )
    except (KeyError, ValueError, RecordError):
        return None
    return None


def _join(fields: list[str], text: str) -> str:
    line = " ".join(fields)
    return f"{line} -- {text}" if text else line


def _required(fields: dict[str, str], name: str, pattern: re.Pattern[str]) -> str:
    value = fields[name]
    if not pattern.fullmatch(value):
        raise RecordError(f"{name}={value!r} does not match {pattern.pattern}")
    return value


def _word(name: str, value: str) -> str:
    value = value.strip().lower()
    if not WORD_RE.fullmatch(value):
        raise RecordError(f"{name} must be 1-40 chars of [a-z0-9._-], got {value!r}")
    return value


def _subject(value: str) -> str:
    value = value.strip()
    if not SUBJECT_RE.fullmatch(value):
        raise RecordError(f"subject must be 1-90 chars without spaces, got {value!r}")
    return value


def _confidence(value: float) -> float:
    if not 0.0 <= value <= 1.0:
        raise RecordError(f"confidence must be between 0 and 1, got {value}")
    return round(value, 2)


def _digest(value: str) -> str:
    value = value.strip().lower()
    if not DIGEST_RE.fullmatch(value):
        raise RecordError("evidence must be a 64-character SHA-256 hex digest")
    return value


def _text(value: str) -> str:
    value = " ".join(value.split())
    if " -- " in value:
        raise RecordError("free text cannot contain the ' -- ' separator")
    return value


def _check_length(line: str) -> None:
    if len(line) > MAX_TEXT:
        raise RecordError(f"record is {len(line)} characters; the cap is {MAX_TEXT}")
