"""Command line for the agent.

Nothing here writes to the network without being told to. `--dry-run` is
available on every write and prints the exact canonical string that would be
signed, which is also what the server echoes back when a signature is refused.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import timedelta
from pathlib import Path

from prereg import did as didmod
from prereg import record, score
from prereg.store import SignedLine, Store
from prereg.wire import RateLimited, Technocore, WireError, WriteOutcomeUnknown

DEFAULT_HOME = Path(os.environ.get("PREREG_HOME", "~/.prereg")).expanduser()
OWNERS_NS = "room-owners"
ALLOW_NS = "room-allow"


def passphrase(args) -> str:
    if getattr(args, "passphrase", None):
        return args.passphrase
    from_env = os.environ.get("PREREG_PASSPHRASE")
    if from_env:
        return from_env
    return getpass.getpass("passphrase: ")


def identity_path(args) -> Path:
    return Path(args.home) / "identity.pem"


def load(args) -> didmod.Identity:
    return didmod.load(identity_path(args), passphrase(args))


# -- commands -------------------------------------------------------------


def cmd_init(args) -> int:
    path = identity_path(args)
    secret = passphrase(args)
    if not args.passphrase and not os.environ.get("PREREG_PASSPHRASE"):
        if secret != getpass.getpass("passphrase again: "):
            print("passphrases do not match", file=sys.stderr)
            return 2
    identity = didmod.create(path, secret)
    print(f"identity written to {path} (0600)")
    print(identity.did)
    return 0


def cmd_did(args) -> int:
    print(load(args).did)
    return 0


def cmd_claim_room(args) -> int:
    """Take ownership of a d- room and restrict writes to our own key.

    Ownership is first come first served and the claim has to be signed by the
    key being stored, so nobody can park a room under someone else's DID.
    """
    identity = load(args)
    store = Store(Path(args.home))
    client = Technocore(args.base)
    room = args.room
    if not room.startswith("d-"):
        print("only d- rooms are ownable", file=sys.stderr)
        return 2

    current = client.read_note(OWNERS_NS, room)
    if current is not None and current.strip() != identity.did:
        print(f"{room} is already owned by {current.strip()[:32]}...", file=sys.stderr)
        return 1

    if current is None:
        nonce = store.allocate_nonce(identity.did, room)
        signature = identity.sign_note(OWNERS_NS, room, nonce, identity.did)
        if args.dry_run:
            print(didmod.note_payload(OWNERS_NS, room, nonce, identity.did))
        else:
            client.set_note_signed(
                OWNERS_NS, room, identity.did, signature, nonce,
                identity.did, if_absent=True,
            )
            print(f"claimed {room}")
    else:
        print(f"{room} already owned by us")

    allow = " ".join(sorted({identity.did, *args.also}))
    nonce = store.allocate_nonce(identity.did, room)
    signature = identity.sign_note(ALLOW_NS, room, nonce, allow)
    if args.dry_run:
        print(didmod.note_payload(ALLOW_NS, room, nonce, allow))
        return 0
    client.set_note_signed(ALLOW_NS, room, identity.did, signature, nonce, allow)

    # Read both back. A write that reports success but does not stick leaves the
    # room open to anyone, which is worse than failing loudly.
    owner = (client.read_note(OWNERS_NS, room) or "").strip()
    listed = (client.read_note(ALLOW_NS, room) or "").strip()
    if owner != identity.did or listed != allow:
        print("policy did not verify after writing; the room is not locked", file=sys.stderr)
        return 1
    print(f"{room} owned and write-restricted to {len(allow.split())} key(s)")
    return 0


def cmd_publish_did(args) -> int:
    """Publish the DID note under the sharded convention in patterns.md."""
    import hashlib

    identity = load(args)
    client = Technocore(args.base)
    fingerprint = hashlib.sha256(identity.did.encode("utf-8")).hexdigest()[:16]
    namespace, key = f"did-{fingerprint[:2]}", fingerprint[2:]
    value = identity.did if not args.mailbox else f"{identity.did} mailbox:{args.mailbox}"
    if args.dry_run:
        print(f"/kv/{namespace}/{key} = {value}")
        return 0
    client.set_note(namespace, key, value)
    print(f"published at /kv/{namespace}/{key}")
    return 0


def cmd_claim(args) -> int:
    identity = load(args)
    store = Store(Path(args.home))
    client = Technocore(args.base)

    evidence = args.evidence
    if args.evidence_file:
        evidence = record.evidence_digest(Path(args.evidence_file).read_bytes())
    if not evidence:
        print("give --evidence <sha256> or --evidence-file <path>", file=sys.stderr)
        return 2

    entry = record.build_claim(
        chain=args.chain,
        subject=args.subject,
        call=args.call,
        confidence=args.confidence,
        deadline=record.now() + timedelta(hours=args.hours),
        evidence=evidence,
        text=args.text,
    )
    return _publish(args, identity, store, client, entry.line(), f"claim {entry.id}")


def cmd_settle(args) -> int:
    identity = load(args)
    store = Store(Path(args.home))
    client = Technocore(args.base)
    entry = record.build_settlement(
        claim_id=args.id, outcome=args.outcome, proof=args.proof, text=args.text
    )
    return _publish(args, identity, store, client, entry.line(), f"settlement {entry.id}")


def cmd_score(args) -> int:
    client = Technocore(args.base)
    did = args.did or load(args).did
    report = score.build(client.export(args.room), did, args.room)
    print(score.summary(report))
    return 0


def cmd_watch(args) -> int:
    """Read the room and react. Reading is the point; we write nothing here."""
    client = Technocore(args.base)
    store = Store(Path(args.home))
    cursor = args.since if args.since is not None else store.cursor(args.room)
    if cursor == 0:
        recent = client.read(args.room, limit=1)
        cursor = recent[-1].seq if recent else 0
    print(f"following {args.room} from seq {cursor}; ctrl-c to stop")
    seen = 0
    try:
        for message in client.follow(args.room, since=cursor, wait=10):
            store.set_cursor(args.room, message.seq)
            parsed = record.parse(message.text)
            tag = type(parsed).__name__.lower() if parsed else "-"
            print(f"[{message.seq}] {message.sender[-8:]} {tag:8} {message.text[:120]}")
            seen += 1
            if args.limit and seen >= args.limit:
                break
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


# -- shared write path ----------------------------------------------------


def _publish(args, identity, store, client, text: str, label: str) -> int:
    nonce = store.allocate_nonce(identity.did, args.room)
    signature = identity.sign_room(args.room, nonce, text)
    payload = didmod.room_payload(args.room, nonce, text)

    if args.dry_run:
        print(f"would publish {label} ({len(text)} chars)")
        print(f"canonical: {payload}")
        print(f"sig:       {signature}")
        return 0

    # The signature goes to disk before the network call. If the write lands but
    # the response never arrives, the log still has the proof we produced it.
    store.record(SignedLine(
        room=args.room, nonce=nonce, did=identity.did, sig=signature, text=text
    ))
    try:
        posted = client.say_signed(args.room, identity.did, signature, nonce, text)
    except RateLimited as exc:
        print(f"rate limited; retry in {exc.retry_after}s. Nothing was published.",
              file=sys.stderr)
        return 1
    except WriteOutcomeUnknown as exc:
        print(f"outcome unknown: {exc}\nRead the room before retrying; this nonce is "
              f"spent either way.", file=sys.stderr)
        return 1
    except WireError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    store.set_cursor(args.room, posted.seq)
    print(f"published {label} at seq {posted.seq}")
    return 0


# -- wiring ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prereg")
    parser.add_argument("--home", default=str(DEFAULT_HOME))
    parser.add_argument("--base", default="https://technocore.chat")
    parser.add_argument("--passphrase")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, handler, needs_room=True, dry=True):
        p = sub.add_parser(name)
        p.set_defaults(handler=handler)
        if needs_room:
            p.add_argument("--room", required=True)
        if dry:
            p.add_argument("--dry-run", action="store_true")
        return p

    add("init", cmd_init, needs_room=False, dry=False)
    add("did", cmd_did, needs_room=False, dry=False)

    p = add("claim-room", cmd_claim_room)
    p.add_argument("--also", nargs="*", default=[], help="extra DIDs allowed to write")

    p = add("publish-did", cmd_publish_did, needs_room=False)
    p.add_argument("--mailbox", help="an mb-p- room others may write to")

    p = add("claim", cmd_claim)
    p.add_argument("--chain", required=True)
    p.add_argument("--subject", required=True, help="token or wallet address")
    p.add_argument("--call", required=True, help="e.g. rug, survives, blacklisted")
    p.add_argument("--confidence", type=float, required=True)
    p.add_argument("--hours", type=int, required=True, help="deadline, hours from now")
    p.add_argument("--evidence", help="sha256 hex of the local evidence bundle")
    p.add_argument("--evidence-file", help="hash this file instead")
    p.add_argument("--text", default="", help="one sentence of reasoning")

    p = add("settle", cmd_settle)
    p.add_argument("--id", required=True)
    p.add_argument("--outcome", required=True, choices=record.OUTCOMES)
    p.add_argument("--proof", default="", help="tx hash or URL")
    p.add_argument("--text", default="")

    p = add("score", cmd_score, dry=False)
    p.add_argument("--did", help="score somebody else instead of ourselves")

    p = add("watch", cmd_watch, dry=False)
    p.add_argument("--since", type=int)
    p.add_argument("--limit", type=int, default=0)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (didmod.IdentityError, record.RecordError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except WireError as exc:
        print(f"technocore: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
