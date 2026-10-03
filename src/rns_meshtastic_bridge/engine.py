"""Pure routing decisions for a bidirectional two-network bridge."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import secrets
from typing import Callable

from rns_meshtastic_bridge.deduplication import DuplicateCache, DuplicateCacheSnapshot
from rns_meshtastic_bridge.envelope import BridgeEnvelope, ID_LENGTH


class Ingress(Enum):
    """The network from which an encoded bridge envelope arrived."""

    RETICULUM = "reticulum"
    MESHTASTIC = "meshtastic"

    @property
    def opposite(self) -> Ingress:
        return (
            Ingress.MESHTASTIC
            if self is Ingress.RETICULUM
            else Ingress.RETICULUM
        )


class BridgeAction(Enum):
    """Auditable outcome of inspecting an incoming envelope."""

    FORWARD = "forward"
    DROP_DUPLICATE = "drop_duplicate"
    DROP_REFLECTION = "drop_reflection"
    DROP_HOP_LIMIT = "drop_hop_limit"


@dataclass(frozen=True, slots=True)
class BridgeDecision:
    """Decision returned to an adapter; no I/O happens inside the engine."""

    action: BridgeAction
    egress: Ingress | None = None
    wire: bytes | None = None


class BridgeEngine:
    """Create and relay envelopes with loop and duplicate protection."""

    def __init__(
        self,
        bridge_id: bytes,
        *,
        max_hops: int = 4,
        duplicate_ttl_seconds: float = 300,
        duplicate_capacity: int = 4096,
        id_factory: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        if len(bridge_id) != ID_LENGTH:
            raise ValueError("bridge_id must be exactly 16 bytes")
        if not 1 <= max_hops <= 255:
            raise ValueError("max_hops must be between 1 and 255")
        self._bridge_id = bridge_id
        self._max_hops = max_hops
        self._id_factory = id_factory
        self._duplicates = DuplicateCache(
            ttl_seconds=duplicate_ttl_seconds,
            capacity=duplicate_capacity,
        )

    def originate(self, payload: bytes, *, now: float) -> BridgeEnvelope:
        """Wrap a new local payload and reserve its ID against reflections."""
        message_id = self._id_factory(ID_LENGTH)
        if len(message_id) != ID_LENGTH:
            raise ValueError("id_factory must return exactly 16 bytes")
        envelope = BridgeEnvelope(
            message_id=message_id,
            origin_id=self._bridge_id,
            payload=payload,
            max_hops=self._max_hops,
        )
        self._duplicates.seen_or_add(message_id, now=now)
        return envelope

    def inspect(
        self,
        wire: bytes,
        *,
        ingress: Ingress,
        now: float,
    ) -> BridgeDecision:
        """Validate and decide whether an envelope may cross the bridge."""
        envelope = BridgeEnvelope.decode(wire)

        # An envelope originating here has returned through the opposite
        # network. Drop it even if its duplicate-cache entry has expired.
        if envelope.origin_id == self._bridge_id:
            return BridgeDecision(BridgeAction.DROP_REFLECTION)
        if self._duplicates.seen_or_add(envelope.message_id, now=now):
            return BridgeDecision(BridgeAction.DROP_DUPLICATE)
        if envelope.hops >= envelope.max_hops:
            return BridgeDecision(BridgeAction.DROP_HOP_LIMIT)

        return BridgeDecision(
            action=BridgeAction.FORWARD,
            egress=ingress.opposite,
            wire=envelope.advanced().encode(),
        )

    def snapshot_duplicates(
        self, *, now: float, wall_clock_now: float
    ) -> DuplicateCacheSnapshot:
        """Capture duplicate-suppression state so a restart can restore it.

        This only captures the duplicate cache. ``bridge_id`` is operator
        configuration, not runtime state, so it is supplied again when the
        engine restarts rather than persisted here.
        """
        return self._duplicates.snapshot(now=now, wall_clock_now=wall_clock_now)

    def restore_duplicates(
        self,
        snapshot: DuplicateCacheSnapshot,
        *,
        now: float,
        wall_clock_now: float,
    ) -> None:
        """Replace this engine's duplicate cache with a restored snapshot.

        Call this immediately after construction and before the first
        ``inspect`` or ``originate``, while the live cache is still empty.
        """
        self._duplicates = DuplicateCache.restore(
            snapshot,
            now=now,
            wall_clock_now=wall_clock_now,
            ttl_seconds=self._duplicates.ttl_seconds,
            capacity=self._duplicates.capacity,
        )
