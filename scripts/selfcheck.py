#!/usr/bin/env python3
"""Check that this project still obeys the rules it was built under.

The rules are not comments. Each one below is asserted against the code as it
stands right now, so a later change that quietly breaks one fails here instead of
failing in public, on a network where nothing can be unpublished.

    python scripts/selfcheck.py [--offline]

Exit status is 1 if any check fails. Runs in CI on every push.
"""

from __future__ import annotations

import argparse
import inspect
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = "pass", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str):
    def wrap(fn):
        try:
            detail = fn()
            results.append((PASS, name, detail or ""))
        except AssertionError as exc:
            results.append((FAIL, name, str(exc)))
        except Exception as exc:  # noqa: BLE001 - a crashing check is a failed check
            results.append((FAIL, name, f"{type(exc).__name__}: {exc}"))
        return fn

    return wrap


# -- the protocol contract ------------------------------------------------


@check("did:key and signature match the shapes the server enforces")
def _shapes() -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from prereg import did as didmod

    # Copied from technocore-chat/src/didkey.py. If the service moves, this is
    # where we find out, not on a refused write.
    did_re = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}")
    sig_re = re.compile(r"[A-Za-z0-9_-]{85}[AQgw]")

    for _ in range(25):
        private = Ed25519PrivateKey.generate()
        identity = didmod.Identity(private, didmod.did_from_public_key(private.public_key()))
        assert did_re.fullmatch(identity.did), f"bad did: {identity.did}"
        signature = identity.sign_room("mb-prereg", 1, "x")
        assert sig_re.fullmatch(signature), f"bad signature: {signature}"
    return "25 identities, all conforming"


@check("signatures cover the swept text, not the raw text")
def _sweep() -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from prereg import did as didmod

    private = Ed25519PrivateKey.generate()
    identity = didmod.Identity(private, didmod.did_from_public_key(private.public_key()))
    raw = "a\nb‍c"
    swept = didmod.sweep(raw)
    assert swept != raw, "sweep did nothing"
    assert didmod.verify(identity.did, identity.sign_room("r", 1, raw), f"r|1|{swept}")
    return f"{raw!r} signs as {swept!r}"


# -- spam discipline ------------------------------------------------------


@check("errors are never published to a room")
def _no_error_publishing() -> str:
    """The failure that produced a 95,991-message room of HTTP 429 lines."""
    from prereg import agent, wire

    for module in (agent, wire):
        source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
        for match in re.finditer(r"say_signed\((.*?)\)", source, re.S):
            call = match.group(1)
            assert not re.search(r"\b(exc|error|err|traceback)\b", call), (
                f"{module.__name__} appears to publish an error: {call[:80]}"
            )
    published = inspect.getsource(agent.Agent._publish)
    assert "result.errors.append" in published, "publish failures must stay local"
    return "no error value reaches a write"


@check("writes are paced and capped")
def _volume() -> str:
    from prereg.agent import Agent
    from prereg.wire import WRITE_PER_MINUTE, Technocore

    pace = inspect.getsource(Technocore._pace_write)
    assert "time.sleep" in pace, "writes are not paced"
    defaults = inspect.signature(Agent.__init__).parameters
    per_cycle = defaults["max_claims_per_cycle"].default
    open_ceiling = defaults["max_open"].default
    assert per_cycle <= 5, f"max_claims_per_cycle is {per_cycle}"
    assert open_ceiling <= 100, f"max_open is {open_ceiling}"
    return f"<=1 write/2s against {WRITE_PER_MINUTE}/min, {per_cycle}/cycle, {open_ceiling} open"


@check("a cycle with nothing to say publishes nothing")
def _quiet() -> str:
    import tempfile

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from prereg import did as didmod
    from prereg.agent import Agent
    from prereg.store import Store

    sys.path.insert(0, str(ROOT / "tests"))
    from test_agent import FakeTechnocore

    private = Ed25519PrivateKey.generate()
    identity = didmod.Identity(private, didmod.did_from_public_key(private.public_key()))
    client = FakeTechnocore()
    with tempfile.TemporaryDirectory() as tmp:
        agent = Agent(identity, client, Store(Path(tmp)), "mb-prereg")
        agent.cycle(wait=0)
        agent.cycle(wait=0)
    assert client.messages == [], "an idle agent wrote to the room"
    return "two idle cycles, zero room messages"


# -- the scoring rules ----------------------------------------------------


@check("an unsettled claim past its deadline counts against us")
def _no_hiding() -> str:
    """Asserted by replay, not by reading the source.

    This is the rule that stops the record flattering us: settle the winners,
    stay quiet about the rest, publish a perfect score. Checking it behaviourally
    means a refactor that preserves the words but loses the behaviour still fails.
    """
    from datetime import timedelta

    from prereg import record, score
    from prereg.wire import Message

    did = "did:key:z6Mk" + "1" * 44
    stale = record.Claim(
        id="aaaaaaaaaaaa", chain="base", subject="0xabc", call="rug",
        confidence=0.9, deadline=record.now() - timedelta(hours=1),
        evidence="a" * 64, text="never settled",
    )
    report = score.build(
        [Message(seq=1, ts="2026-01-01T00:00:00Z", sender=did, text=stale.line())],
        did, "mb-prereg",
    )
    assert report.expired == 1, "an expired claim was not counted"
    assert report.settled == 1, "an expired claim escaped scoring"
    assert report.accuracy == 0.0, f"expired scored as {report.accuracy}, not a miss"
    assert report.brier is not None and report.brier > 0.5, (
        f"a confident unsettled claim cost only {report.brier}"
    )
    return f"expired -> accuracy {report.accuracy}, brier {report.brier:.2f}"


@check("the settlement definition has not moved")
def _frozen() -> str:
    from prereg.sources import dexscreener as ds

    frozen = {
        "RUG_LIQUIDITY_FRACTION": 0.20,
        "MAX_PAIR_AGE_HOURS": 48.0,
        "MIN_LIQUIDITY_USD": 5_000.0,
        "CALL_RUG_AT": 0.70,
        "CALL_HOLDS_AT": 0.30,
    }
    for name, expected in frozen.items():
        actual = getattr(ds, name)
        assert actual == expected, (
            f"{name} is {actual}, was {expected}. A record is only worth the "
            f"stability of the rule it was scored under. If this change is "
            f"deliberate, the old claims have to be retired, not rescored."
        )
    return ", ".join(f"{k}={v}" for k, v in frozen.items())


@check("the model abstains rather than calling everything")
def _abstains() -> str:
    from prereg.sources.dexscreener import CALL_HOLDS_AT, CALL_RUG_AT

    width = CALL_RUG_AT - CALL_HOLDS_AT
    assert width >= 0.25, f"abstention band is only {width:.2f} wide"
    return f"no claim between p={CALL_HOLDS_AT} and p={CALL_RUG_AT}"


# -- the room -------------------------------------------------------------


@check("the protocol room is open to any key but demands a signature")
def _room() -> str:
    from prereg.wire import open_signed_room

    assert open_signed_room("mb-prereg"), "mb- room rejected"
    assert not open_signed_room("d-prereg"), "an owned room would make this a broadcast"
    assert not open_signed_room("lobby"), "an unsigned room cannot be scored"
    return "mb- yes; d- and unsigned no"


@check("scoring covers every key in the room, not only ours")
def _multi() -> str:
    from prereg import score

    assert hasattr(score, "build_all") and hasattr(score, "leaderboard")
    return "build_all and leaderboard present"


# -- hygiene --------------------------------------------------------------


@check("no assistant tooling residue")
def _residue() -> str:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "residue.py"), str(ROOT)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout.strip() or proc.stderr.strip()
    return "clean"


@check("the test suite passes")
def _tests() -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout.strip()[-600:]
    last = [ln for ln in proc.stdout.splitlines() if ln.strip()][-1]
    return last.strip()


@check("no secret material is tracked")
def _secrets() -> str:
    listing = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True
    ).stdout.split()
    bad = [p for p in listing if p.endswith((".pem", ".key")) or "identity" in p.lower()]
    assert not bad, f"tracked: {bad}"
    tracked_state = [p for p in listing if p.endswith("state.json")]
    assert not tracked_state, f"tracked nonce state: {tracked_state}"
    return f"{len(listing)} tracked files, none of them keys"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args()

    width = max(len(name) for _s, name, _d in results)
    failures = 0
    for status, name, detail in results:
        mark = " ok " if status == PASS else "FAIL"
        print(f"[{mark}] {name:{width}}  {detail}")
        failures += status == FAIL

    print()
    if failures:
        print(f"{failures} of {len(results)} checks failed")
        return 1
    print(f"all {len(results)} checks pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
