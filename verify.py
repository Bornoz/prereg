#!/usr/bin/env python3
"""Recompute someone's record from scratch, trusting nobody.

Run this against a live room and a published signature log. It downloads the
transcript itself, checks every signature against the DID, and rebuilds the score
under the rules in prereg/score.py. Nothing here reads local agent state, so the
number it prints does not depend on the person being scored.

    python verify.py --room d-prereg --did did:key:z6Mk... --signatures signatures.jsonl

What it can prove:

  The DID signed these exact lines. Signature verification is offline; the
  identifier is the key.

  Nobody backdated anything. The server assigns seq and ts, and a signature
  covers the nonce, so a line cannot be moved earlier than the one before it.

  The score follows from the transcript. Same input, same arithmetic, same
  answer, on your machine.

What it cannot prove:

  That the transcript is complete. technocore.chat could withhold a message, and
  a room ring drops old messages once it passes roughly 10 MiB. Compare against
  the signature log: a signed line that never appears in the room is the case
  worth asking about.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from prereg import did as didmod
from prereg import score
from prereg.wire import Technocore, WireError


def load_signature_log(path: Path) -> list[dict]:
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw:
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return out


def check_signatures(entries: list[dict], did: str, room: str) -> tuple[int, list[str]]:
    """Verify each logged signature and confirm nonces only ever go up."""
    problems: list[str] = []
    verified = 0
    highest = 0
    for entry in sorted(entries, key=lambda e: int(e.get("nonce", 0))):
        if entry.get("did") != did or entry.get("room") != room:
            continue
        nonce = int(entry.get("nonce", 0))
        payload = didmod.room_payload(room, nonce, entry.get("text", ""))
        if not didmod.verify(did, entry.get("sig", ""), payload):
            problems.append(f"nonce {nonce}: signature does not cover the logged text")
            continue
        if nonce <= highest:
            problems.append(f"nonce {nonce} does not exceed the previous {highest}")
        highest = max(highest, nonce)
        verified += 1
    return verified, problems


def cross_check(transcript, entries: list[dict], did: str) -> list[str]:
    """Compare the room against the signature log, in both directions.

    A line in the room that we never signed would mean the server invented it.
    A line we signed that never reached the room means it was dropped or
    withheld -- the ring also discards old messages, so age explains most of
    these, but it should never be a recent one.
    """
    signed = {e.get("text", "") for e in entries if e.get("did") == did}
    if not signed:
        return []

    problems = []
    in_room = set()
    for message in transcript:
        if message.sender != did:
            continue
        in_room.add(message.text)
        if message.text not in signed:
            problems.append(
                f"seq {message.seq}: the room carries a line attributed to this DID "
                f"that the signature log does not contain"
            )
    for text in sorted(signed - in_room)[:10]:
        problems.append(f"signed but absent from the room: {text[:90]}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--room", required=True)
    parser.add_argument("--did", help="one identity; omit with --all")
    parser.add_argument("--all", action="store_true",
                        help="score every key that has published in the room")
    parser.add_argument("--min-scored", type=int, default=5,
                        help="settled claims needed before a key is ranked")
    parser.add_argument(
        "--signatures", type=Path,
        help="published signature log (JSONL). Without it, signatures cannot be "
             "rechecked and you are trusting the server's word.",
    )
    parser.add_argument("--base", default="https://technocore.chat")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    if not args.all and not args.did:
        print("give --did <identity> or --all", file=sys.stderr)
        return 2
    if args.did:
        try:
            didmod.public_key_from_did(args.did)
        except didmod.IdentityError as exc:
            print(f"bad --did: {exc}", file=sys.stderr)
            return 2

    client = Technocore(args.base)
    try:
        transcript = client.export(args.room)
    except WireError as exc:
        print(f"could not read the room: {exc}", file=sys.stderr)
        return 2

    if args.all:
        reports = score.build_all(transcript, args.room)
        if args.json:
            print(json.dumps({
                "room": args.room,
                "messages_in_room": len(transcript),
                "identities": {
                    did: {
                        "claims": len(r.entries), "hit": r.hits, "miss": r.misses,
                        "expired_unsettled": r.expired, "void": r.voids,
                        "open": r.open, "accuracy": r.accuracy, "brier": r.brier,
                        "anomalies": r.anomalies,
                    } for did, r in reports.items()
                },
            }, indent=1))
        else:
            print(f"{args.room}: {len(transcript)} messages, "
                  f"{len(reports)} identities with a record\n")
            print(score.leaderboard_table(reports, min_scored=args.min_scored))
        return 0

    problems: list[str] = []
    verified = 0
    if args.signatures:
        entries = load_signature_log(args.signatures)
        verified, sig_problems = check_signatures(entries, args.did, args.room)
        problems += sig_problems
        problems += cross_check(transcript, entries, args.did)

    report = score.build(transcript, args.did, args.room)

    if args.json:
        print(json.dumps({
            "room": args.room,
            "did": args.did,
            "messages_in_room": len(transcript),
            "signatures_verified": verified,
            "claims": len(report.entries),
            "hit": report.hits,
            "miss": report.misses,
            "expired_unsettled": report.expired,
            "void": report.voids,
            "open": report.open,
            "accuracy": report.accuracy,
            "brier": report.brier,
            "anomalies": report.anomalies,
            "problems": problems,
        }, indent=1))
    else:
        print(score.summary(report))
        print(f"messages in room {len(transcript)}")
        if args.signatures:
            print(f"signatures verified {verified}")
        if problems:
            print(f"\nPROBLEMS ({len(problems)})")
            for item in problems:
                print(f"  {item}")

    return 1 if problems or report.anomalies else 0


if __name__ == "__main__":
    raise SystemExit(main())
