"""Rate limiting and a bounded outbound frame queue for Meshtastic egress.

This is deliberately just the timing and admission policy, with no transmit
path behind it: nothing in this module imports Meshtastic, opens a serial
device, or calls ``sendData``. A separate, default-off transmit adapter
(``meshtastic_transmit.py``) calls ``next_ready_frame`` below and actually
writes a frame to the radio.

This module cannot know the true on-air time of a given LoRa frame — that
depends on spreading factor, bandwidth, and coding rate, which are radio
configuration this software stack does not see. ``TokenBucketLimiter`` only
enforces whatever ``rate_per_second`` an operator configures; it does not
supply a default, because a safe default would have to assume a specific
regulatory region and radio configuration this project cannot verify.
Configure it conservatively for the deployed hardware and local regulations
before it is ever connected to a real transmit path.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


class TokenBucketLimiter:
    """A classic token bucket: allows a configured burst, then a steady rate.

    Every method takes an explicit ``now`` instead of reading a clock, so
    tests (and, later, a real caller) control time exactly the way the rest
    of this package's ``now``-taking methods do.
    """

    def __init__(self, *, rate_per_second: float, burst: int, now: float) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        if burst < 1:
            raise ValueError("burst must be at least 1")
        self._rate = rate_per_second
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._last_update = now

    def try_acquire(self, *, now: float) -> bool:
        """Consume one token if available; never blocks or sleeps."""
        if now < self._last_update:
            raise ValueError("now must not move backwards")
        elapsed = now - self._last_update
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_update = now
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True

    @property
    def available_tokens(self) -> float:
        """Tokens available as of the last ``try_acquire`` call."""
        return self._tokens


@dataclass(frozen=True, slots=True)
class QueuedFrame:
    """One Meshtastic-sized frame still waiting to be sent, with its origin."""

    envelope_id: bytes
    frame: bytes


class BoundedFrameQueue:
    """Bound outbound frames by both count and total bytes.

    Capacity is expressed in frames and bytes rather than envelopes, because
    ``fragment_message`` can turn one large envelope into many frames;
    bounding only envelope count would not bound the actual airtime-relevant
    backlog. Admission is all-or-nothing per envelope: either every frame for
    an envelope fits, or none are enqueued, so a receiver can never observe a
    fragment set that the queue started and then had to truncate.
    """

    def __init__(self, *, max_frames: int, max_bytes: int) -> None:
        if max_frames < 1:
            raise ValueError("max_frames must be at least 1")
        if max_bytes < 1:
            raise ValueError("max_bytes must be at least 1")
        self._max_frames = max_frames
        self._max_bytes = max_bytes
        self._queue: deque[QueuedFrame] = deque()
        self._byte_count = 0

    @property
    def frame_count(self) -> int:
        return len(self._queue)

    @property
    def byte_count(self) -> int:
        return self._byte_count

    def offer_envelope(self, envelope_id: bytes, frames: list[bytes]) -> bool:
        """Enqueue every frame for one envelope, or none of them."""
        if not frames:
            raise ValueError("frames must not be empty")
        added_bytes = sum(len(frame) for frame in frames)
        if (
            len(self._queue) + len(frames) > self._max_frames
            or self._byte_count + added_bytes > self._max_bytes
        ):
            return False
        for frame in frames:
            self._queue.append(QueuedFrame(envelope_id=envelope_id, frame=frame))
        self._byte_count += added_bytes
        return True

    def peek(self) -> QueuedFrame | None:
        """Return the oldest queued frame without removing it."""
        return self._queue[0] if self._queue else None

    def pop(self) -> QueuedFrame | None:
        """Remove and return the oldest queued frame, if any."""
        if not self._queue:
            return None
        item = self._queue.popleft()
        self._byte_count -= len(item.frame)
        return item


def next_ready_frame(
    queue: BoundedFrameQueue, limiter: TokenBucketLimiter, *, now: float
) -> QueuedFrame | None:
    """Pop the next frame only if the rate limiter currently permits it.

    The frame is left in the queue when the limiter denies it, so a later
    call with a later ``now`` can still send it once enough tokens have
    accumulated. This function does not send anything; a future transmit
    adapter is expected to call it and then hand the returned frame to the
    radio.
    """
    if queue.peek() is None:
        return None
    if not limiter.try_acquire(now=now):
        return None
    return queue.pop()
