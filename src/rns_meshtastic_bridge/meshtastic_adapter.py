"""Receive-only boundary for Meshtastic's Reticulum application port.

Meshtastic publishes received packets as dictionaries. Keeping that dynamic
shape at this one boundary prevents the rest of the bridge from depending on
the client library or protobuf-generated classes. This module deliberately has
no serial connection and no call to ``sendData``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from rns_meshtastic_bridge.envelope import BridgeEnvelope
from rns_meshtastic_bridge.errors import BridgeProtocolError
from rns_meshtastic_bridge.fragments import FragmentReassembler, fragment_message


# Meshtastic's official portnums.proto assigns 76 to RETICULUM_TUNNEL_APP.
# Keeping the numeric value here allows unit tests and the pure adapter to run
# without installing the hardware-specific Meshtastic dependency everywhere.
RETICULUM_TUNNEL_PORT = 76
RETICULUM_TUNNEL_NAME = "RETICULUM_TUNNEL_APP"


class PacketDisposition(Enum):
    """Why a received Meshtastic callback was accepted or ignored."""

    IGNORED_PORT = "ignored_port"
    IGNORED_CHANNEL = "ignored_channel"
    MALFORMED = "malformed"
    PARTIAL = "partial"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class ReceiveResult:
    """Structured callback result suitable for metrics and tests."""

    disposition: PacketDisposition
    envelope: BridgeEnvelope | None = None
    error: str | None = None


class TransmissionDisabled(RuntimeError):
    """Raised whenever code tries to transmit through this milestone."""


class MeshtasticReceiveAdapter:
    """Filter and reassemble port-76 packets from one explicit channel."""

    def __init__(
        self,
        *,
        channel_index: int,
        reassembler: FragmentReassembler | None = None,
    ) -> None:
        # Meshtastic devices currently expose eight channel slots numbered 0-7.
        # Requiring the index prevents an accidental fallback to whichever
        # channel happens to be primary on a device.
        if not 0 <= channel_index <= 7:
            raise ValueError("channel_index must be between 0 and 7")
        self._channel_index = channel_index
        self._reassembler = reassembler or FragmentReassembler()

    def receive(self, packet: object, *, now: float) -> ReceiveResult:
        """Validate one client callback and return a side-effect-free result."""
        if not isinstance(packet, Mapping):
            return _malformed("packet must be a mapping")

        decoded = packet.get("decoded")
        if not isinstance(decoded, Mapping):
            return _malformed("packet.decoded must be a mapping")

        port = decoded.get("portnum")
        if port not in (RETICULUM_TUNNEL_PORT, RETICULUM_TUNNEL_NAME):
            return ReceiveResult(PacketDisposition.IGNORED_PORT)

        # Protobuf JSON omits fields holding their default value. Channel zero
        # can therefore be absent and is normalized to zero here.
        channel = packet.get("channel", 0)
        if not isinstance(channel, int):
            return _malformed("packet.channel must be an integer")
        if channel != self._channel_index:
            return ReceiveResult(PacketDisposition.IGNORED_CHANNEL)

        payload = decoded.get("payload")
        if not isinstance(payload, (bytes, bytearray)):
            return _malformed("packet.decoded.payload must contain bytes")

        try:
            assembled = self._reassembler.add(bytes(payload), now=now)
            if assembled is None:
                return ReceiveResult(PacketDisposition.PARTIAL)
            envelope = BridgeEnvelope.decode(assembled)
        except BridgeProtocolError as error:
            # Radio input is untrusted. Report a bad packet without allowing a
            # malformed frame to terminate the long-running callback thread.
            return _malformed(str(error))

        return ReceiveResult(PacketDisposition.COMPLETE, envelope=envelope)

    def frames_for(self, envelope: BridgeEnvelope) -> list[bytes]:
        """Prepare port-76 frames without sending them to any interface."""
        return fragment_message(envelope.message_id, envelope.encode())

    def send(self, envelope: BridgeEnvelope) -> None:
        """Make receive-only operation enforceable rather than conventional."""
        del envelope
        raise TransmissionDisabled(
            "Meshtastic transmission is disabled until the transmit adapter "
            "and on-air integration tests are approved"
        )


def _malformed(message: str) -> ReceiveResult:
    """Construct consistent malformed-packet results."""
    return ReceiveResult(PacketDisposition.MALFORMED, error=message)
