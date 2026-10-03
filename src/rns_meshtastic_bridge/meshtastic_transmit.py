"""Default-off Meshtastic transmit path.

Constructing ``MeshtasticTransmitAdapter`` does nothing transmit-capable by
itself: it refuses every operation unless a caller passes ``enabled=True``
to its constructor, and even then it never touches a serial device, pubsub,
or the Meshtastic client library itself. It only ever hands a frame to an
injected ``FrameTransmitter`` the caller supplies and owns.

Enabling it is a deliberate, separate decision from merely deploying this
code. Within this package, only an operator passing ``--live``
(``transmit_check.py``) or both ``--enable-transmit`` and ``--rate``
(``bridge_service.py``) ever constructs it with ``enabled=True`` — and both
of those also verify the live device's region/preset first. No default code
path turns transmission on.
"""

from __future__ import annotations

from typing import Protocol

from rns_meshtastic_bridge.airtime import BoundedFrameQueue, TokenBucketLimiter, next_ready_frame
from rns_meshtastic_bridge.envelope import BridgeEnvelope
from rns_meshtastic_bridge.fragments import fragment_message
from rns_meshtastic_bridge.meshtastic_adapter import RETICULUM_TUNNEL_PORT, TransmissionDisabled


_DISABLED_MESSAGE = (
    "Meshtastic transmission is disabled until explicitly enabled and "
    "approved for a controlled on-air test"
)

_DEFAULT_MAX_FRAMES = 256
_DEFAULT_MAX_BYTES = 64 * 1024


class FrameTransmitter(Protocol):
    """The one operation this adapter is allowed to call to transmit a frame.

    A real implementation would wrap a Meshtastic client's ``sendData``; this
    adapter never constructs or imports that client itself, so tests supply a
    plain mock here instead.
    """

    def send_frame(self, frame: bytes, *, channel_index: int, port: int) -> None: ...


class MeshtasticTransmitAdapter:
    """Fragment, rate-limit, and queue envelopes for Meshtastic transmission.

    Every public method raises ``TransmissionDisabled`` unless this adapter
    was constructed with ``enabled=True``. There is no way to flip it on
    after construction, so enabling transmission is always a conscious choice
    made at the call site that builds the adapter, not a runtime toggle a bug
    elsewhere could trip.
    """

    def __init__(
        self,
        *,
        channel_index: int,
        limiter: TokenBucketLimiter,
        queue: BoundedFrameQueue | None = None,
        enabled: bool = False,
    ) -> None:
        if not 0 <= channel_index <= 7:
            raise ValueError("channel_index must be between 0 and 7")
        self._channel_index = channel_index
        self._limiter = limiter
        self._queue = queue or BoundedFrameQueue(
            max_frames=_DEFAULT_MAX_FRAMES, max_bytes=_DEFAULT_MAX_BYTES
        )
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def queued_frames(self) -> int:
        return self._queue.frame_count

    def enqueue(self, envelope: BridgeEnvelope) -> bool:
        """Fragment one envelope and admit its frames to the outbound queue.

        Returns False if the bounded queue has no room; no partial fragment
        set is ever admitted (see ``BoundedFrameQueue.offer_envelope``).
        """
        self._require_enabled()
        frames = fragment_message(envelope.message_id, envelope.encode())
        return self._queue.offer_envelope(envelope.message_id, frames)

    def send_ready(self, *, now: float, transmitter: FrameTransmitter) -> int:
        """Send every currently rate-permitted queued frame.

        Returns how many frames were sent. A frame the rate limiter denies
        stays queued for a later call with a greater ``now``.
        """
        self._require_enabled()
        sent = 0
        while True:
            item = next_ready_frame(self._queue, self._limiter, now=now)
            if item is None:
                break
            transmitter.send_frame(
                item.frame,
                channel_index=self._channel_index,
                port=RETICULUM_TUNNEL_PORT,
            )
            sent += 1
        return sent

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise TransmissionDisabled(_DISABLED_MESSAGE)
