"""Hardware-free end-to-end simulation of the bridge receive pipeline.

This is intentionally more than a unit test helper: operators can run it on a
laptop, Lightsail, or the Mac mini to prove that all pure bridge layers agree
on their wire formats without opening USB or transmitting LoRa.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import Enum

from rns_meshtastic_bridge.engine import BridgeAction, BridgeEngine, Ingress
from rns_meshtastic_bridge.meshtastic_adapter import (
    MeshtasticReceiveAdapter,
    PacketDisposition,
    RETICULUM_TUNNEL_NAME,
)
from rns_meshtastic_bridge.receive_service import ReceiveMonitor


SOURCE_BRIDGE_ID = b"local-source-id!"
RECEIVER_BRIDGE_ID = b"local-receiver!!"
SIMULATED_MESSAGE_ID = b"local-message-id"


class SimulationOutcome(Enum):
    """Final result visible to an operator or integration assertion."""

    FORWARDED = "forwarded"
    INCOMPLETE = "incomplete"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class SimulationReport:
    outcome: SimulationOutcome
    payload_bytes: int
    generated_frames: int
    delivered_frames: int
    forwarded_hops: int | None = None


def simulate_pipeline(
    payload: bytes,
    *,
    channel_index: int = 1,
    delivered_channel: int | None = None,
    drop_index: int | None = None,
    corrupt_index: int | None = None,
    duplicate_index: int | None = None,
) -> SimulationReport:
    """Run a deterministic message through every non-hardware bridge layer.

    Frames are delivered in reverse order to prove reassembly does not depend
    on friendly ordering. Optional fault controls model the failures most
    likely on a constrained radio link.
    """
    source = BridgeEngine(
        SOURCE_BRIDGE_ID,
        id_factory=lambda _length: SIMULATED_MESSAGE_ID,
    )
    envelope = source.originate(payload, now=0)
    adapter = MeshtasticReceiveAdapter(channel_index=channel_index)
    monitor = ReceiveMonitor(adapter, clock=lambda: 1)
    receiver = BridgeEngine(RECEIVER_BRIDGE_ID)
    frames = adapter.frames_for(envelope)

    indexed_frames = list(enumerate(frames))
    if drop_index is not None:
        indexed_frames = [item for item in indexed_frames if item[0] != drop_index]
    if duplicate_index is not None:
        matches = [item for item in indexed_frames if item[0] == duplicate_index]
        indexed_frames.extend(matches)

    delivered = 0
    complete_envelope = None
    malformed = False
    for index, original_frame in reversed(indexed_frames):
        frame = original_frame
        if corrupt_index == index:
            # Alter payload data, not the header, so whole-message CRC checking
            # is the layer that detects the fault.
            frame = frame[:-1] + bytes([frame[-1] ^ 1])
        callback = {
            "channel": channel_index if delivered_channel is None else delivered_channel,
            "decoded": {
                "portnum": RETICULUM_TUNNEL_NAME,
                "payload": frame,
            },
        }
        result = monitor.receive_callback(callback)
        delivered += 1
        if result.disposition is PacketDisposition.MALFORMED:
            malformed = True
        elif result.disposition is PacketDisposition.COMPLETE:
            complete_envelope = result.envelope

    if malformed:
        return SimulationReport(
            SimulationOutcome.MALFORMED,
            len(payload),
            len(frames),
            delivered,
        )
    if complete_envelope is None:
        return SimulationReport(
            SimulationOutcome.INCOMPLETE,
            len(payload),
            len(frames),
            delivered,
        )

    decision = receiver.inspect(
        complete_envelope.encode(),
        ingress=Ingress.MESHTASTIC,
        now=2,
    )
    assert decision.action is BridgeAction.FORWARD
    assert decision.wire is not None
    forwarded = type(complete_envelope).decode(decision.wire)
    return SimulationReport(
        SimulationOutcome.FORWARDED,
        len(payload),
        len(frames),
        delivered,
        forwarded_hops=forwarded.hops,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--payload-bytes",
        type=int,
        default=1024,
        help="synthetic payload size; bytes are generated locally and never sent",
    )
    args = parser.parse_args(argv)
    if args.payload_bytes < 0:
        parser.error("--payload-bytes must not be negative")

    report = simulate_pipeline(b"x" * args.payload_bytes)
    print(
        f"outcome={report.outcome.value} payload_bytes={report.payload_bytes} "
        f"frames={report.generated_frames} delivered={report.delivered_frames} "
        f"forwarded_hops={report.forwarded_hops}"
    )
    return 0 if report.outcome is SimulationOutcome.FORWARDED else 1


if __name__ == "__main__":
    raise SystemExit(main())
