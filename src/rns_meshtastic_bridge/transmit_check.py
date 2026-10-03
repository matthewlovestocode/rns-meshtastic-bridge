"""Operator tool for a controlled, one-message Meshtastic on-air test.

It defaults to a dry run that never imports Meshtastic or opens a serial
device; actually transmitting requires ``--live`` plus two explicit
confirmation flags, and even then this tool refuses to send unless the live
device reports exactly the expected region and modem preset (US915 /
MEDIUM_FAST by default — edit the constants below for your own deployment).
It never writes a device setting — like ``deploy/check-hardware.sh``, it
only reads configuration to verify it.

This module intentionally hardcodes the expected region/preset as plain
protobuf enum integers instead of importing ``meshtastic``'s generated
protobuf module, the same way ``rns_meshtastic_bridge.meshtastic_adapter`` hardcodes
``RETICULUM_TUNNEL_PORT`` instead of importing Meshtastic's portnums module.
That keeps this file importable and unit-testable without the edge-only
``meshtastic`` package installed. The numbers come from the same .proto
definition both the firmware and the Python client generate from
(``meshtastic/firmware``'s ``config.pb.h``): ``RegionCode.US = 1``,
``ModemPreset.MEDIUM_FAST = 4``.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import time
from typing import Any, Callable, Protocol

from rns_meshtastic_bridge.airtime import TokenBucketLimiter
from rns_meshtastic_bridge.engine import BridgeEngine
from rns_meshtastic_bridge.fragments import fragment_message
from rns_meshtastic_bridge.meshtastic_adapter import RETICULUM_TUNNEL_PORT
from rns_meshtastic_bridge.meshtastic_transmit import MeshtasticTransmitAdapter


EXPECTED_REGION_CODE = 1  # meshtastic.protobuf.config_pb2 Config.LoRaConfig.RegionCode.US
EXPECTED_REGION_NAME = "US"
EXPECTED_PRESET_CODE = 4  # Config.LoRaConfig.ModemPreset.MEDIUM_FAST
EXPECTED_PRESET_NAME = "MEDIUM_FAST"

# A fixed bridge ID makes this tool's test envelopes recognizable in logs; it
# is not a secret and carries no routing meaning outside this one-off test.
TEST_BRIDGE_ID = b"transmit-test-id"


class DeviceConfigMismatch(RuntimeError):
    """The live device's region or modem preset does not match what's expected."""


class LocalLoraConfig(Protocol):
    """The two already-configured fields this tool reads, and never writes."""

    region: int
    modem_preset: int


class RealMeshtasticInterface(Protocol):
    """The subset of a connected Meshtastic client interface this tool uses."""

    localNode: Any

    def sendData(
        self, data: bytes, *, portNum: int, channelIndex: int, wantAck: bool
    ) -> object: ...

    def close(self) -> None: ...


def plan_frames(message: str) -> list[bytes]:
    """Fragment one test envelope exactly as a live send would, with no I/O."""
    engine = BridgeEngine(TEST_BRIDGE_ID, id_factory=lambda length: b"t" * length)
    envelope = engine.originate(message.encode("utf-8"), now=0)
    return fragment_message(envelope.message_id, envelope.encode())


def describe_plan(
    message: str, *, channel: int, rate_per_second: float, burst: int
) -> str:
    """A human-readable summary of what a live run would do; no I/O."""
    frames = plan_frames(message)
    total_bytes = sum(len(frame) for frame in frames)
    return (
        f"would send {len(frames)} frame(s), {total_bytes} total byte(s), "
        f"on channel {channel}, port {RETICULUM_TUNNEL_PORT}, "
        f"rate_per_second={rate_per_second}, burst={burst}"
    )


def verify_device_config(local_config: LocalLoraConfig) -> None:
    """Refuse to continue unless the live device matches the decided config."""
    region, preset = local_config.region, local_config.modem_preset
    if region != EXPECTED_REGION_CODE or preset != EXPECTED_PRESET_CODE:
        raise DeviceConfigMismatch(
            f"device reports region={region!r} modem_preset={preset!r}, "
            f"expected region={EXPECTED_REGION_NAME} ({EXPECTED_REGION_CODE}) "
            f"modem_preset={EXPECTED_PRESET_NAME} ({EXPECTED_PRESET_CODE}); "
            "refusing to transmit. This tool never changes device config; set "
            "it yourself with the meshtastic CLI if this mismatch is intentional."
        )


class SerialFrameTransmitter:
    """Adapts a real client interface to ``rns_meshtastic_bridge.meshtastic_transmit``'s protocol."""

    def __init__(self, interface: RealMeshtasticInterface) -> None:
        self._interface = interface

    def send_frame(self, frame: bytes, *, channel_index: int, port: int) -> None:
        self._interface.sendData(
            frame, portNum=port, channelIndex=channel_index, wantAck=False
        )


def run_live(
    *,
    message: str,
    channel: int,
    rate_per_second: float,
    burst: int,
    interface: RealMeshtasticInterface,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Verify the live device, then fragment, rate-limit, and send one message.

    Returns the number of frames actually sent. Raises ``DeviceConfigMismatch``
    without sending anything if the device's region/preset do not match.
    """
    verify_device_config(interface.localNode.localConfig.lora)

    limiter = TokenBucketLimiter(rate_per_second=rate_per_second, burst=burst, now=clock())
    adapter = MeshtasticTransmitAdapter(channel_index=channel, limiter=limiter, enabled=True)
    engine = BridgeEngine(TEST_BRIDGE_ID, id_factory=lambda length: b"t" * length)
    envelope = engine.originate(message.encode("utf-8"), now=clock())
    if not adapter.enqueue(envelope):
        raise RuntimeError("test message did not fit in the outbound queue")

    transmitter = SerialFrameTransmitter(interface)
    sent_total = 0
    while adapter.queued_frames > 0:
        sent_total += adapter.send_ready(now=clock(), transmitter=transmitter)
        if adapter.queued_frames > 0:
            sleep(0.2)
    return sent_total


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default=None, help="serial device path; required with --live")
    parser.add_argument("--channel", type=int, required=True)
    parser.add_argument("--rate", type=float, required=True, dest="rate_per_second")
    parser.add_argument("--burst", type=int, default=1)
    parser.add_argument("--message", required=True)
    parser.add_argument(
        "--live", action="store_true", help="actually transmit; omit for a dry run (the default)"
    )
    parser.add_argument(
        "--confirm-psk-rotated",
        action="store_true",
        help=(
            "required with --live: confirms the device private key and "
            "channel PSK are not compromised (e.g. were rotated after any "
            "prior exposure, such as printing `meshtastic --info` output "
            "somewhere it shouldn't have gone)"
        ),
    )
    parser.add_argument(
        "--confirm-antenna-connected",
        action="store_true",
        help="required with --live: confirms an antenna is attached before transmitting",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 0 <= args.channel <= 7:
        print("error: --channel must be between 0 and 7", file=sys.stderr)
        return 2

    print(
        describe_plan(
            args.message,
            channel=args.channel,
            rate_per_second=args.rate_per_second,
            burst=args.burst,
        )
    )

    if not args.live:
        print("dry run only; pass --live to actually transmit")
        return 0

    missing = [
        flag
        for flag, present in (
            ("--serial", bool(args.serial)),
            ("--confirm-psk-rotated", args.confirm_psk_rotated),
            ("--confirm-antenna-connected", args.confirm_antenna_connected),
        )
        if not present
    ]
    if missing:
        print(
            "error: --live requires " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2

    # Hardware-only dependency imported at the outer process boundary, the
    # same pattern rns_meshtastic_bridge.receive_service uses to keep this module importable
    # in environments without the edge-only meshtastic package installed.
    serial_module = importlib.import_module("meshtastic.serial_interface")
    interface = serial_module.SerialInterface(devPath=args.serial)
    try:
        sent = run_live(
            message=args.message,
            channel=args.channel,
            rate_per_second=args.rate_per_second,
            burst=args.burst,
            interface=interface,
        )
    finally:
        interface.close()
    print(f"sent {sent} frame(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
