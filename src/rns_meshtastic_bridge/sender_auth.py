"""Reticulum-side sender authorization, mirroring Meshtastic's ``admin_key``.

Meshtastic restricts privileged operations to senders who cryptographically
prove possession of a private key matching one of a short, operator-configured
``admin_key`` allowlist (see ``meshtastic/firmware``'s ``AdminKeys.cpp`` and
``AdminModule.cpp``): a PKI-encrypted admin packet only decrypts successfully
if the sender held the matching private key, so successful decryption *is*
the proof of identity; firmware then does one more check — is that now-proven
public key actually on the configured allowlist.

Reticulum has no equivalent "prove it by successfully decrypting" step for a
single, connectionless destination the way Meshtastic's PKI admin packets do
(that would require an ``RNS.Link`` session instead of a one-shot packet), so
this module asks senders to prove possession explicitly instead: sign the
inner bridge envelope with ``RNS.Identity.sign()`` and send the sender's
public key, the signature, and the envelope together.
``RNS.Identity.validate()`` is the proof-of-possession step;  checking the
signer's resulting identity hash against an allowlist is the authorization
step — the same two steps Meshtastic itself performs, implemented with
Reticulum's own primitives instead of Meshtastic's.

This is deliberately a small, flat, operator-edited allowlist (a handful of
trusted identity hashes), the same shape ``admin_key`` has (up to three
configured keys) rather than a database or a running auth service. A future
web-based signup/auth system can replace how the allowlist is populated
without changing how a packet is verified.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import struct

import RNS

from rns_meshtastic_bridge.envelope import ID_LENGTH


PUBLIC_KEY_LENGTH = RNS.Identity.KEYSIZE // 8
SIGNATURE_LENGTH = RNS.Identity.SIGLENGTH // 8
_HEADER = struct.Struct(f">{PUBLIC_KEY_LENGTH}s{SIGNATURE_LENGTH}s")


class SignatureInvalid(RuntimeError):
    """A signed envelope is malformed, or its signature does not verify."""


class SenderNotAuthorized(RuntimeError):
    """A validly-signed envelope's signer is not on the configured allowlist."""


def sign_envelope(identity: RNS.Identity, envelope_bytes: bytes) -> bytes:
    """Wrap ``envelope_bytes`` with the sender's public key and a signature.

    This is the sender-side half of the protocol; a future sender-facing
    tool (CLI, or eventually the planned web app's backend) calls this
    before handing the result to ``RNS.Packet(destination, wire).send()``.
    """
    public_key = identity.get_public_key()
    if public_key is None or len(public_key) != PUBLIC_KEY_LENGTH:
        raise ValueError("identity has no usable public key")
    signature = identity.sign(envelope_bytes)
    return _HEADER.pack(public_key, signature) + envelope_bytes


class SenderAllowlist:
    """A short list of identity hashes authorized to have traffic forwarded.

    Mirrors ``meshtastic/firmware``'s ``AdminKeys``: a flat, small,
    operator-edited list, with membership checked only after a signature has
    already proven the claimed identity is genuine.
    """

    def __init__(self, authorized_hashes: Iterable[bytes]) -> None:
        normalized: set[bytes] = set()
        for identity_hash in authorized_hashes:
            if len(identity_hash) != ID_LENGTH:
                raise ValueError(f"authorized hash must be exactly {ID_LENGTH} bytes")
            normalized.add(identity_hash)
        self._authorized = frozenset(normalized)

    def __len__(self) -> int:
        return len(self._authorized)

    def is_authorized(self, identity_hash: bytes) -> bool:
        return identity_hash in self._authorized

    @classmethod
    def from_hex_lines(cls, text: str) -> SenderAllowlist:
        """Parse one hex-encoded identity hash per line.

        Blank lines and ``#``-prefixed comments are ignored, so the file an
        operator edits can be self-documenting.
        """
        hashes = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            hashes.append(bytes.fromhex(stripped))
        return cls(hashes)


@dataclass(frozen=True, slots=True)
class VerifiedEnvelope:
    """A signed envelope that passed both verification steps."""

    identity_hash: bytes
    envelope_bytes: bytes


def unwrap_and_verify(wire: bytes, allowlist: SenderAllowlist) -> VerifiedEnvelope:
    """Verify a signed envelope's signature, then its signer's authorization.

    Raises ``SignatureInvalid`` for a structurally wrong or forged wrapper,
    and ``SenderNotAuthorized`` for a validly-signed sender who is simply not
    on the allowlist. Callers should treat both as "drop and log the
    structural fact, never the payload," the same posture
    ``rns_meshtastic_bridge.meshtastic_adapter`` and ``rns_meshtastic_bridge.reticulum_adapter`` already
    take toward untrusted input.
    """
    if len(wire) < _HEADER.size:
        raise SignatureInvalid("signed envelope is shorter than its header")
    public_key, signature = _HEADER.unpack_from(wire)
    envelope_bytes = wire[_HEADER.size :]

    verifier = RNS.Identity(create_keys=False)
    if verifier.load_public_key(public_key) is False:
        raise SignatureInvalid("malformed sender public key")

    if not verifier.validate(signature, envelope_bytes):
        raise SignatureInvalid("signature does not match envelope bytes")

    identity_hash = verifier.hash
    if identity_hash is None:
        # Unreachable once load_public_key succeeds, but keeps this function
        # honest about the Optional type RNS declares for Identity.hash.
        raise SignatureInvalid("verified identity has no hash")

    if not allowlist.is_authorized(identity_hash):
        raise SenderNotAuthorized(
            f"sender {identity_hash.hex()} is not on the allowlist"
        )

    return VerifiedEnvelope(identity_hash=identity_hash, envelope_bytes=envelope_bytes)
