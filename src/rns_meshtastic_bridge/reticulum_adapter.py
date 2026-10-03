"""Mocked Reticulum ingress/egress boundary for the bridge.

This module owns every place the bridge touches ``RNS.Packet`` so no other
module needs to parse a raw Reticulum payload or build an outbound packet.
Nothing here opens a live Reticulum instance: egress sends go through an
injected packet factory, and ingress only decodes bytes a caller already
received from a destination's packet callback. That keeps this adapter
testable with plain mocks, matching ``rns_meshtastic_bridge.meshtastic_adapter``.

``RNS.Packet.send()`` returns one of three things:
a ``PacketReceipt`` when Reticulum will track delivery, ``False`` when no
interface accepted the packet, and ``None`` when the packet was built without
receipt tracking. ``ReticulumEgressAdapter`` classifies all three outcomes and
never lets an unbounded number of outbound envelopes accumulate in memory.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from rns_meshtastic_bridge.envelope import BridgeEnvelope
from rns_meshtastic_bridge.errors import BridgeProtocolError


class PacketReceiptLike(Protocol):
    """The two receipt operations this adapter relies on."""

    def set_delivery_callback(self, callback: Callable[[Any], Any]) -> Any: ...

    def set_timeout_callback(self, callback: Callable[[Any], Any]) -> Any: ...


PacketFactory = Callable[[object, bytes], Any]


def _default_packet_factory(destination: object, data: bytes) -> Any:
    import RNS  # Imported lazily so unit tests never need a live instance.

    return RNS.Packet(destination, data)


class SendDisposition(Enum):
    """Immediate, synchronous outcome of handing a packet to Reticulum."""

    IN_FLIGHT = "in_flight"
    REJECTED = "rejected"
    UNTRACKED = "untracked"


class DeliveryDisposition(Enum):
    """Later, asynchronous outcome reported through a receipt callback."""

    DELIVERED = "delivered"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SendResult:
    """Structured result for one dequeued send attempt."""

    disposition: SendDisposition
    envelope: BridgeEnvelope


class ReticulumEgressAdapter:
    """Own outbound packet construction with a bounded pending queue.

    The queue bounds memory when Reticulum cannot keep up with the rate at
    which envelopes are offered; ``offer`` rejects new work instead of
    growing without limit. Delivery and timeout callbacks are tracked by
    receipt identity so a late Reticulum callback cannot be attributed to the
    wrong envelope.
    """

    def __init__(
        self,
        *,
        capacity: int = 64,
        packet_factory: PacketFactory | None = None,
        on_delivered: Callable[[BridgeEnvelope], None] | None = None,
        on_failed: Callable[[BridgeEnvelope], None] | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._capacity = capacity
        self._packet_factory = packet_factory or _default_packet_factory
        self._on_delivered = on_delivered
        self._on_failed = on_failed
        self._queue: deque[tuple[object, BridgeEnvelope]] = deque()
        self._in_flight: dict[int, BridgeEnvelope] = {}

    @property
    def pending(self) -> int:
        """Envelopes queued but not yet handed to Reticulum."""
        return len(self._queue)

    @property
    def in_flight(self) -> int:
        """Envelopes handed to Reticulum with a receipt still outstanding."""
        return len(self._in_flight)

    def offer(self, destination: object, envelope: BridgeEnvelope) -> bool:
        """Queue one envelope for sending; return False under backpressure."""
        if len(self._queue) >= self._capacity:
            return False
        self._queue.append((destination, envelope))
        return True

    def send_next(self) -> SendResult | None:
        """Hand the oldest queued envelope to Reticulum, or return None."""
        if not self._queue:
            return None
        destination, envelope = self._queue.popleft()
        packet = self._packet_factory(destination, envelope.encode())
        result = packet.send()

        if result is False:
            return SendResult(SendDisposition.REJECTED, envelope)
        if result is None:
            return SendResult(SendDisposition.UNTRACKED, envelope)

        receipt = result
        key = id(receipt)
        self._in_flight[key] = envelope
        receipt.set_delivery_callback(lambda _receipt, _key=key: self._resolve(
            _key, DeliveryDisposition.DELIVERED
        ))
        receipt.set_timeout_callback(lambda _receipt, _key=key: self._resolve(
            _key, DeliveryDisposition.FAILED
        ))
        return SendResult(SendDisposition.IN_FLIGHT, envelope)

    def _resolve(self, key: int, disposition: DeliveryDisposition) -> None:
        envelope = self._in_flight.pop(key, None)
        if envelope is None:
            # Already resolved, or the receipt belongs to a prior adapter
            # instance; ignore rather than report a stale or duplicate event.
            return
        callback = (
            self._on_delivered
            if disposition is DeliveryDisposition.DELIVERED
            else self._on_failed
        )
        if callback is not None:
            callback(envelope)


class IngressDisposition(Enum):
    """Why a decoded Reticulum payload was accepted or rejected."""

    MALFORMED = "malformed"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class IngressResult:
    """Structured result mirroring ``rns_meshtastic_bridge.meshtastic_adapter.ReceiveResult``."""

    disposition: IngressDisposition
    envelope: BridgeEnvelope | None = None
    error: str | None = None


class ReticulumIngressAdapter:
    """Decode a Reticulum destination's packet payload into a bridge envelope.

    This is the sole place outside ``rns_meshtastic_bridge.envelope`` that turns bytes
    received from a Reticulum packet callback into a ``BridgeEnvelope``.
    Reticulum delivers whole packets, so there is no fragmentation boundary
    here the way there is on the Meshtastic side.
    """

    def receive(self, message: bytes) -> IngressResult:
        try:
            envelope = BridgeEnvelope.decode(message)
        except BridgeProtocolError as error:
            return IngressResult(IngressDisposition.MALFORMED, error=str(error))
        return IngressResult(IngressDisposition.COMPLETE, envelope=envelope)
