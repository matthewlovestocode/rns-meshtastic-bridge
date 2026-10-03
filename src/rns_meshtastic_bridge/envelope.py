"""Versioned binary envelope shared by both sides of the bridge.

The envelope carries an opaque application payload. Reticulum identities and
Meshtastic channel encryption remain the responsibility of their respective
networks; this header exists only to make relay behavior deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import struct

from rns_meshtastic_bridge.errors import MalformedEnvelope


MAGIC = b"RB"
VERSION = 1
ID_LENGTH = 16
MAX_PAYLOAD_BYTES = 1024 * 1024

# magic, version, flags, message ID, origin bridge ID, hops, max hops, length
_HEADER = struct.Struct(">2sBB16s16sBBI")


@dataclass(frozen=True, slots=True)
class BridgeEnvelope:
    """One logical message plus the metadata needed for safe relaying."""

    message_id: bytes
    origin_id: bytes
    payload: bytes
    hops: int = 0
    max_hops: int = 4

    def __post_init__(self) -> None:
        if len(self.message_id) != ID_LENGTH:
            raise MalformedEnvelope("message_id must be exactly 16 bytes")
        if len(self.origin_id) != ID_LENGTH:
            raise MalformedEnvelope("origin_id must be exactly 16 bytes")
        if not 1 <= self.max_hops <= 255:
            raise MalformedEnvelope("max_hops must be between 1 and 255")
        if not 0 <= self.hops <= self.max_hops:
            raise MalformedEnvelope("hops must be between zero and max_hops")
        if len(self.payload) > MAX_PAYLOAD_BYTES:
            raise MalformedEnvelope("payload exceeds the one MiB safety limit")

    def encode(self) -> bytes:
        """Return the stable wire representation of this envelope."""
        header = _HEADER.pack(
            MAGIC,
            VERSION,
            0,  # Flags are reserved and must stay zero in protocol version 1.
            self.message_id,
            self.origin_id,
            self.hops,
            self.max_hops,
            len(self.payload),
        )
        return header + self.payload

    def advanced(self) -> BridgeEnvelope:
        """Return the copy a bridge sends after consuming one relay hop."""
        if self.hops >= self.max_hops:
            raise MalformedEnvelope("cannot advance an exhausted hop limit")
        return replace(self, hops=self.hops + 1)

    @classmethod
    def decode(cls, wire: bytes) -> BridgeEnvelope:
        """Validate untrusted bytes before constructing an envelope."""
        if len(wire) < _HEADER.size:
            raise MalformedEnvelope("envelope is shorter than its header")

        magic, version, flags, message_id, origin_id, hops, max_hops, length = (
            _HEADER.unpack_from(wire)
        )
        if magic != MAGIC:
            raise MalformedEnvelope("unknown envelope magic")
        if version != VERSION:
            raise MalformedEnvelope(f"unsupported envelope version {version}")
        if flags != 0:
            raise MalformedEnvelope("version 1 reserved flags must be zero")
        if length > MAX_PAYLOAD_BYTES:
            raise MalformedEnvelope("declared payload exceeds the safety limit")
        if len(wire) != _HEADER.size + length:
            raise MalformedEnvelope("declared payload length does not match bytes")

        return cls(
            message_id=message_id,
            origin_id=origin_id,
            payload=wire[_HEADER.size :],
            hops=hops,
            max_hops=max_hops,
        )
