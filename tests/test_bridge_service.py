"""BridgeService's callbacks/tick are tested with mocks; run()/main() wiring
follows tests/test_reticulum_destination.py's monkeypatched-RNS style, plus
an injected stop_event the way tests/test_receive_service.py drives
run_monitor()'s loop.
"""

import logging
from pathlib import Path
import sys
import threading
from typing import cast
from unittest.mock import Mock

import pytest
import RNS

from rns_meshtastic_bridge import bridge_service as module
from rns_meshtastic_bridge.bridge_service import BridgeService
from rns_meshtastic_bridge.channel_util import ChannelUtilizationGate
from rns_meshtastic_bridge.engine import BridgeEngine
from rns_meshtastic_bridge.envelope import BridgeEnvelope
from rns_meshtastic_bridge.fragments import fragment_message
from rns_meshtastic_bridge.meshtastic_adapter import MeshtasticReceiveAdapter, PacketDisposition, RETICULUM_TUNNEL_NAME
from rns_meshtastic_bridge.meshtastic_transmit import MeshtasticTransmitAdapter
from rns_meshtastic_bridge.recipients import RecipientList, RecipientResolver
from rns_meshtastic_bridge.reticulum_adapter import ReticulumEgressAdapter
from rns_meshtastic_bridge.sender_auth import SenderAllowlist, sign_envelope
from rns_meshtastic_bridge.transmit_check import EXPECTED_PRESET_CODE, EXPECTED_REGION_CODE, DeviceConfigMismatch


LOCAL_ID = b"l" * 16
REMOTE_ID = b"r" * 16
MESSAGE_ID = b"m" * 16
RECIPIENT_A = b"a" * 16


def remote_envelope(*, payload: bytes = b"payload") -> BridgeEnvelope:
    return BridgeEnvelope(message_id=MESSAGE_ID, origin_id=REMOTE_ID, payload=payload)


def meshtastic_packet(payload: bytes, *, channel: int = 2) -> dict[str, object]:
    return {"channel": channel, "decoded": {"portnum": RETICULUM_TUNNEL_NAME, "payload": payload}}


def complete_frame(envelope: BridgeEnvelope) -> bytes:
    return fragment_message(envelope.message_id, envelope.encode())[0]


def build_service(
    *, transmit_adapter: MeshtasticTransmitAdapter | None = None, channel_gate: ChannelUtilizationGate | None = None
) -> tuple[BridgeService, Mock, Mock]:
    egress = Mock(spec=ReticulumEgressAdapter)
    egress.send_next.return_value = None
    resolver = Mock(spec=RecipientResolver)
    service = BridgeService(
        meshtastic_adapter=MeshtasticReceiveAdapter(channel_index=2),
        meshtastic_on_complete=Mock(),
        egress=egress,
        resolver=resolver,
        transmit_adapter=transmit_adapter,
        channel_gate=channel_gate or ChannelUtilizationGate(),
        clock=lambda: 0,
    )
    return service, egress, resolver


class TestMeshtasticReceiveCallback:
    def test_complete_envelope_reaches_on_complete(self) -> None:
        on_complete = Mock()
        service, _, _ = build_service()
        service._meshtastic_on_complete = on_complete  # type: ignore[attr-defined]
        envelope = remote_envelope()

        service.meshtastic_receive_callback(meshtastic_packet(complete_frame(envelope)))

        on_complete.assert_called_once_with(envelope)

    def test_malformed_packet_is_logged_without_raising(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        service, _, _ = build_service()
        with caplog.at_level(logging.WARNING):
            service.meshtastic_receive_callback(meshtastic_packet(b"short frame"))
        assert "malformed" in caplog.text

    def test_ignored_port_does_nothing(self) -> None:
        on_complete = Mock()
        service, _, _ = build_service()
        service._meshtastic_on_complete = on_complete  # type: ignore[attr-defined]

        service.meshtastic_receive_callback({"channel": 2, "decoded": {"portnum": 1, "payload": b"x"}})

        on_complete.assert_not_called()


class TestTelemetryCallback:
    def test_records_own_devices_telemetry(self) -> None:
        gate = Mock(spec=ChannelUtilizationGate)
        service, _, _ = build_service(channel_gate=gate)
        interface = Mock()
        interface.myInfo.my_node_num = 42
        packet = {
            "from": 42,
            "decoded": {"telemetry": {"deviceMetrics": {"channelUtilization": 12.5, "airUtilTx": 3.0}}},
        }

        service.meshtastic_telemetry_callback(packet, interface=interface)

        gate.record_telemetry.assert_called_once_with(
            channel_utilization_percent=12.5, air_util_tx_percent=3.0, now=0
        )

    def test_ignores_another_nodes_telemetry(self) -> None:
        gate = Mock(spec=ChannelUtilizationGate)
        service, _, _ = build_service(channel_gate=gate)
        interface = Mock()
        interface.myInfo.my_node_num = 42
        packet = {
            "from": 99,
            "decoded": {"telemetry": {"deviceMetrics": {"channelUtilization": 1, "airUtilTx": 1}}},
        }

        service.meshtastic_telemetry_callback(packet, interface=interface)

        gate.record_telemetry.assert_not_called()

    @pytest.mark.parametrize(
        "packet",
        [
            "not a dict",
            {"from": 42, "decoded": {}},
            {"from": 42, "decoded": {"telemetry": {}}},
            {"from": 42, "decoded": {"telemetry": {"deviceMetrics": {}}}},
            {"from": 42, "decoded": {"telemetry": {"deviceMetrics": {"channelUtilization": "bad", "airUtilTx": 1}}}},
        ],
    )
    def test_malformed_or_incomplete_telemetry_is_ignored(self, packet: object) -> None:
        gate = Mock(spec=ChannelUtilizationGate)
        service, _, _ = build_service(channel_gate=gate)
        interface = Mock()
        interface.myInfo.my_node_num = 42

        service.meshtastic_telemetry_callback(packet, interface=interface)

        gate.record_telemetry.assert_not_called()

    def test_no_interface_is_ignored(self) -> None:
        gate = Mock(spec=ChannelUtilizationGate)
        service, _, _ = build_service(channel_gate=gate)
        service.meshtastic_telemetry_callback({"from": 1, "decoded": {}}, interface=None)
        gate.record_telemetry.assert_not_called()


class TestReticulumOnForward:
    def test_logs_and_drops_when_transmit_is_disabled(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        service, _, _ = build_service(transmit_adapter=None)
        with caplog.at_level(logging.WARNING):
            service.reticulum_on_forward(remote_envelope().encode())
        assert "transmission is disabled" in caplog.text

    def test_enqueues_when_transmit_is_enabled(self) -> None:
        limiter = module.TokenBucketLimiter(rate_per_second=1, burst=1, now=0)
        transmit_adapter = MeshtasticTransmitAdapter(channel_index=2, limiter=limiter, enabled=True)
        service, _, _ = build_service(transmit_adapter=transmit_adapter)

        service.reticulum_on_forward(remote_envelope().encode())

        assert transmit_adapter.queued_frames == 1

    def test_logs_when_the_outbound_queue_is_full(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from rns_meshtastic_bridge.airtime import BoundedFrameQueue

        limiter = module.TokenBucketLimiter(rate_per_second=1, burst=1, now=0)
        tiny_queue = BoundedFrameQueue(max_frames=1, max_bytes=10_000)
        transmit_adapter = MeshtasticTransmitAdapter(
            channel_index=2, limiter=limiter, queue=tiny_queue, enabled=True
        )
        service, _, _ = build_service(transmit_adapter=transmit_adapter)

        with caplog.at_level(logging.WARNING):
            service.reticulum_on_forward(remote_envelope(payload=b"x" * 500).encode())

        assert "queue full" in caplog.text


class TestTick:
    def test_drains_the_reticulum_egress_queue_fully(self) -> None:
        service, egress, _ = build_service()
        egress.send_next.side_effect = [object(), object(), None]

        service.tick(now=0)

        assert egress.send_next.call_count == 3

    def test_refreshes_recipients_every_tick(self) -> None:
        service, _, resolver = build_service()
        service.tick(now=0)
        resolver.refresh.assert_called_once()

    def test_sends_ready_meshtastic_frames_when_channel_is_clear(self) -> None:
        gate = ChannelUtilizationGate()
        gate.record_telemetry(channel_utilization_percent=0, air_util_tx_percent=0, now=0)
        limiter = module.TokenBucketLimiter(rate_per_second=100, burst=100, now=0)
        transmit_adapter = Mock(spec=MeshtasticTransmitAdapter)
        service, _, _ = build_service(transmit_adapter=transmit_adapter, channel_gate=gate)
        transmitter = Mock()

        service.tick(now=0, meshtastic_transmitter=transmitter)

        transmit_adapter.send_ready.assert_called_once_with(now=0, transmitter=transmitter)

    def test_does_not_send_when_the_channel_gate_denies(self) -> None:
        gate = ChannelUtilizationGate()  # No telemetry yet: fails closed.
        transmit_adapter = Mock(spec=MeshtasticTransmitAdapter)
        service, _, _ = build_service(transmit_adapter=transmit_adapter, channel_gate=gate)

        service.tick(now=0, meshtastic_transmitter=Mock())

        transmit_adapter.send_ready.assert_not_called()

    def test_does_nothing_transmit_related_when_disabled(self) -> None:
        service, _, _ = build_service(transmit_adapter=None)
        service.tick(now=0, meshtastic_transmitter=Mock())  # Must not raise.


class TestCli:
    def test_channel_must_be_in_range(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = module.main(
            [
                "--serial", "/dev/fake",
                "--channel", "9",
                "--allowlist-file", "/tmp/does-not-matter",
                "--recipients-file", "/tmp/does-not-matter",
            ]
        )
        assert exit_code == 2

    def test_enable_transmit_requires_rate(self) -> None:
        exit_code = module.main(
            [
                "--serial", "/dev/fake",
                "--channel", "1",
                "--allowlist-file", "/tmp/does-not-matter",
                "--recipients-file", "/tmp/does-not-matter",
                "--enable-transmit",
            ]
        )
        assert exit_code == 2

    def test_run_rejects_enable_transmit_without_rate_directly(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="requires --rate"):
            module.run(
                serial_path="/dev/fake",
                meshtastic_channel=1,
                reticulum_config=str(tmp_path),
                allowlist=SenderAllowlist([]),
                recipients=RecipientList([]),
                announce_interval=60,
                tick_interval=1,
                persist_interval=300,
                dedup_snapshot_path=None,
                region_duty_cycle_percent=100,
                enable_transmit=True,
                transmit_rate=None,
                transmit_burst=1,
                stop_event=threading.Event(),
            )


@pytest.fixture
def empty_policy() -> tuple[SenderAllowlist, RecipientList]:
    # Built before any test monkeypatches RNS.Reticulum, since RecipientList's
    # constructor reads the real RNS.Reticulum.TRUNCATED_HASHLENGTH class
    # attribute, which a patched-out Reticulum class would no longer have.
    return SenderAllowlist([]), RecipientList([])


class TestRun:
    def _patch_rns(self, monkeypatch: pytest.MonkeyPatch) -> tuple[list[object], type]:
        created: list[object] = []

        class FakeDestination:
            IN = 1
            SINGLE = 2
            PROVE_ALL = 3

            def __init__(self, *_args: object) -> None:
                self.hexhash = "ab" * 16
                created.append(self)

            def set_proof_strategy(self, _strategy: int) -> None:
                pass

            def set_packet_callback(self, _callback: object) -> None:
                pass

            def announce(self, app_data: bytes) -> None:
                del app_data

        fake_identity = cast(RNS.Identity, type("I", (), {"hash": b"i" * 16})())
        monkeypatch.setattr(module.RNS, "Reticulum", lambda _config: None)
        monkeypatch.setattr(module.RNS, "Destination", FakeDestination)
        monkeypatch.setattr(module, "load_or_create_identity", lambda _config: fake_identity)
        return created, FakeDestination

    def _fake_serial_and_pubsub(self, monkeypatch: pytest.MonkeyPatch) -> Mock:
        connection = Mock()
        serial_module = Mock()
        serial_module.SerialInterface.return_value = connection
        pubsub_module = Mock()

        def fake_import(name: str) -> object:
            if name == "meshtastic.serial_interface":
                return serial_module
            if name == "pubsub":
                return pubsub_module
            raise AssertionError(f"unexpected import: {name}")

        monkeypatch.setattr(module.importlib, "import_module", fake_import)
        return connection

    def test_run_subscribes_and_cleans_up(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        empty_policy: tuple[SenderAllowlist, RecipientList],
    ) -> None:
        self._patch_rns(monkeypatch)
        connection = self._fake_serial_and_pubsub(monkeypatch)
        stop_event = Mock(spec=threading.Event)
        stop_event.wait.return_value = True  # Exit the loop immediately.

        module.run(
            serial_path="/dev/fake",
            meshtastic_channel=1,
            reticulum_config=str(tmp_path),
            allowlist=empty_policy[0],
            recipients=empty_policy[1],
            announce_interval=60,
            tick_interval=1,
            persist_interval=300,
            dedup_snapshot_path=None,
            region_duty_cycle_percent=100,
            enable_transmit=False,
            transmit_rate=None,
            transmit_burst=1,
            stop_event=stop_event,
        )

        connection.close.assert_called_once()
        assert (tmp_path / module.DESTINATION_FILENAME).read_text(encoding="utf-8") == "ab" * 16 + "\n"

    def test_run_ticks_until_stopped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        empty_policy: tuple[SenderAllowlist, RecipientList],
    ) -> None:
        self._patch_rns(monkeypatch)
        self._fake_serial_and_pubsub(monkeypatch)
        stop_event = Mock(spec=threading.Event)
        stop_event.wait.side_effect = [False, False, True]
        monkeypatch.setattr(module.time, "monotonic", lambda: 0.0)

        module.run(
            serial_path="/dev/fake",
            meshtastic_channel=1,
            reticulum_config=str(tmp_path),
            allowlist=empty_policy[0],
            recipients=empty_policy[1],
            announce_interval=60,
            tick_interval=1,
            persist_interval=300,
            dedup_snapshot_path=None,
            region_duty_cycle_percent=100,
            enable_transmit=False,
            transmit_rate=None,
            transmit_burst=1,
            stop_event=stop_event,
        )

        assert stop_event.wait.call_count == 3

    def test_run_refuses_to_enable_transmit_against_a_mismatched_device(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        empty_policy: tuple[SenderAllowlist, RecipientList],
    ) -> None:
        self._patch_rns(monkeypatch)
        connection = self._fake_serial_and_pubsub(monkeypatch)
        connection.localNode.localConfig.lora.region = 2  # Not US.
        connection.localNode.localConfig.lora.modem_preset = 4
        stop_event = Mock(spec=threading.Event)

        with pytest.raises(DeviceConfigMismatch):
            module.run(
                serial_path="/dev/fake",
                meshtastic_channel=1,
                reticulum_config=str(tmp_path),
                allowlist=empty_policy[0],
                recipients=empty_policy[1],
                announce_interval=60,
                tick_interval=1,
                persist_interval=300,
                dedup_snapshot_path=None,
                region_duty_cycle_percent=100,
                enable_transmit=True,
                transmit_rate=0.05,
                transmit_burst=1,
                stop_event=stop_event,
            )
        connection.close.assert_called_once()

    def test_run_enables_transmit_against_a_matching_device(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        empty_policy: tuple[SenderAllowlist, RecipientList],
    ) -> None:
        self._patch_rns(monkeypatch)
        connection = self._fake_serial_and_pubsub(monkeypatch)
        connection.localNode.localConfig.lora.region = EXPECTED_REGION_CODE
        connection.localNode.localConfig.lora.modem_preset = EXPECTED_PRESET_CODE
        stop_event = Mock(spec=threading.Event)
        stop_event.wait.return_value = True

        module.run(
            serial_path="/dev/fake",
            meshtastic_channel=1,
            reticulum_config=str(tmp_path),
            allowlist=empty_policy[0],
            recipients=empty_policy[1],
            announce_interval=60,
            tick_interval=1,
            persist_interval=300,
            dedup_snapshot_path=None,
            region_duty_cycle_percent=100,
            enable_transmit=True,
            transmit_rate=0.05,
            transmit_burst=1,
            stop_event=stop_event,
        )

        connection.close.assert_called_once()

    def test_run_requires_an_identity_with_a_hash(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        empty_policy: tuple[SenderAllowlist, RecipientList],
    ) -> None:
        monkeypatch.setattr(module.RNS, "Reticulum", lambda _config: None)
        hashless_identity = cast(RNS.Identity, type("I", (), {"hash": None})())
        monkeypatch.setattr(module, "load_or_create_identity", lambda _config: hashless_identity)

        with pytest.raises(RuntimeError, match="no hash"):
            module.run(
                serial_path="/dev/fake",
                meshtastic_channel=1,
                reticulum_config=str(tmp_path),
                allowlist=empty_policy[0],
                recipients=empty_policy[1],
                announce_interval=60,
                tick_interval=1,
                persist_interval=300,
                dedup_snapshot_path=None,
                region_duty_cycle_percent=100,
                enable_transmit=False,
                transmit_rate=None,
                transmit_burst=1,
                stop_event=Mock(spec=threading.Event),
            )

    def test_run_persists_the_dedup_snapshot_on_schedule(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        empty_policy: tuple[SenderAllowlist, RecipientList],
    ) -> None:
        self._patch_rns(monkeypatch)
        self._fake_serial_and_pubsub(monkeypatch)
        stop_event = Mock(spec=threading.Event)
        stop_event.wait.side_effect = [False, True]
        clock_values = iter([0.0, 1000.0, 1000.0])
        monkeypatch.setattr(module.time, "monotonic", lambda: next(clock_values))
        monkeypatch.setattr(module.time, "time", lambda: 12345.0)
        save_snapshot = Mock()
        monkeypatch.setattr(module, "save_snapshot", save_snapshot)

        snapshot_path = str(tmp_path / "dedup.bin")
        module.run(
            serial_path="/dev/fake",
            meshtastic_channel=1,
            reticulum_config=str(tmp_path),
            allowlist=empty_policy[0],
            recipients=empty_policy[1],
            announce_interval=60,
            tick_interval=1,
            persist_interval=1,
            dedup_snapshot_path=snapshot_path,
            region_duty_cycle_percent=100,
            enable_transmit=False,
            transmit_rate=None,
            transmit_burst=1,
            stop_event=stop_event,
        )

        save_snapshot.assert_called_once()
        assert save_snapshot.call_args.args[0] == snapshot_path


def test_main_reports_a_device_config_mismatch_as_a_clean_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    allowlist_path = tmp_path / "allowlist.txt"
    allowlist_path.write_text("", encoding="utf-8")
    recipients_path = tmp_path / "recipients.txt"
    recipients_path.write_text("", encoding="utf-8")

    def fake_run(**_kwargs: object) -> None:
        raise DeviceConfigMismatch("device reports the wrong region/preset")

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bridge",
            "--serial", "/dev/fake",
            "--channel", "1",
            "--allowlist-file", str(allowlist_path),
            "--recipients-file", str(recipients_path),
        ],
    )

    assert module.main() == 1


def test_main_loads_allowlist_and_recipients_and_calls_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    allowlist_path = tmp_path / "allowlist.txt"
    allowlist_path.write_text(f"{('a' * 32)}\n", encoding="utf-8")
    recipients_path = tmp_path / "recipients.txt"
    recipients_path.write_text(f"{('b' * 32)}\n", encoding="utf-8")

    received: dict[str, object] = {}

    def fake_run(**kwargs: object) -> None:
        received.update(kwargs)

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bridge",
            "--serial", "/dev/fake",
            "--channel", "3",
            "--allowlist-file", str(allowlist_path),
            "--recipients-file", str(recipients_path),
        ],
    )

    exit_code = module.main()

    assert exit_code == 0
    assert received["meshtastic_channel"] == 3
    assert len(cast(SenderAllowlist, received["allowlist"])) == 1
    assert len(cast(RecipientList, received["recipients"])) == 1
