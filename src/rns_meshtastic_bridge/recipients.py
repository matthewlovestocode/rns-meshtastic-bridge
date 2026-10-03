"""Fan-out Reticulum recipients for Meshtastic-originated bridge traffic.

Meshtastic broadcasts on a channel — no addressee needed, which is exactly
how ``MeshtasticTransmitAdapter`` already works. Reticulum has no broadcast
primitive: every ``RNS.Packet`` needs a specific destination. So a message
received from Meshtastic and approved by ``BridgeEngine`` for the Reticulum
side has to be fanned out as one packet per authorized recipient instead.

``RecipientList`` mirrors ``rns_meshtastic_bridge.sender_auth.SenderAllowlist``'s shape (a
flat, hex-per-line, operator-edited list) but the other direction: these are
Reticulum destinations a message gets forwarded *to*, not senders being
authorized to send one. This is deliberately a small, local, operator-edited
list for now — the same placeholder status ``SenderAllowlist`` has — a
future web-based signup/auth system can change how recipients get added
without the resolution or fan-out logic below changing at all.

``RecipientResolver`` turns each destination hash into a usable
``RNS.Destination`` by recalling its identity (requesting a path first if
Reticulum doesn't already have one). That can block for seconds, so it must
never run inside a receive callback — call ``refresh()`` periodically from a
service's own loop instead, and read ``destinations()`` from the hot fan-out
path.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
import logging
import time

import RNS

from rns_meshtastic_bridge.engine import BridgeAction, BridgeEngine, Ingress
from rns_meshtastic_bridge.envelope import BridgeEnvelope
from rns_meshtastic_bridge.reticulum_adapter import ReticulumEgressAdapter
from rns_meshtastic_bridge.protocol import APP_NAME, BRIDGE_DESTINATION_ASPECT


class RecipientList:
    """A flat list of Reticulum destination hashes authorized to receive
    Meshtastic-originated bridge traffic.
    """

    def __init__(self, destination_hashes: Iterable[bytes]) -> None:
        expected_length = RNS.Reticulum.TRUNCATED_HASHLENGTH // 8
        normalized: set[bytes] = set()
        for destination_hash in destination_hashes:
            if len(destination_hash) != expected_length:
                raise ValueError(
                    f"destination hash must be exactly {expected_length} bytes"
                )
            normalized.add(destination_hash)
        self._hashes = frozenset(normalized)

    def __len__(self) -> int:
        return len(self._hashes)

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._hashes)

    @classmethod
    def from_hex_lines(cls, text: str) -> RecipientList:
        """Parse one hex-encoded destination hash per line.

        Blank lines and ``#``-prefixed comments are ignored, matching
        ``SenderAllowlist.from_hex_lines``'s format exactly.
        """
        hashes = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            hashes.append(bytes.fromhex(stripped))
        return cls(hashes)


class RecipientResolver:
    """Resolve each recipient hash to a usable ``RNS.Destination``, cached.

    A recipient that cannot be resolved yet (no announce seen) is retried on
    a later ``refresh()`` rather than failing permanently.
    """

    def __init__(
        self,
        recipients: RecipientList,
        *,
        path_timeout: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self._recipients = recipients
        self._path_timeout = path_timeout
        self._sleep = sleep
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._resolved: dict[bytes, RNS.Destination] = {}

    def refresh(self) -> None:
        """Attempt to resolve every not-yet-resolved recipient."""
        for destination_hash in self._recipients:
            if destination_hash not in self._resolved:
                self._try_resolve_one(destination_hash)

    def _try_resolve_one(self, destination_hash: bytes) -> None:
        if not RNS.Transport.has_path(destination_hash):
            RNS.Transport.request_path(destination_hash)
            deadline = self._clock() + self._path_timeout
            while not RNS.Transport.has_path(destination_hash):
                if self._clock() >= deadline:
                    self._logger.warning(
                        "no path yet to recipient %s; will retry later",
                        destination_hash.hex(),
                    )
                    return
                self._sleep(0.1)

        identity = RNS.Identity.recall(destination_hash)
        if identity is None:
            self._logger.warning(
                "path known but identity not yet recalled for recipient %s",
                destination_hash.hex(),
            )
            return

        destination = RNS.Destination(
            identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            APP_NAME,
            BRIDGE_DESTINATION_ASPECT,
        )
        self._resolved[destination_hash] = destination
        self._logger.info("resolved recipient %s", destination_hash.hex())

    @property
    def resolved_count(self) -> int:
        return len(self._resolved)

    def destinations(self) -> list[RNS.Destination]:
        """Currently resolved recipients, ready to receive a packet."""
        return list(self._resolved.values())


def fanout_forward(
    wire: bytes,
    *,
    resolver: RecipientResolver,
    egress: ReticulumEgressAdapter,
    logger: logging.Logger | None = None,
) -> int:
    """Queue one Reticulum packet per currently resolved recipient.

    Returns how many recipients the envelope was successfully queued for.
    Reticulum's own delivery/timeout callbacks (see
    ``ReticulumEgressAdapter``) handle each recipient's send independently
    from here on, so one recipient's bounded queue being full does not
    affect delivery to any other.
    """
    log = logger or logging.getLogger(__name__)
    envelope = BridgeEnvelope.decode(wire)
    sent = 0
    for destination in resolver.destinations():
        if egress.offer(destination, envelope):
            sent += 1
        else:
            log.warning(
                "recipient queue full; dropping envelope id=%s for one recipient",
                envelope.message_id.hex(),
            )
    return sent


def make_meshtastic_on_forward(
    *,
    engine: BridgeEngine,
    resolver: RecipientResolver,
    egress: ReticulumEgressAdapter,
    clock: Callable[[], float] = time.monotonic,
    logger: logging.Logger | None = None,
) -> Callable[[BridgeEnvelope], None]:
    """Build the Meshtastic-side counterpart to ``BridgeDestination``.

    ``BridgeDestination`` (``rns_meshtastic_bridge.reticulum_destination``) already shows
    this shape for the Reticulum-received direction: validate, run
    ``BridgeEngine.inspect``, and on ``FORWARD`` hand the decision's wire
    bytes to a callback. This function builds the equivalent callback for
    envelopes arriving from Meshtastic, so a future combined process can wire
    ``MeshtasticReceiveAdapter``'s completed envelopes through
    ``BridgeEngine.inspect(ingress=MESHTASTIC)`` the same way.
    """
    log = logger or logging.getLogger(__name__)

    def on_complete_envelope(envelope: BridgeEnvelope) -> None:
        decision = engine.inspect(
            envelope.encode(), ingress=Ingress.MESHTASTIC, now=clock()
        )
        if decision.action is not BridgeAction.FORWARD:
            log.info(
                "dropped envelope id=%s action=%s",
                envelope.message_id.hex(),
                decision.action.value,
            )
            return
        assert decision.wire is not None
        sent = fanout_forward(decision.wire, resolver=resolver, egress=egress, logger=log)
        log.info(
            "forwarded envelope id=%s to %d recipient(s)",
            envelope.message_id.hex(),
            sent,
        )

    return on_complete_envelope
