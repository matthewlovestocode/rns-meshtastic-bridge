"""Lifecycle tests prove the monitor never invokes a transmit operation."""

import logging
from pathlib import Path
import threading
from unittest.mock import Mock

import pytest

from rns_meshtastic_bridge.envelope import BridgeEnvelope
from rns_meshtastic_bridge.fragments import fragment_message
from rns_meshtastic_bridge.local_sink import SinkDeliveryError
from rns_meshtastic_bridge.meshtastic_adapter import (
    MeshtasticReceiveAdapter,
    PacketDisposition,
    RETICULUM_TUNNEL_NAME,
)
from rns_meshtastic_bridge.receive_service import (
    RECEIVE_TOPIC,
    ReceiveMetrics,
    ReceiveMonitor,
    _parser,
    run_monitor,
)


def callback_packet(payload: bytes, *, channel: int = 2) -> dict[str, object]:
    return {
        "channel": channel,
        "decoded": {
            "portnum": RETICULUM_TUNNEL_NAME,
            "payload": payload,
        },
    }


def complete_frame() -> bytes:
    envelope = BridgeEnvelope(
        message_id=b"m" * 16,
        origin_id=b"o" * 16,
        payload=b"secret application payload",
    )
    return fragment_message(envelope.message_id, envelope.encode())[0]


def test_metrics_snapshot_has_stable_zero_fields() -> None:
    metrics = ReceiveMetrics()
    metrics.record(PacketDisposition.COMPLETE)
    snapshot = metrics.snapshot()

    assert snapshot["complete"] == 1
    assert snapshot["malformed"] == 0
    assert set(snapshot) == {disposition.value for disposition in PacketDisposition}


def test_monitor_logs_metadata_but_not_payload(caplog: pytest.LogCaptureFixture) -> None:
    monitor = ReceiveMonitor(
        MeshtasticReceiveAdapter(channel_index=2), clock=lambda: 10
    )
    with caplog.at_level(logging.INFO):
        result = monitor.receive_callback(
            callback_packet(complete_frame()), interface=Mock(), topic="ignored"
        )
        monitor.log_summary()

    assert result.disposition is PacketDisposition.COMPLETE
    assert "6d" * 16 in caplog.text
    assert "payload_bytes=26" in caplog.text
    assert "secret application payload" not in caplog.text
    assert "complete=1" in caplog.text


def test_monitor_forwards_complete_envelope_to_sink_instead_of_discarding(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = Mock()
    monitor = ReceiveMonitor(
        MeshtasticReceiveAdapter(channel_index=2), clock=lambda: 10, sink=sink
    )
    with caplog.at_level(logging.INFO):
        result = monitor.receive_callback(callback_packet(complete_frame()))

    assert result.disposition is PacketDisposition.COMPLETE
    sink.send.assert_called_once_with(result.envelope.encode())  # type: ignore[union-attr]
    assert "delivered" in caplog.text
    assert "to local sink" in caplog.text
    assert "secret application payload" not in caplog.text


def test_monitor_logs_sink_failure_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = Mock()
    sink.send.side_effect = SinkDeliveryError("broken pipe")
    monitor = ReceiveMonitor(MeshtasticReceiveAdapter(channel_index=2), sink=sink)

    with caplog.at_level(logging.WARNING):
        result = monitor.receive_callback(callback_packet(complete_frame()))

    assert result.disposition is PacketDisposition.COMPLETE
    assert "sink delivery failed" in caplog.text
    assert "broken pipe" in caplog.text


def test_monitor_logs_structural_malformed_error(caplog: pytest.LogCaptureFixture) -> None:
    monitor = ReceiveMonitor(MeshtasticReceiveAdapter(channel_index=2))
    with caplog.at_level(logging.WARNING):
        result = monitor.receive_callback(callback_packet(b"short"))

    assert result.disposition is PacketDisposition.MALFORMED
    assert "shorter than its header" in caplog.text


def test_run_monitor_subscribes_reports_and_cleans_up(tmp_path: Path) -> None:
    device = tmp_path / "ttyACM0"
    device.touch()
    pubsub = Mock()
    connection = Mock()
    factory = Mock(return_value=connection)
    monitor = Mock(spec=ReceiveMonitor)
    monitor.receive_callback = Mock()

    # First wait interval reports metrics; the second requests shutdown.
    stop_event = Mock(spec=threading.Event)
    stop_event.wait.side_effect = [False, True]
    run_monitor(
        monitor,
        serial_path=str(device),
        report_interval=5,
        stop_event=stop_event,
        interface_factory=factory,
        pubsub=pubsub,
    )

    pubsub.subscribe.assert_called_once_with(monitor.receive_callback, RECEIVE_TOPIC)
    factory.assert_called_once_with(str(device))
    monitor.log_summary.assert_called_once_with()
    pubsub.unsubscribe.assert_called_once_with(monitor.receive_callback, RECEIVE_TOPIC)
    connection.close.assert_called_once_with()
    assert not hasattr(connection, "sendData") or not connection.sendData.called


def test_run_monitor_cleans_subscription_when_open_fails(tmp_path: Path) -> None:
    device = tmp_path / "ttyACM0"
    device.touch()
    pubsub = Mock()
    monitor = Mock(spec=ReceiveMonitor)
    error = OSError("serial busy")

    with pytest.raises(OSError, match="serial busy"):
        run_monitor(
            monitor,
            serial_path=str(device),
            report_interval=5,
            stop_event=threading.Event(),
            interface_factory=Mock(side_effect=error),
            pubsub=pubsub,
        )
    pubsub.unsubscribe.assert_called_once()


def test_run_monitor_validates_inputs(tmp_path: Path) -> None:
    common = {
        "monitor": Mock(spec=ReceiveMonitor),
        "stop_event": threading.Event(),
        "interface_factory": Mock(),
        "pubsub": Mock(),
    }
    with pytest.raises(ValueError, match="positive"):
        run_monitor(serial_path=str(tmp_path), report_interval=0, **common)
    with pytest.raises(FileNotFoundError, match="not found"):
        run_monitor(
            serial_path=str(tmp_path / "missing"), report_interval=1, **common
        )


def test_cli_requires_explicit_channel() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args([])
    args = _parser().parse_args(
        ["--serial", "/dev/fake", "--channel", "2", "--report-interval", "10"]
    )
    assert args.channel == 2
    assert args.report_interval == 10


def test_cli_sink_path_defaults_to_disabled() -> None:
    args = _parser().parse_args(["--serial", "/dev/fake", "--channel", "2"])
    assert args.sink_path is None


def test_cli_accepts_an_explicit_sink_path() -> None:
    args = _parser().parse_args(
        [
            "--serial",
            "/dev/fake",
            "--channel",
            "2",
            "--sink-path",
            "/tmp/sink.sock",
        ]
    )
    assert args.sink_path == "/tmp/sink.sock"
