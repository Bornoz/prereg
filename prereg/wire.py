"""HTTP client for technocore.chat.

Two rules shape this file.

Writes never carry our own failures. There is a room on the live service with
95,991 messages in it, 97% of which are the operator's translation bot posting
`[HTTP Error 429: Too Many Requests]` back into the channel it was rate limited
by. Every error here is raised to the caller and logged locally; nothing about a
failure is ever published.

Nonces count up per key per room, and the server refuses a repeat. We allocate
them from a local counter that only moves forward, and a write whose outcome we
could not read is reported as unknown rather than retried blindly -- retrying a
write that actually landed burns the nonce and looks like a replay attempt.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_BASE = "https://technocore.chat"

# The server prefixes every note read with a two-line banner warning that the
# content is caller-written. It is not part of the stored value, and a
# compare-and-swap that keeps it will never match. Strip it on the way in.
UNTRUSTED_MARK = "!! UNTRUSTED CONTENT"
USER_AGENT = "prereg/0.1 (+https://github.com/Bornoz/prereg)"

# Published defaults at /config. We stay well under them; these are only used to
# decide how long to wait when the server pushes back.
READ_PER_MINUTE = 120
WRITE_PER_MINUTE = 30


class WireError(Exception):
    pass


class RateLimited(WireError):
    def __init__(self, retry_after: int, body: str) -> None:
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after
        self.body = body


class WriteOutcomeUnknown(WireError):
    """The request may or may not have committed. Reconcile before retrying."""


class ServiceUnavailable(WireError):
    """A 502/503/504 from the service or its proxy. The service is down, not us.

    Reads raise it so a cycle can log and wait; writes raise WriteOutcomeUnknown
    instead, because a 503 from a proxy in front of the write cannot promise the
    write did not reach the origin.
    """


@dataclass(frozen=True)
class Message:
    seq: int
    ts: str
    sender: str
    text: str
    nonce: int | None = None

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Message:
        return cls(
            seq=int(record["seq"]),
            ts=str(record["ts"]),
            sender=str(record.get("from", "")),
            text=str(record.get("text", "")),
            nonce=int(record["nonce"]) if record.get("nonce") is not None else None,
        )


class Technocore:
    def __init__(self, base: str = DEFAULT_BASE, timeout: float = 20.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._last_write = 0.0

    # -- transport ---------------------------------------------------------

    def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> tuple[int, str]:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("User-Agent", USER_AGENT)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", "replace")
            if exc.code == 429:
                raise RateLimited(_retry_after(exc, payload), payload) from None
            if exc.code in (502, 503, 504):
                if method == "POST":
                    raise WriteOutcomeUnknown(
                        f"{method} {path} -> {exc.code}: service unavailable") from None
                raise ServiceUnavailable(
                    f"{method} {path} -> {exc.code}: service unavailable") from None
            raise WireError(f"{method} {path} -> {exc.code}: {payload[:300]}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            if method == "POST":
                raise WriteOutcomeUnknown(f"{method} {path}: {exc}") from None
            raise WireError(f"{method} {path}: {exc}") from None

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        _status, payload = self._request(method, path, **kwargs)
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            raise WireError(f"{path} did not return JSON: {payload[:200]}") from None

    def _pace_write(self) -> None:
        """Keep writes at most one per two seconds regardless of the burst budget.

        The limit is 30/minute. Volume is not what we are selling, so there is no
        reason to sit anywhere near it.
        """
        gap = time.monotonic() - self._last_write
        if gap < 2.0:
            time.sleep(2.0 - gap)
        self._last_write = time.monotonic()

    # -- reads -------------------------------------------------------------

    def read(
        self, room: str, since: int | None = None, limit: int = 50, wait: int = 0
    ) -> list[Message]:
        params: dict[str, Any] = {"format": "json", "limit": limit}
        if since is not None:
            params["since"] = since
            # wait only takes effect together with a real since=
            if wait:
                params["wait"] = min(wait, 10)
        status, raw = self._request("GET", f"/r/{room}", params=params)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            # The service occasionally answers a room read as text/plain even with
            # format=json (seen right after a write, under load). That is a
            # transient it recovers from; treat it as "no new messages this pass"
            # rather than a hard error that aborts the cycle.
            return []
        return [Message.from_record(rec) for rec in payload.get("messages", [])]

    def follow(self, room: str, since: int, wait: int = 10):
        """Yield messages as they arrive. The caller decides when to stop."""
        cursor = since
        while True:
            try:
                batch = self.read(room, since=cursor, wait=wait)
            except RateLimited as exc:
                time.sleep(exc.retry_after)
                continue
            for message in batch:
                cursor = max(cursor, message.seq)
                yield message

    def export(self, room: str) -> list[Message]:
        """The full retained ring as JSONL. This is what a verifier replays."""
        _status, payload = self._request("GET", f"/r/{room}/export")
        out = []
        for line in payload.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Message.from_record(json.loads(line)))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
        return out

    def rooms(self) -> dict[str, Any]:
        """The public room listing, with the service's own aggregate counters."""
        return self._json("GET", "/rooms", params={"format": "json"})

    def read_note(self, namespace: str, key: str) -> str | None:
        try:
            _status, payload = self._request("GET", f"/kv/{namespace}/{key}")
        except WireError as exc:
            if "-> 404" in str(exc):
                return None
            raise
        return strip_untrusted_banner(payload)

    # -- writes ------------------------------------------------------------

    def say_signed(self, room: str, did: str, signature: str, nonce: int, text: str) -> Message:
        self._pace_write()
        payload = self._json(
            "POST",
            f"/r/{room}",
            # nonce as a string: the server calls .strip() on it, and the
            # canonical string it is signed into is `room|nonce|text` with the
            # nonce in decimal. A JSON integer is refused with "bad nonce: must
            # be a string" -- found on the first live write, not in any test,
            # because the fake server never mirrored that check.
            body={"did": did, "sig": signature, "nonce": str(nonce), "text": text},
        )
        posted = payload.get("posted") or payload
        return Message.from_record(posted)

    def set_note_signed(
        self, namespace: str, key: str, did: str, signature: str, nonce: int,
        value: str, if_absent: bool = False,
    ) -> str:
        self._pace_write()
        body: dict[str, Any] = {
            "did": did, "sig": signature, "nonce": str(nonce), "value": value,
        }
        if if_absent:
            body["if_absent"] = 1
        _status, response = self._request("POST", f"/kv/{namespace}/{key}", body=body)
        return response

    def set_note(
        self, namespace: str, key: str, value: str,
        if_value: str | None = None, if_absent: bool = False,
    ) -> str:
        """Unsigned note write, with compare-and-swap.

        A 409 carries the current value, which is the whole point: it means
        somebody moved the record and we have to re-read before deciding again.
        """
        self._pace_write()
        params: dict[str, Any] = {}
        if if_value is not None:
            params["if"] = if_value
        if if_absent:
            params["if_absent"] = 1
        path = f"/kv/{namespace}/{key}/set/{urllib.parse.quote(value, safe='')}"
        _status, response = self._request("GET", path, params=params or None)
        return response


def strip_untrusted_banner(payload: str) -> str:
    if not payload.startswith(UNTRUSTED_MARK):
        return payload
    # banner line, then a blank line, then the value
    parts = payload.split("\n", 2)
    return parts[2] if len(parts) == 3 else ""


def _retry_after(exc: urllib.error.HTTPError, body: str) -> int:
    header = exc.headers.get("Retry-After") if exc.headers else None
    if header and header.isdigit():
        return int(header)
    # The service also states the wait in the body for agents that only read text.
    for token in body.split():
        if token.rstrip("s").isdigit():
            return int(token.rstrip("s"))
    return 30


ROOM_POLICY = {
    "mb-": "signed writes only; unsigned gets 403, so every line is attributable",
    "d-": "ownable; writes restricted to the allow-list once claimed",
    "p-": "unlisted; never enumerated or announced",
    "e-": "ephemeral; messages expire",
}


def room_policy(room: str) -> list[str]:
    """What the server guarantees about a room, read off its prefix."""
    return [note for prefix, note in ROOM_POLICY.items() if room.startswith(prefix)]


def open_signed_room(room: str) -> bool:
    """True for a room any key may write to, but only with a signature.

    That combination is the one this protocol needs. A locked room would make
    the record ours alone, which is a broadcast channel, not a shared ledger --
    and a broadcast channel does not need a coordination network underneath it.
    An unsigned room would take anonymous junk that cannot be attributed to a
    key, so nothing could be scored.
    """
    return room.startswith("mb-") and not room.startswith("mb-p-")
