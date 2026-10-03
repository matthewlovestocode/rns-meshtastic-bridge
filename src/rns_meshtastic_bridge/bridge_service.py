"""The combined live bridge process: one Meshtastic connection, one Reticulum
destination, one shared ``BridgeEngine``.

This is deliberately one process, not two: a Meshtastic ``SerialInterface``
only supports one exclusive owner, and receiving via pubsub plus sending via
``sendData()`` both go through that same open connection, so a real
bidirectional bridge cannot be split across two processes the way
receive-only testing elsewhere in this package is.

``BridgeService`` is pure glue with no I/O of its own: two callbacks (one per
network) handle what arrives, and ``tick()`` is called periodically by the
real process loop to perform the only actions that must never happen inside
a receive callback — draining outbound queues, i.e. the actual network
sends, and resolving recipients (which can block on path discovery).

Transmission stays disabled by default everywhere in this module: ``run()``
only constructs a `MeshtasticTransmitAdapter(enabled=True)` when the operator
passes ``--enable-transmit`` *and* ``--rate`` explicitly, and even then
refuses to proceed unless the live device's region/preset still match the
expected values (the same ``verify_device_config()`` check
``rns_meshtastic_bridge.transmit_check`` already uses).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import importlib
import logging
import os
import signal
import threading
import time

import RNS

from rns_meshtastic_bridge.airtime import TokenBucketLimiter
from rns_meshtastic_bridge.channel_util import ChannelUtilizationGate
from rns_meshtastic_bridge.dedup_persistence import load_snapshot, save_snapshot
from rns_meshtastic_bridge.engine import BridgeEngine
from rns_meshtastic_bridge.envelope import BridgeEnvelope
from rns_meshtastic_bridge.meshtastic_adapter import MeshtasticReceiveAdapter, PacketDisposition
from rns_meshtastic_bridge.meshtastic_transmit import FrameTransmitter, MeshtasticTransmitAdapter
from rns_meshtastic_bridge.receive_service import RECEIVE_TOPIC
from rns_meshtastic_bridge.recipients import RecipientList, RecipientResolver, make_meshtastic_on_forward
from rns_meshtastic_bridge.reticulum_adapter import ReticulumEgressAdapter
from rns_meshtastic_bridge.reticulum_destination import BridgeDestination, load_or_create_identity
from rns_meshtastic_bridge.sender_auth import SenderAllowlist
from rns_meshtastic_bridge.transmit_check import DeviceConfigMismatch, SerialFrameTransmitter, verify_device_config
from rns_meshtastic_bridge.protocol import APP_NAME, BRIDGE_ANNOUNCE_APP_DATA, BRIDGE_DESTINATION_ASPECT


TELEMETRY_TOPIC = "meshtastic.receive.telemetry"
DESTINATION_FILENAME = "bridge.destination"


class BridgeService:
    """Ties both directions to one shared ``BridgeEngine``; no I/O itself."""

    def __init__(
        self,
        *,
        meshtastic_adapter: MeshtasticReceiveAdapter,
        meshtastic_on_complete: Callable[[BridgeEnvelope], None],
        egress: ReticulumEgressAdapter,
        resolver: RecipientResolver,
        transmit_adapter: MeshtasticTransmitAdapter | None,
        channel_gate: ChannelUtilizationGate,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self._meshtastic_adapter = meshtastic_adapter
        self._meshtastic_on_complete = meshtastic_on_complete
        self._egress = egress
        self._resolver = resolver
        self._transmit_adapter = transmit_adapter
        self._channel_gate = channel_gate
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)

    def meshtastic_receive_callback(
        self, packet: object, interface: object | None = None, **event_metadata: object
    ) -> None:
        """Meshtastic's callback signature for the port-76 data topic."""
        del interface, event_metadata
        result = self._meshtastic_adapter.receive(packet, now=self._clock())
        if result.disposition is PacketDisposition.COMPLETE:
            assert result.envelope is not None
            self._meshtastic_on_complete(result.envelope)
        elif result.disposition is PacketDisposition.MALFORMED:
            # Structural facts only, matching rns_meshtastic_bridge.receive_service's
            # logging: never the payload itself.
            self._logger.warning("discarded malformed port-76 packet: %s", result.error)

    def meshtastic_telemetry_callback(
        self, packet: object, interface: object | None = None, **event_metadata: object
    ) -> None:
        """Feed the channel-utilization gate from the device's own telemetry.

        Silently ignores anything that isn't this device's own DeviceMetrics
        telemetry (most telemetry on a shared mesh is from other nodes, and
        their readings describe RF conditions at their location, not ours).
        """
        del event_metadata
        if not isinstance(packet, dict) or interface is None:
            return
        my_node_num = getattr(getattr(interface, "myInfo", None), "my_node_num", None)
        if my_node_num is None or packet.get("from") != my_node_num:
            return

        decoded = packet.get("decoded")
        telemetry = decoded.get("telemetry") if isinstance(decoded, dict) else None
        metrics = telemetry.get("deviceMetrics") if isinstance(telemetry, dict) else None
        if not isinstance(metrics, dict):
            return
        channel_util = metrics.get("channelUtilization")
        air_util = metrics.get("airUtilTx")
        if not isinstance(channel_util, (int, float)) or not isinstance(air_util, (int, float)):
            return

        self._channel_gate.record_telemetry(
            channel_utilization_percent=float(channel_util),
            air_util_tx_percent=float(air_util),
            now=self._clock(),
        )

    def reticulum_on_forward(self, wire: bytes) -> None:
        """Wired as ``BridgeDestination``'s ``on_forward``; never sends directly.

        Only queues; the actual Meshtastic send happens from ``tick()``, the
        same split ``rns_meshtastic_bridge.recipients.fanout_forward`` already uses for the
        opposite direction (``ReticulumEgressAdapter.offer()`` queues,
        ``send_next()`` sends).
        """
        if self._transmit_adapter is None:
            self._logger.warning(
                "forward decision reached but transmission is disabled; "
                "dropping %d wire byte(s)",
                len(wire),
            )
            return
        envelope = BridgeEnvelope.decode(wire)
        if not self._transmit_adapter.enqueue(envelope):
            self._logger.warning(
                "Meshtastic outbound queue full; dropping envelope id=%s",
                envelope.message_id.hex(),
            )

    def tick(self, *, now: float, meshtastic_transmitter: FrameTransmitter | None = None) -> None:
        """Drain outbound queues and refresh recipients.

        Call this periodically from the process's own loop — never from
        inside a receive callback, since both actions here can perform real
        network I/O or block on path discovery.
        """
        while self._egress.send_next() is not None:
            pass
        self._resolver.refresh()
        if self._transmit_adapter is not None and meshtastic_transmitter is not None:
            if self._channel_gate.is_send_allowed(now=now):
                self._transmit_adapter.send_ready(now=now, transmitter=meshtastic_transmitter)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Combined live bridge: one Meshtastic connection, one Reticulum destination"
    )
    parser.add_argument("--serial", required=True, help="Meshtastic serial device path")
    parser.add_argument("--channel", type=int, required=True, help="Meshtastic channel index (0-7)")
    parser.add_argument("--reticulum-config", default=os.path.expanduser("~/.reticulum-bridge"))
    parser.add_argument("--allowlist-file", required=True, help="authorized Reticulum senders")
    parser.add_argument("--recipients-file", required=True, help="Meshtastic-forward recipients")
    parser.add_argument("--announce-interval", type=float, default=60)
    parser.add_argument("--tick-interval", type=float, default=1.0)
    parser.add_argument(
        "--persist-interval", type=float, default=300,
        help="how often to save the duplicate-cache snapshot; see --dedup-snapshot",
    )
    parser.add_argument(
        "--dedup-snapshot", default=None,
        help="path to persist/restore duplicate-suppression state across restarts; omit to disable",
    )
    parser.add_argument(
        "--region-duty-cycle-percent", type=float, default=100.0,
        help="the Meshtastic region's firmware duty-cycle percent (100 = unlimited, e.g. US915)",
    )
    parser.add_argument(
        "--enable-transmit", action="store_true",
        help="allow Meshtastic transmission; requires --rate. Off by default.",
    )
    parser.add_argument("--rate", type=float, default=None, help="frames/sec; required with --enable-transmit")
    parser.add_argument("--burst", type=int, default=1)
    return parser


def run(
    *,
    serial_path: str,
    meshtastic_channel: int,
    reticulum_config: str,
    allowlist: SenderAllowlist,
    recipients: RecipientList,
    announce_interval: float,
    tick_interval: float,
    persist_interval: float,
    dedup_snapshot_path: str | None,
    region_duty_cycle_percent: float,
    enable_transmit: bool,
    transmit_rate: float | None,
    transmit_burst: int,
    stop_event: threading.Event,
) -> None:
    """Attach to both networks and run until ``stop_event`` is set."""
    if enable_transmit and transmit_rate is None:
        raise ValueError("--enable-transmit requires --rate")

    logger = logging.getLogger(__name__)

    os.makedirs(reticulum_config, mode=0o700, exist_ok=True)
    RNS.Reticulum(reticulum_config)

    identity = load_or_create_identity(reticulum_config)
    if identity.hash is None:
        raise RuntimeError("Reticulum identity has no hash; cannot derive a bridge ID")
    engine = BridgeEngine(identity.hash)

    if dedup_snapshot_path is not None:
        snapshot = load_snapshot(dedup_snapshot_path)
        engine.restore_duplicates(snapshot, now=time.monotonic(), wall_clock_now=time.time())
        logger.info(
            "restored %d duplicate-cache entr(y/ies) from %s",
            len(snapshot.entries),
            dedup_snapshot_path,
        )

    egress = ReticulumEgressAdapter()
    resolver = RecipientResolver(recipients)
    channel_gate = ChannelUtilizationGate(region_duty_cycle_percent=region_duty_cycle_percent)
    meshtastic_on_complete = make_meshtastic_on_forward(engine=engine, resolver=resolver, egress=egress)

    # Hardware-only dependencies are imported at the outer process boundary,
    # the same pattern rns_meshtastic_bridge.receive_service uses to keep this module
    # importable without the edge-only meshtastic package installed.
    serial_module = importlib.import_module("meshtastic.serial_interface")
    pubsub_module = importlib.import_module("pubsub")
    pub = pubsub_module.pub

    connection = serial_module.SerialInterface(devPath=serial_path)
    transmit_adapter: MeshtasticTransmitAdapter | None = None
    transmitter: FrameTransmitter | None = None
    try:
        if enable_transmit:
            assert transmit_rate is not None
            # Refuses to proceed if the live device no longer matches the
            # expected region/preset, the same gate
            # rns_meshtastic_bridge.transmit_check.run_live() applies before its own send.
            verify_device_config(connection.localNode.localConfig.lora)
            limiter = TokenBucketLimiter(
                rate_per_second=transmit_rate, burst=transmit_burst, now=time.monotonic()
            )
            transmit_adapter = MeshtasticTransmitAdapter(
                channel_index=meshtastic_channel, limiter=limiter, enabled=True
            )
            transmitter = SerialFrameTransmitter(connection)
            logger.info("Meshtastic transmission enabled at %.4f frame(s)/sec", transmit_rate)
        else:
            logger.info("Meshtastic transmission disabled (default)")

        service = BridgeService(
            meshtastic_adapter=MeshtasticReceiveAdapter(channel_index=meshtastic_channel),
            meshtastic_on_complete=meshtastic_on_complete,
            egress=egress,
            resolver=resolver,
            transmit_adapter=transmit_adapter,
            channel_gate=channel_gate,
        )

        destination = RNS.Destination(
            identity,
            RNS.Destination.IN,
            RNS.Destination.SINGLE,
            APP_NAME,
            BRIDGE_DESTINATION_ASPECT,
        )
        destination.set_proof_strategy(RNS.Destination.PROVE_ALL)
        bridge_destination = BridgeDestination(
            engine, allowlist, on_forward=service.reticulum_on_forward
        )
        destination.set_packet_callback(bridge_destination.packet_received)

        destination_path = os.path.join(reticulum_config, DESTINATION_FILENAME)
        with open(destination_path, "w", encoding="utf-8") as destination_file:
            destination_file.write(destination.hexhash + "\n")
        logger.info("Bridge destination: %s", destination.hexhash)

        pub.subscribe(service.meshtastic_receive_callback, RECEIVE_TOPIC)
        pub.subscribe(service.meshtastic_telemetry_callback, TELEMETRY_TOPIC)
        try:
            next_announce = 0.0
            next_persist = 0.0
            while not stop_event.wait(tick_interval):
                now = time.monotonic()
                service.tick(now=now, meshtastic_transmitter=transmitter)

                if now >= next_announce:
                    destination.announce(app_data=BRIDGE_ANNOUNCE_APP_DATA)
                    next_announce = now + announce_interval

                if dedup_snapshot_path is not None and now >= next_persist:
                    save_snapshot(
                        dedup_snapshot_path,
                        engine.snapshot_duplicates(now=now, wall_clock_now=time.time()),
                    )
                    next_persist = now + persist_interval
        finally:
            pub.unsubscribe(service.meshtastic_receive_callback, RECEIVE_TOPIC)
            pub.unsubscribe(service.meshtastic_telemetry_callback, TELEMETRY_TOPIC)
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not 0 <= args.channel <= 7:
        logging.getLogger(__name__).error("--channel must be between 0 and 7")
        return 2
    if args.enable_transmit and args.rate is None:
        logging.getLogger(__name__).error("--enable-transmit requires --rate")
        return 2

    with open(args.allowlist_file, encoding="utf-8") as allowlist_file:
        allowlist = SenderAllowlist.from_hex_lines(allowlist_file.read())
    with open(args.recipients_file, encoding="utf-8") as recipients_file:
        recipients = RecipientList.from_hex_lines(recipients_file.read())
    logging.getLogger(__name__).info(
        "loaded %d authorized sender(s), %d recipient(s)", len(allowlist), len(recipients)
    )

    stop_event = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stop_event.set())

    try:
        run(
            serial_path=args.serial,
            meshtastic_channel=args.channel,
            reticulum_config=args.reticulum_config,
            allowlist=allowlist,
            recipients=recipients,
            announce_interval=args.announce_interval,
            tick_interval=args.tick_interval,
            persist_interval=args.persist_interval,
            dedup_snapshot_path=args.dedup_snapshot,
            region_duty_cycle_percent=args.region_duty_cycle_percent,
            enable_transmit=args.enable_transmit,
            transmit_rate=args.rate,
            transmit_burst=args.burst,
            stop_event=stop_event,
        )
    except DeviceConfigMismatch as error:
        logging.getLogger(__name__).error("refusing to start: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
