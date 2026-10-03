"""Live, receive-only Meshtastic soak-test process.

This process proves that the real serial client and our packet parser can run
together for long periods. By default, complete envelopes are counted,
logged, and discarded. An operator may opt into forwarding them to a local
Unix domain socket sink (see ``rns_meshtastic_bridge.local_sink``) instead; that sink never
reaches a network interface. This module still does not import Reticulum and
cannot forward or transmit a packet onto either network.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable
import importlib
import logging
from pathlib import Path
import signal
import threading
import time
from typing import Any, Protocol

from rns_meshtastic_bridge.envelope import BridgeEnvelope
from rns_meshtastic_bridge.local_sink import EnvelopeSink, SinkDeliveryError, UnixSocketSink
from rns_meshtastic_bridge.meshtastic_adapter import (
    MeshtasticReceiveAdapter,
    PacketDisposition,
    ReceiveResult,
    RETICULUM_TUNNEL_NAME,
)


RECEIVE_TOPIC = f"meshtastic.receive.data.{RETICULUM_TUNNEL_NAME}"


class SerialConnection(Protocol):
    """Only the client operation this receive-only service is allowed to use."""

    def close(self) -> None: ...


class PubSub(Protocol):
    """Small seam around pypubsub for deterministic lifecycle tests."""

    def subscribe(self, listener: Callable[..., Any], topic: str) -> Any: ...

    def unsubscribe(self, listener: Callable[..., Any], topic: str) -> Any: ...


class ReceiveMetrics:
    """Thread-safe counters updated by Meshtastic's callback thread."""

    def __init__(self) -> None:
        self._counts: Counter[PacketDisposition] = Counter()
        self._lock = threading.Lock()

    def record(self, disposition: PacketDisposition) -> None:
        with self._lock:
            self._counts[disposition] += 1

    def snapshot(self) -> dict[str, int]:
        """Return all fields, including zeros, for stable log parsing."""
        with self._lock:
            return {
                disposition.value: self._counts[disposition]
                for disposition in PacketDisposition
            }


class ReceiveMonitor:
    """Translate client callbacks into safe logs and aggregate counters."""

    def __init__(
        self,
        adapter: MeshtasticReceiveAdapter,
        *,
        metrics: ReceiveMetrics | None = None,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
        sink: EnvelopeSink | None = None,
    ) -> None:
        self.adapter = adapter
        self.metrics = metrics or ReceiveMetrics()
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        # Opt-in only: the live service passes no sink today, so a validated
        # envelope is logged and discarded exactly as before this milestone.
        self._sink = sink

    def receive_callback(
        self,
        packet: object,
        interface: object | None = None,
        **event_metadata: object,
    ) -> ReceiveResult:
        """Handle the callback signature used by meshtastic-python/pypubsub."""
        # These callback fields are intentionally unused. In particular, the
        # service never calls sendData on the supplied interface.
        del interface, event_metadata
        result = self.adapter.receive(packet, now=self._clock())
        self.metrics.record(result.disposition)

        if result.disposition is PacketDisposition.COMPLETE:
            assert result.envelope is not None
            if self._sink is None:
                self._logger.info(
                    "validated envelope id=%s payload_bytes=%d hops=%d/%d; discarded",
                    result.envelope.message_id.hex(),
                    len(result.envelope.payload),
                    result.envelope.hops,
                    result.envelope.max_hops,
                )
            else:
                self._deliver_to_sink(self._sink, result.envelope)
        elif result.disposition is PacketDisposition.MALFORMED:
            # Parser errors contain structural facts, never payload contents or
            # channel keys, so they are safe to retain in the journal.
            self._logger.warning("discarded malformed port-76 packet: %s", result.error)

        return result

    def _deliver_to_sink(self, sink: EnvelopeSink, envelope: BridgeEnvelope) -> None:
        """Hand a validated envelope to the configured local sink.

        A sink failure is logged and the envelope is still discarded; it must
        never crash the long-running Meshtastic callback thread the way an
        unhandled exception from this callback would.
        """
        try:
            sink.send(envelope.encode())
        except SinkDeliveryError as error:
            self._logger.warning(
                "sink delivery failed for envelope id=%s: %s",
                envelope.message_id.hex(),
                error,
            )
        else:
            self._logger.info(
                "delivered envelope id=%s payload_bytes=%d hops=%d/%d to local sink",
                envelope.message_id.hex(),
                len(envelope.payload),
                envelope.hops,
                envelope.max_hops,
            )

    def log_summary(self) -> None:
        counts = self.metrics.snapshot()
        self._logger.info(
            "receive totals complete=%d partial=%d malformed=%d "
            "ignored_channel=%d ignored_port=%d",
            counts[PacketDisposition.COMPLETE.value],
            counts[PacketDisposition.PARTIAL.value],
            counts[PacketDisposition.MALFORMED.value],
            counts[PacketDisposition.IGNORED_CHANNEL.value],
            counts[PacketDisposition.IGNORED_PORT.value],
        )


def run_monitor(
    monitor: ReceiveMonitor,
    *,
    serial_path: str,
    report_interval: float,
    stop_event: threading.Event,
    interface_factory: Callable[[str], SerialConnection],
    pubsub: PubSub,
) -> None:
    """Own subscription/serial cleanup even during failure or shutdown."""
    if report_interval <= 0:
        raise ValueError("report_interval must be positive")
    if not Path(serial_path).exists():
        raise FileNotFoundError(f"Meshtastic serial device not found: {serial_path}")

    callback = monitor.receive_callback
    connection: SerialConnection | None = None
    pubsub.subscribe(callback, RECEIVE_TOPIC)
    try:
        # Construction opens the serial connection and downloads device state.
        # No writeConfig or sendData call exists anywhere in this service.
        connection = interface_factory(serial_path)
        logging.getLogger(__name__).info(
            "receive-only Meshtastic monitor connected path=%s topic=%s",
            serial_path,
            RECEIVE_TOPIC,
        )
        while not stop_event.wait(report_interval):
            monitor.log_summary()
    finally:
        pubsub.unsubscribe(callback, RECEIVE_TOPIC)
        if connection is not None:
            connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and discard Meshtastic Reticulum port-76 packets"
    )
    parser.add_argument(
        "--serial",
        required=True,
        help="Meshtastic serial device path; prefer a stable /dev/serial/by-id path",
    )
    parser.add_argument(
        "--channel",
        type=int,
        required=True,
        help="Meshtastic channel index to accept (0-7); there is no implicit default",
    )
    parser.add_argument("--report-interval", type=float, default=300)
    parser.add_argument(
        "--sink-path",
        default=None,
        help=(
            "optional Unix domain socket path; when given, validated envelopes "
            "are forwarded there instead of only being logged and discarded. "
            "The socket never reaches a network interface or Reticulum."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Hardware-only dependencies are imported at the outer process boundary.
    # The core and its tests remain usable without installing Meshtastic.
    # Dynamic imports keep the optional edge-only packages from appearing as
    # missing imports in laptop/Lightsail static analysis environments.
    serial_module = importlib.import_module("meshtastic.serial_interface")
    pubsub_module = importlib.import_module("pubsub")
    serial_interface = serial_module.SerialInterface
    pub = pubsub_module.pub

    stop_event = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stop_event.set())

    # Connecting the sink here, before the signal handlers and run_monitor's
    # own try/finally take over, means a missing consumer fails fast instead
    # of silently falling back to discard-only behavior.
    sink = UnixSocketSink.connect(args.sink_path) if args.sink_path else None
    monitor = ReceiveMonitor(
        MeshtasticReceiveAdapter(channel_index=args.channel), sink=sink
    )
    try:
        run_monitor(
            monitor,
            serial_path=args.serial,
            report_interval=args.report_interval,
            stop_event=stop_event,
            interface_factory=lambda path: serial_interface(devPath=path),
            pubsub=pub,
        )
    finally:
        if sink is not None:
            sink.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
