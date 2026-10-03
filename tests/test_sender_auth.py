"""Sender authorization is tested with real RNS.Identity keypairs.

No mocking of the cryptography here: the whole point of this module is that
a forged or unauthorized sender's packet is rejected by real signature
verification, not by a check that could be bypassed if the crypto were
stubbed out.
"""

import pytest
import RNS

from rns_meshtastic_bridge.sender_auth import (
    SenderAllowlist,
    SenderNotAuthorized,
    SignatureInvalid,
    sign_envelope,
    unwrap_and_verify,
)


ENVELOPE_BYTES = b"an encoded BridgeEnvelope would go here"


@pytest.fixture(scope="module")
def authorized_identity() -> RNS.Identity:
    return RNS.Identity()


@pytest.fixture(scope="module")
def stranger_identity() -> RNS.Identity:
    return RNS.Identity()


@pytest.fixture
def allowlist(authorized_identity: RNS.Identity) -> SenderAllowlist:
    assert authorized_identity.hash is not None
    return SenderAllowlist([authorized_identity.hash])


class TestRoundTrip:
    def test_an_authorized_signer_is_verified(
        self, authorized_identity: RNS.Identity, allowlist: SenderAllowlist
    ) -> None:
        wire = sign_envelope(authorized_identity, ENVELOPE_BYTES)

        result = unwrap_and_verify(wire, allowlist)

        assert result.envelope_bytes == ENVELOPE_BYTES
        assert result.identity_hash == authorized_identity.hash

    def test_a_validly_signed_stranger_is_rejected(
        self, stranger_identity: RNS.Identity, allowlist: SenderAllowlist
    ) -> None:
        wire = sign_envelope(stranger_identity, ENVELOPE_BYTES)

        with pytest.raises(SenderNotAuthorized, match="not on the allowlist"):
            unwrap_and_verify(wire, allowlist)


class TestTampering:
    def test_tampered_envelope_bytes_fail_signature_verification(
        self, authorized_identity: RNS.Identity, allowlist: SenderAllowlist
    ) -> None:
        wire = sign_envelope(authorized_identity, ENVELOPE_BYTES)
        tampered = wire[:-1] + bytes([wire[-1] ^ 1])

        with pytest.raises(SignatureInvalid, match="signature does not match"):
            unwrap_and_verify(tampered, allowlist)

    def test_substituting_a_different_valid_public_key_fails_verification(
        self,
        authorized_identity: RNS.Identity,
        stranger_identity: RNS.Identity,
        allowlist: SenderAllowlist,
    ) -> None:
        wire = sign_envelope(authorized_identity, ENVELOPE_BYTES)
        stranger_key = stranger_identity.get_public_key()
        assert stranger_key is not None
        forged = stranger_key + wire[len(stranger_key) :]

        with pytest.raises(SignatureInvalid, match="signature does not match"):
            unwrap_and_verify(forged, allowlist)

    def test_garbage_key_bytes_fail_signature_verification_without_raising_unexpectedly(
        self, allowlist: SenderAllowlist
    ) -> None:
        # The underlying crypto library accepts any correctly-sized byte
        # string as X25519/Ed25519 key material (point validation is not
        # performed), so a fixed-length, correctly-framed garbage key cannot
        # make load_public_key() itself fail here; it is still rejected, via
        # the signature-mismatch path, and never raises an unhandled error.
        garbage = b"\x00" * 64 + b"\x00" * 64 + ENVELOPE_BYTES

        with pytest.raises(SignatureInvalid, match="signature does not match"):
            unwrap_and_verify(garbage, allowlist)

    def test_truncated_wire_is_reported(self, allowlist: SenderAllowlist) -> None:
        with pytest.raises(SignatureInvalid, match="shorter than its header"):
            unwrap_and_verify(b"too short", allowlist)


class TestSenderAllowlist:
    def test_rejects_a_wrong_length_hash(self) -> None:
        with pytest.raises(ValueError, match="exactly 16 bytes"):
            SenderAllowlist([b"short"])

    def test_len_reflects_unique_entries(self, authorized_identity: RNS.Identity) -> None:
        assert authorized_identity.hash is not None
        allowlist = SenderAllowlist([authorized_identity.hash, authorized_identity.hash])
        assert len(allowlist) == 1

    def test_from_hex_lines_ignores_blanks_and_comments(self) -> None:
        text = """
        # trusted bridge operators
        6d6d6d6d6d6d6d6d6d6d6d6d6d6d6d6d

        # a second one
        726f6f6f6f6f6f6f6f6f6f6f6f6f6f6f
        """
        allowlist = SenderAllowlist.from_hex_lines(text)

        assert len(allowlist) == 2
        assert allowlist.is_authorized(bytes.fromhex("6d6d6d6d6d6d6d6d6d6d6d6d6d6d6d6d"))
        assert not allowlist.is_authorized(b"x" * 16)


def test_sign_envelope_rejects_an_identity_without_a_public_key() -> None:
    bare = RNS.Identity(create_keys=False)
    with pytest.raises(ValueError, match="no usable public key"):
        sign_envelope(bare, ENVELOPE_BYTES)
