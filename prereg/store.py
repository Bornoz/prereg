"""Local state: the nonce counter and the signature log.

The signature log exists because the server does not keep signatures. It verifies
one at write time and stores the DID it proved, not the proof itself. That is a
reasonable thing for the server to do -- it has nothing to gain from hoarding
them -- but it means a reader of the room transcript is trusting technocore.chat
rather than checking arithmetic.

So we keep every signature we produce and publish the log alongside the room. A
third party can then verify each line against our public key without trusting the
server or us. If the transcript and the log disagree, one of them is lying, and
the signature is the half that can be checked.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class SignedLine:
    room: str
    nonce: int
    did: str
    sig: str
    text: str
    seq: int | None = None
    ts: str | None = None


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.log_path = self.root / "signatures.jsonl"
        self.state_path = self.root / "state.json"

    # -- nonces ------------------------------------------------------------

    def allocate_nonce(self, did: str, scope: str) -> int:
        """Next nonce for this key in this scope, never reused.

        The server wants strictly increasing per key per room. A millisecond
        clock satisfies that on its own, but only if it never goes backwards, so
        we take the larger of the clock and the last value we handed out.
        """
        state = self._read_state()
        counters = state.setdefault("nonces", {})
        key = f"{did}|{scope}"
        clock = int(time.time() * 1000)
        nonce = max(clock, int(counters.get(key, 0)) + 1)
        counters[key] = nonce
        self._write_state(state)
        return nonce

    def last_nonce(self, did: str, scope: str) -> int:
        return int(self._read_state().get("nonces", {}).get(f"{did}|{scope}", 0))

    # -- cursors -----------------------------------------------------------

    def cursor(self, room: str) -> int:
        return int(self._read_state().get("cursors", {}).get(room, 0))

    def set_cursor(self, room: str, seq: int) -> None:
        state = self._read_state()
        cursors = state.setdefault("cursors", {})
        if seq > int(cursors.get(room, 0)):
            cursors[room] = seq
            self._write_state(state)

    # -- signature log -----------------------------------------------------

    def record(self, line: SignedLine) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(line), ensure_ascii=False) + "\n")

    def signatures(self) -> list[SignedLine]:
        if not self.log_path.exists():
            return []
        out = []
        for raw in self.log_path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(SignedLine(**json.loads(raw)))
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    # -- internals ---------------------------------------------------------

    def _read_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _write_state(self, state: dict) -> None:
        # Replace atomically: a half-written counter file loses the nonce
        # ceiling, and every later write in that room gets refused as a replay.
        temp = self.state_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(temp, self.state_path)
