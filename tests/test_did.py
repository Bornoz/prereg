import re

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from prereg import did as didmod

# The exact shapes technocore.chat enforces, copied from its didkey.py so that a
# change on either side fails here rather than at the first live write.
DID_RE = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}")
SIG_RE = re.compile(r"[A-Za-z0-9_-]{85}[AQgw]")


def make() -> didmod.Identity:
    private = Ed25519PrivateKey.generate()
    return didmod.Identity(private, didmod.did_from_public_key(private.public_key()))


def test_did_has_the_shape_the_server_accepts():
    for _ in range(50):
        assert DID_RE.fullmatch(make().did)


def test_did_round_trips_through_the_public_key():
    identity = make()
    recovered = didmod.public_key_from_did(identity.did)
    assert didmod.did_from_public_key(recovered) == identity.did


def test_signature_is_86_characters_ending_in_the_canonical_set():
    identity = make()
    for i in range(50):
        assert SIG_RE.fullmatch(identity.sign(f"lobby|{i}|hello"))


def test_room_signature_verifies_against_the_canonical_payload():
    identity = make()
    signature = identity.sign_room("d-prereg", 17, "a claim")
    assert didmod.verify(identity.did, signature, "d-prereg|17|a claim")


def test_a_signature_does_not_cover_a_different_nonce():
    identity = make()
    signature = identity.sign_room("d-prereg", 17, "a claim")
    assert not didmod.verify(identity.did, signature, "d-prereg|18|a claim")


def test_note_payload_orders_namespace_key_nonce_value():
    identity = make()
    signature = identity.sign_note("room-owners", "d-prereg", 4, identity.did)
    assert didmod.verify(
        identity.did, signature, f"room-owners|d-prereg|4|{identity.did}"
    )


def test_invisible_characters_are_swept_before_signing():
    # The server rewrites these to spaces before it verifies, so signing the raw
    # text would produce a signature that cannot cover the stored record.
    identity = make()
    raw = "before\nafter​end"
    swept = "before after end"
    assert didmod.sweep(raw) == swept
    assert didmod.verify(identity.did, identity.sign_room("x", 1, raw), f"x|1|{swept}")


def test_another_key_cannot_verify():
    a, b = make(), make()
    assert not didmod.verify(b.did, a.sign_room("x", 1, "hi"), "x|1|hi")


def test_base58_round_trip_keeps_leading_zeros():
    for raw in (b"\x00\x01\x02", b"\x00\x00\xff", b"\xed\x01" + b"\x11" * 32):
        assert didmod.b58decode(didmod.b58encode(raw)) == raw


def test_rejects_a_did_that_is_not_ed25519():
    with pytest.raises(didmod.IdentityError):
        didmod.public_key_from_did("did:key:z6MkTooShort")


def test_identity_file_is_written_unreadable_to_others(tmp_path):
    path = tmp_path / "identity.pem"
    identity = didmod.create(path, "correct horse battery")
    assert path.stat().st_mode & 0o077 == 0
    assert didmod.load(path, "correct horse battery").did == identity.did


def test_refuses_to_overwrite_an_existing_key(tmp_path):
    path = tmp_path / "identity.pem"
    didmod.create(path, "correct horse battery")
    with pytest.raises(didmod.IdentityError):
        didmod.create(path, "correct horse battery")


def test_short_passphrases_are_refused(tmp_path):
    with pytest.raises(didmod.IdentityError):
        didmod.create(tmp_path / "k.pem", "short")
