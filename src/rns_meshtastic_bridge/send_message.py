"""Operator tool to originate one authorized bridge message over Reticulum.

This is the sender-side half of ``rns_meshtastic_bridge.sender_auth``. It
signs a ``BridgeEnvelope`` with the sender's own persistent ``RNS.Identity``
(``sign_envelope()``) and sends it to a bridge destination
(``rns_meshtastic_bridge.reticulum_destination`` /
``rns_meshtastic_bridge.bridge_service``), waiting for Reticulum's delivery
proof the same way a Reticulum client ordinarily waits for proof of
delivery.

For a sent message to actually be forwarded anywhere, the sender's identity
hash — printed every run — must already be on the receiving bridge's
``--allowlist-file`` (see ``rns_meshtastic_bridge.sender_auth.SenderAllowlist``). A
successful delivery proof here only means the bridge's destination accepted
and decrypted the packet; ``BridgeDestination`` logs, not this tool, says
whether it was then authorized and forwarded.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import time

import RNS

from rns_meshtastic_bridge.envelope import ID_LENGTH, BridgeEnvelope
from rns_meshtastic_bridge.sender_auth import sign_envelope
from rns_meshtastic_bridge.protocol import APP_NAME, BRIDGE_DESTINATION_ASPECT, encode_message


IDENTITY_FILENAME = "sender.identity"
DEFAULT_MAX_HOPS = 4


def load_or_create_identity(config_dir: str) -> RNS.Identity:
    """Load this sender's private identity, creating it only on the first run.

    Reusing the identity keeps the sender's identity hash — the value that
    must be added to a bridge's allowlist — stable across runs.
    """
    identity_path = os.path.join(config_dir, IDENTITY_FILENAME)

    identity = RNS.Identity.from_file(identity_path)
    if identity is not None:
        return identity

    identity = RNS.Identity()
    old_umask = os.umask(0o077)
    try:
        if not identity.to_file(identity_path):
            raise RuntimeError(f"Could not save identity to {identity_path}")
    finally:
        os.umask(old_umask)
    return identity


def parse_destination(value: str) -> bytes:
    """Validate a printable destination hash and convert it to bytes."""
    expected_length = (RNS.Reticulum.TRUNCATED_HASHLENGTH // 8) * 2
    if len(value) != expected_length:
        raise ValueError(f"destination must be {expected_length} hexadecimal characters")
    return bytes.fromhex(value)


def build_signed_envelope(
    identity: RNS.Identity, payload: bytes, *, max_hops: int = DEFAULT_MAX_HOPS
) -> bytes:
    """Wrap one new envelope with this identity's signature.

    A fresh random message ID is used every call; duplicate suppression on
    the receiving bridge is keyed on that ID, so resending the same text
    produces a distinct, independently-forwardable message, not a retry.
    """
    if identity.hash is None:
        raise ValueError("identity has no usable hash")
    envelope = BridgeEnvelope(
        message_id=secrets.token_bytes(ID_LENGTH),
        origin_id=identity.hash,
        payload=payload,
        hops=0,
        max_hops=max_hops,
    )
    return sign_envelope(identity, envelope.encode())


def run(
    config_dir: str,
    destination_hex: str,
    message: str,
    timeout: float,
    *,
    max_hops: int = DEFAULT_MAX_HOPS,
) -> int:
    """Send one signed message and return a shell-friendly exit code."""
    destination_hash = parse_destination(destination_hex)

    RNS.Reticulum(config_dir)
    identity = load_or_create_identity(config_dir)
    if identity.hash is None:
        print("This sender identity has no usable hash", file=sys.stderr)
        return 1
    print(f"Sender identity (add to the bridge's allowlist): {identity.hash.hex()}")

    if not RNS.Transport.has_path(destination_hash):
        print(f"Requesting path to {destination_hex}...")
        RNS.Transport.request_path(destination_hash)
        deadline = time.monotonic() + timeout
        while not RNS.Transport.has_path(destination_hash):
            if time.monotonic() >= deadline:
                print("No path to the bridge destination was found", file=sys.stderr)
                return 1
            time.sleep(0.1)

    bridge_identity = RNS.Identity.recall(destination_hash)
    if bridge_identity is None:
        print("The bridge destination identity could not be recalled", file=sys.stderr)
        return 1

    destination = RNS.Destination(
        bridge_identity,
        RNS.Destination.OUT,
        RNS.Destination.SINGLE,
        APP_NAME,
        BRIDGE_DESTINATION_ASPECT,
    )

    wire = build_signed_envelope(identity, encode_message(message), max_hops=max_hops)
    receipt = RNS.Packet(destination, wire).send()
    if not isinstance(receipt, RNS.PacketReceipt):
        print("Reticulum could not send the message", file=sys.stderr)
        return 1

    receipt.set_timeout(timeout)
    deadline = time.monotonic() + timeout
    while receipt.status == RNS.PacketReceipt.SENT:
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)

    if receipt.status == RNS.PacketReceipt.DELIVERED:
        print(f"Bridge destination accepted the message in {receipt.get_rtt() * 1000:.1f} ms")
        print(
            "This only confirms the packet decrypted; check the bridge's own "
            "logs for whether it was authorized and forwarded."
        )
        return 0

    print("Delivery was not confirmed before the timeout", file=sys.stderr)
    return 1


def main() -> None:
    """Parse arguments and expose run()'s result as the process exit code."""
    parser = argparse.ArgumentParser(
        description="Send one signed, authorized message to a bridge destination"
    )
    parser.add_argument("destination", help="bridge destination hash")
    parser.add_argument("message", nargs="?", default="hello from rns-meshtastic-bridge")
    parser.add_argument("--config", default=os.path.expanduser("~/.reticulum-bridge-sender"))
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument(
        "--max-hops",
        type=int,
        default=DEFAULT_MAX_HOPS,
        help="hop budget for the envelope once it crosses onto the other network",
    )
    args = parser.parse_args()
    raise SystemExit(
        run(args.config, args.destination, args.message, args.timeout, max_hops=args.max_hops)
    )


if __name__ == "__main__":
    main()
