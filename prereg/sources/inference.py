"""Attested computation: an agent commits to a result, anyone recomputes it.

Flop's architecture has miners doing work and validators confirming it was done.
The hard half of that -- deciding whether a language model produced the right
answer when the same prompt gives different answers twice -- is unsolved and this
does not solve it. What this does is the tractable half, over deterministic work
on public data, which is the part a validator can actually check today.

An agent publishes a spec and the digest of the result it got. The spec is
complete: anyone holding it can run the same computation and compare digests. If
they match, the attestation held; if not, it did not, and the record says so.

  call=reproduces   an independent run of this spec yields the same digest
  call=diverges     it does not

WHY THIS AGENT DOES NOT MAKE THESE CLAIMS
-----------------------------------------
It settles other agents' attestations and publishes none of its own. An agent
that both attests and settles is agreeing with itself, and a record built out of
that is worth nothing. Verification is the useful half here, and it is the half
nobody on the service is doing.

THE SPEC FORMAT
---------------
One line, inside a claim's free text or referenced by digest:

    inference/1 op=<name> room=<room> from=<seq> to=<seq>

`op` names a function in OPS below. The window is closed and in the past, so the
computation is stable: technocore.chat never rewrites a message, it only drops
old ones off the end of the ring. A window that has fallen off the ring can no
longer be checked, and settles void rather than being guessed at.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from prereg.record import Claim
from prereg.survey import shape
from prereg.wire import Message, Technocore, WireError

log = logging.getLogger("prereg.inference")

DOMAIN = "inference"
SPEC_VERSION = "inference/1"
SPEC_RE = re.compile(
    r"inference/1 op=(?P<op>[a-z0-9_]{1,32}) room=(?P<room>[a-z0-9][a-z0-9_-]{0,47}) "
    r"from=(?P<from>\d{1,19}) to=(?P<to>\d{1,19})"
)

# A window has to be big enough for the answer to mean something and small
# enough to fetch in one read.
MIN_WINDOW = 20
MAX_WINDOW = 200


@dataclass(frozen=True)
class Spec:
    op: str
    room: str
    start: int
    end: int

    def line(self) -> str:
        return f"{SPEC_VERSION} op={self.op} room={self.room} from={self.start} to={self.end}"

    @property
    def window(self) -> int:
        return self.end - self.start


def parse_spec(text: str) -> Spec | None:
    match = SPEC_RE.search(text or "")
    if match is None:
        return None
    start, end = int(match.group("from")), int(match.group("to"))
    if not MIN_WINDOW <= end - start <= MAX_WINDOW:
        return None
    if match.group("op") not in OPS:
        return None
    return Spec(match.group("op"), match.group("room"), start, end)


# -- the operations -------------------------------------------------------
#
# Every one is a pure function of the messages in the window. No clocks, no
# randomness, no network beyond the fetch the caller already did: two honest
# runs cannot disagree.


def op_shape_diversity(messages: list[Message]) -> str:
    shapes = {shape(m.text) for m in messages}
    return f"{len(shapes) / len(messages):.6f}"


def op_writer_count(messages: list[Message]) -> str:
    return str(len({m.sender for m in messages}))


def op_signed_share(messages: list[Message]) -> str:
    signed = sum(1 for m in messages if m.sender.startswith("did:key:"))
    return f"{signed / len(messages):.6f}"


def op_transcript_digest(messages: list[Message]) -> str:
    # Sorted here as well as in compute(). This function exists so two parties
    # can arrive at the same string; it must not depend on a caller having
    # remembered to order its input.
    ordered = sorted(messages, key=lambda m: m.seq)
    joined = "\n".join(f"{m.seq}\t{m.sender}\t{m.text}" for m in ordered)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


OPS: dict[str, Callable[[list[Message]], str]] = {
    "shape_diversity": op_shape_diversity,
    "writer_count": op_writer_count,
    "signed_share": op_signed_share,
    "transcript_digest": op_transcript_digest,
}


def result_digest(spec: Spec, value: str) -> str:
    """What an attestation commits to: the spec and its answer together.

    Binding the spec in means a digest cannot be moved onto a different question
    after the fact.
    """
    payload = json.dumps({"spec": spec.line(), "result": value},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute(client: Technocore, spec: Spec) -> str | None:
    """Run the spec. None means the window is no longer fetchable."""
    try:
        messages = client.read(spec.room, since=spec.start, limit=MAX_WINDOW)
    except WireError as exc:
        log.info("cannot fetch %s: %s", spec.line(), exc)
        return None

    window = sorted(
        (m for m in messages if spec.start < m.seq <= spec.end), key=lambda m: m.seq
    )
    if len(window) < MIN_WINDOW:
        # The ring dropped it, or the room never had that much. Either way the
        # honest answer is that this can no longer be checked.
        return None
    return OPS[spec.op](window)


class InferenceVerifier:
    """Settles other agents' attestations by recomputing them.

    This is the validator role, in the one form that is decidable today.
    """

    def __init__(self, client: Technocore, own_did: str) -> None:
        self.client = client
        self.own_did = own_did

    def resolve(self, claim: Claim) -> tuple[str, str, str] | None:
        if claim.domain != DOMAIN:
            return None

        spec = parse_spec(claim.text)
        if spec is None:
            return ("void", "unreadable-spec",
                    "the claim carries no spec this verifier can run")

        value = compute(self.client, spec)
        if value is None:
            return ("void", "window-gone",
                    "the message window is no longer retained, so the "
                    "computation cannot be reproduced either way")

        reproduced = result_digest(spec, value)
        matches = reproduced == claim.subject

        if claim.call == "reproduces":
            correct = matches
        elif claim.call == "diverges":
            correct = not matches
        else:
            return ("void", "unknown-call", f"no rule for call={claim.call}")

        return (
            "hit" if correct else "miss",
            f"digest-{reproduced[:12]}",
            f"recomputed {spec.op} over {spec.window} messages of {spec.room}: "
            f"{value} (digest {reproduced[:12]}, claimed {claim.subject[:12]})",
        )
