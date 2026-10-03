"""Small TTL cache used to suppress repeats without unbounded memory use."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DuplicateCacheSnapshot:
    """Portable duplicate-cache state: message IDs with wall-clock deadlines.

    Deadlines are wall-clock (``time.time()``) rather than whatever clock the
    cache was built with, because ``BridgeEngine`` is normally driven by a
    monotonic clock that resets to zero on every process restart. A duration
    like "nine seconds left" survives a restart; a monotonic timestamp like
    "expires at 1532.9" does not.
    """

    entries: tuple[tuple[bytes, float], ...]


class DuplicateCache:
    """Remember recently accepted message IDs for a fixed amount of time."""

    def __init__(self, *, ttl_seconds: float = 300, capacity: int = 4096) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._ttl = ttl_seconds
        self._capacity = capacity
        self._expirations: OrderedDict[bytes, float] = OrderedDict()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    @property
    def capacity(self) -> int:
        return self._capacity

    def seen_or_add(self, message_id: bytes, *, now: float) -> bool:
        """Return true for a live duplicate, otherwise remember the ID."""
        self._expire(now)
        expiration = self._expirations.get(message_id)
        if expiration is not None and expiration > now:
            # Recently seen IDs stay near the end, making capacity eviction an
            # approximate least-recently-used policy.
            self._expirations.move_to_end(message_id)
            return True

        self._expirations[message_id] = now + self._ttl
        self._expirations.move_to_end(message_id)
        while len(self._expirations) > self._capacity:
            self._expirations.popitem(last=False)
        return False

    def _expire(self, now: float) -> None:
        # Expirations are normally ordered, but refreshing a duplicate moves it
        # without extending its TTL. Scan the bounded cache for correctness.
        expired = [key for key, deadline in self._expirations.items() if deadline <= now]
        for key in expired:
            del self._expirations[key]

    def snapshot(self, *, now: float, wall_clock_now: float) -> DuplicateCacheSnapshot:
        """Capture live entries as wall-clock deadlines for later restore."""
        self._expire(now)
        return DuplicateCacheSnapshot(
            entries=tuple(
                (message_id, wall_clock_now + (expiration - now))
                for message_id, expiration in self._expirations.items()
            )
        )

    @classmethod
    def restore(
        cls,
        snapshot: DuplicateCacheSnapshot,
        *,
        now: float,
        wall_clock_now: float,
        ttl_seconds: float = 300,
        capacity: int = 4096,
    ) -> DuplicateCache:
        """Rebuild a cache from a snapshot, dropping anything already expired.

        Entries are re-based onto this process's own ``now`` using how much
        real time was left at snapshot time, so it does not matter whether
        this process's clock resumed at zero or kept running.
        """
        cache = cls(ttl_seconds=ttl_seconds, capacity=capacity)
        live = (
            (message_id, deadline)
            for message_id, deadline in snapshot.entries
            if deadline > wall_clock_now
        )
        # Oldest-expiring first, preserving the cache's own LRU-ish eviction
        # order instead of whatever order the snapshot happened to be in.
        for message_id, deadline in sorted(live, key=lambda item: item[1]):
            cache._expirations[message_id] = now + (deadline - wall_clock_now)
        while len(cache._expirations) > cache._capacity:
            cache._expirations.popitem(last=False)
        return cache
