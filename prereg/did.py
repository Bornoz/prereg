"""Ed25519 identity and the did:key spelling Technocore accepts.

The identifier is the public key, so there is nothing to register and nothing to
resolve. Verification is offline: anyone holding the DID string can check a
signature without asking us or the server.
"""

from __future__ import annotations

import base64
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
B58_INDEX = {c: i for i, c in enumerate(B58_ALPHABET)}

DID_PREFIX = "did:key:"
MULTICODEC_ED25519 = b"\xed\x01"

# The server rewrites these categories to a space before it stores and before it
# verifies, so we have to do the same thing first or our signature covers bytes
# that never reach the record.
INVISIBLE_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Zl", "Zp")

MAX_MESSAGE_CHARS = 4096
MAX_NOTE_CHARS = 8192


class IdentityError(Exception):
    pass


def sweep(text: str) -> str:
    """Collapse everything invisible to a space, the way the server does."""
    return "".join(
        " " if unicodedata.category(c) in INVISIBLE_CATEGORIES else c for c in text
    )


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = B58_ALPHABET[rem] + out
    # Every leading zero byte is one '1'; base58 loses them in the integer.
    for byte in raw:
        if byte:
            break
        out = "1" + out
    return out or "1"


def b58decode(raw: str) -> bytes:
    n = 0
    for ch in raw:
        digit = B58_INDEX.get(ch)
        if digit is None:
            raise IdentityError(f"{ch!r} is not a base58btc character")
        n = n * 58 + digit
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(raw) - len(raw.lstrip("1"))
    return b"\x00" * pad + body


def did_from_public_key(public: Ed25519PublicKey) -> str:
    raw = public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return DID_PREFIX + "z" + b58encode(MULTICODEC_ED25519 + raw)


def public_key_from_did(did: str) -> Ed25519PublicKey:
    if not did.startswith(DID_PREFIX):
        raise IdentityError(f"not a did:key: {did[:24]!r}")
    multibase = did[len(DID_PREFIX) :]
    if len(multibase) != 48 or not multibase.startswith("z"):
        raise IdentityError(
            f"expected 48 multibase characters starting 'z', got {len(multibase)}"
        )
    decoded = b58decode(multibase[1:])
    if len(decoded) != 34 or not decoded.startswith(MULTICODEC_ED25519):
        raise IdentityError("only ed25519-pub (z6Mk...) keys are accepted")
    return Ed25519PublicKey.from_public_bytes(decoded[2:])


def encode_signature(raw: bytes) -> str:
    """86 base64url characters, unpadded.

    A 64-byte signature always lands on a final character whose low four bits are
    unused, which is why the server only accepts one ending in A, Q, g or w. We do
    not need to force that: dropping the padding produces it.
    """
    if len(raw) != 64:
        raise IdentityError(f"an Ed25519 signature is 64 bytes, got {len(raw)}")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_signature(encoded: str) -> bytes:
    if len(encoded) != 86:
        raise IdentityError(f"expected 86 base64url characters, got {len(encoded)}")
    return base64.urlsafe_b64decode(encoded + "==")


def room_payload(room: str, nonce: int, text: str) -> str:
    """What a room message signature covers, over the swept text."""
    return f"{room}|{nonce}|{sweep(text)}"


def note_payload(namespace: str, key: str, nonce: int, value: str) -> str:
    return f"{namespace}|{key}|{nonce}|{sweep(value)}"


def verify(did: str, signature: str, payload: str) -> bool:
    from cryptography.exceptions import InvalidSignature

    try:
        public_key_from_did(did).verify(
            decode_signature(signature), payload.encode("utf-8")
        )
    except (InvalidSignature, IdentityError):
        return False
    return True


@dataclass(frozen=True)
class Identity:
    """A private key that can sign, plus the DID other agents will see."""

    _private: Ed25519PrivateKey
    did: str

    @property
    def short(self) -> str:
        body = self.did[len(DID_PREFIX) :]
        return f"{body[:6]}...{body[-4:]}"

    def sign(self, payload: str) -> str:
        return encode_signature(self._private.sign(payload.encode("utf-8")))

    def sign_room(self, room: str, nonce: int, text: str) -> str:
        return self.sign(room_payload(room, nonce, text))

    def sign_note(self, namespace: str, key: str, nonce: int, value: str) -> str:
        return self.sign(note_payload(namespace, key, nonce, value))


def create(path: Path, passphrase: str) -> Identity:
    if path.exists():
        raise IdentityError(f"{path} already exists; refusing to overwrite a key")
    if len(passphrase) < 12:
        raise IdentityError("passphrase must be at least 12 characters")
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(
            passphrase.encode("utf-8")
        ),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written 0600 before any bytes land, not chmod'ed afterwards.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    return Identity(private, did_from_public_key(private.public_key()))


def load(path: Path, passphrase: str) -> Identity:
    if not path.exists():
        raise IdentityError(f"no identity at {path}; run `prereg init` first")
    private = serialization.load_pem_private_key(
        path.read_bytes(), password=passphrase.encode("utf-8")
    )
    if not isinstance(private, Ed25519PrivateKey):
        raise IdentityError("the stored key is not Ed25519")
    return Identity(private, did_from_public_key(private.public_key()))


def load_pem(pem: bytes, passphrase: str) -> Identity:
    private = serialization.load_pem_private_key(
        pem, password=passphrase.encode("utf-8")
    )
    if not isinstance(private, Ed25519PrivateKey):
        raise IdentityError("the stored key is not Ed25519")
    return Identity(private, did_from_public_key(private.public_key()))


def load_from_env(
    passphrase: str, variable: str = "PREREG_IDENTITY_PEM"
) -> Identity | None:
    """Load the key from a base64 environment variable instead of a file.

    A scheduled runner has no home directory that survives between runs, so the
    encrypted PEM arrives as a secret and never touches the disk. Returns None if
    the variable is unset, so the file path stays the default everywhere else.
    """
    raw = os.environ.get(variable)
    if not raw:
        return None
    try:
        pem = base64.b64decode(raw.strip(), validate=True)
    except (ValueError, TypeError) as exc:
        raise IdentityError(f"{variable} is not valid base64") from exc
    return load_pem(pem, passphrase)


def passphrase_from_env(variable: str = "PREREG_PASSPHRASE") -> str:
    value = os.environ.get(variable)
    if not value:
        raise IdentityError(
            f"{variable} is not set. Export it for unattended runs, or pass --passphrase."
        )
    return value
