"""Live Reticulum destination for the bridge's Reticulum-facing side.

A real Reticulum destination whose received packets reach ``BridgeEngine``'s
forward/drop decision, under its own identity and destination aspect
(``protocol.APP_NAME`` / ``protocol.BRIDGE_DESTINATION_ASPECT``).

Every packet is also checked against ``rns_meshtastic_bridge.sender_auth``'s
signature and allowlist gates before anything else happens to it — this is a
small, local, operator-edited allowlist (mirroring Meshtastic firmware's own
``admin_key`` list), not a public, self-service system; a different
identity-management system could replace how the allowlist is populated
without changing how a packet is verified.

On a FORWARD decision, ``BridgeDestination`` calls an injected ``on_forward``
callback with the re-encoded wire bytes meant for the opposite network — the
same wire-bytes boundary ``BridgeDecision.wire`` already establishes
elsewhere in this package (see ``rns_meshtastic_bridge.local_simulation``).
This module never imports or constructs anything Meshtastic-specific;
``bridge_service.py`` is what wires ``on_forward`` to a real Meshtastic
transmit path.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import logging
import os
import time

import RNS

from rns_meshtastic_bridge.engine import BridgeAction, BridgeEngine, Ingress
from rns_meshtastic_bridge.reticulum_adapter import IngressDisposition, ReticulumIngressAdapter
from rns_meshtastic_bridge.sender_auth import SenderAllowlist, SenderNotAuthorized, SignatureInvalid, unwrap_and_verify
from rns_meshtastic_bridge.protocol import APP_NAME, BRIDGE_ANNOUNCE_APP_DATA, BRIDGE_DESTINATION_ASPECT


IDENTITY_FILENAME = "bridge.identity"
DESTINATION_FILENAME = "bridge.destination"


def load_or_create_identity(config_dir: str) -> RNS.Identity:
    """Load the bridge's private identity, creating it only on the first run.

    Reusing the identity keeps the destination hash, and therefore the
    derived ``BridgeEngine`` bridge ID (see ``run``), stable across restarts.
    """
    identity_path = os.path.join(config_dir, IDENTITY_FILENAME)

    identity = RNS.Identity.from_file(identity_path)
    if identity is not None:
        return identity

    identity = RNS.Identity()
    # A 077 umask makes the new private-key file readable only by its owner,
    # so only this process's owner can read the private key material.
    old_umask = os.umask(0o077)
    try:
        if not identity.to_file(identity_path):
            raise RuntimeError(f"Could not save identity to {identity_path}")
    finally:
        os.umask(old_umask)
    return identity


class BridgeDestination:
    """Turn one Reticulum destination's packets into forward/drop decisions.

    Every accepted packet passes three gates in order: signature/authorization
    (``rns_meshtastic_bridge.sender_auth`` — is this sender who they claim, and allowed at
    all), structural validation (``ReticulumIngressAdapter``, which never
    raises), then ``BridgeEngine.inspect`` for the actual forward/drop
    decision. Putting authorization first means an unauthorized sender's
    packets never even reach envelope parsing.
    """

    def __init__(
        self,
        engine: BridgeEngine,
        allowlist: SenderAllowlist,
        *,
        on_forward: Callable[[bytes], None],
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self._engine = engine
        self._allowlist = allowlist
        self._ingress = ReticulumIngressAdapter()
        self._on_forward = on_forward
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)

    def packet_received(self, message: bytes, packet: object) -> None:
        """Reticulum's packet callback signature.

        Never raises on malformed or unauthorized input: an unhandled
        exception here would otherwise stop future packets from this
        long-running callback.
        """
        del packet  # Reticulum already proves receipt; nothing else is used.
        try:
            verified = unwrap_and_verify(message, self._allowlist)
        except SenderNotAuthorized as error:
            # The identity hash is a public value proven genuine by a valid
            # signature; logging it (not the payload) helps an operator see
            # who is asking to be added to the allowlist.
            self._logger.warning("rejected unauthorized sender: %s", error)
            return
        except SignatureInvalid as error:
            self._logger.warning("rejected unverifiable packet: %s", error)
            return

        result = self._ingress.receive(verified.envelope_bytes)
        if result.disposition is IngressDisposition.MALFORMED:
            # Structural facts only, matching rns_meshtastic_bridge.receive_service's
            # logging: never the payload itself.
            self._logger.warning("discarded malformed bridge packet: %s", result.error)
            return

        envelope = result.envelope
        assert envelope is not None
        decision = self._engine.inspect(
            envelope.encode(), ingress=Ingress.RETICULUM, now=self._clock()
        )
        if decision.action is BridgeAction.FORWARD:
            assert decision.wire is not None
            self._logger.info(
                "forwarding envelope id=%s payload_bytes=%d hops=%d/%d",
                envelope.message_id.hex(),
                len(envelope.payload),
                envelope.hops,
                envelope.max_hops,
            )
            self._on_forward(decision.wire)
        else:
            self._logger.info(
                "dropped envelope id=%s action=%s",
                envelope.message_id.hex(),
                decision.action.value,
            )


def run(
    config_dir: str,
    announce_interval: int,
    *,
    allowlist: SenderAllowlist,
    on_forward: Callable[[bytes], None],
) -> None:
    """Attach to Reticulum, publish the bridge destination, and wait forever."""
    os.makedirs(config_dir, mode=0o700, exist_ok=True)
    RNS.Reticulum(config_dir)

    identity = load_or_create_identity(config_dir)
    # The identity hash is already a stable 16-byte value derived from this
    # destination's own private key, so it doubles as BridgeEngine's bridge
    # ID without a second file to keep in sync with the identity. RNS types
    # it as optional, but a loaded or freshly created Identity always has one.
    if identity.hash is None:
        raise RuntimeError("Reticulum identity has no hash; cannot derive a bridge ID")
    engine = BridgeEngine(identity.hash)

    destination = RNS.Destination(
        identity,
        RNS.Destination.IN,
        RNS.Destination.SINGLE,
        APP_NAME,
        BRIDGE_DESTINATION_ASPECT,
    )
    # Proving every accepted packet lets a Reticulum-side sender confirm the
    # bridge received it.
    destination.set_proof_strategy(RNS.Destination.PROVE_ALL)
    bridge_destination = BridgeDestination(engine, allowlist, on_forward=on_forward)
    destination.set_packet_callback(bridge_destination.packet_received)

    destination_hex = destination.hexhash
    destination_path = os.path.join(config_dir, DESTINATION_FILENAME)
    with open(destination_path, "w", encoding="utf-8") as destination_file:
        destination_file.write(destination_hex + "\n")

    RNS.log(f"Bridge destination: {destination_hex}", RNS.LOG_NOTICE)
    while True:
        destination.announce(app_data=BRIDGE_ANNOUNCE_APP_DATA)
        RNS.log("Bridge destination announced", RNS.LOG_INFO)
        time.sleep(announce_interval)


def _log_forward(wire: bytes) -> None:
    """Placeholder on_forward used until a real Meshtastic egress exists."""
    logging.getLogger(__name__).warning(
        "forward decision reached but no Meshtastic egress is wired up yet; "
        "dropping %d wire byte(s)",
        len(wire),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reticulum-facing half of the bridge (receive-and-decide only)"
    )
    parser.add_argument("--config", default=os.path.expanduser("~/.reticulum-bridge"))
    parser.add_argument("--announce-interval", type=int, default=60)
    parser.add_argument(
        "--allowlist-file",
        required=True,
        help=(
            "path to a file of hex-encoded authorized sender identity hashes, "
            "one per line ('#' comments allowed); there is no implicit "
            "default, so a forgotten flag fails loudly instead of silently "
            "authorizing everyone or no one"
        ),
    )
    return parser


def main() -> None:
    """Parse service options and start the bridge destination."""
    args = _parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    with open(args.allowlist_file, encoding="utf-8") as allowlist_file:
        allowlist = SenderAllowlist.from_hex_lines(allowlist_file.read())
    logging.getLogger(__name__).info(
        "loaded %d authorized sender(s) from %s", len(allowlist), args.allowlist_file
    )
    run(args.config, args.announce_interval, allowlist=allowlist, on_forward=_log_forward)


if __name__ == "__main__":
    main()
