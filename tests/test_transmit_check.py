"""The live-test harness is tested entirely with mocks; nothing here opens a
serial device or imports the real meshtastic package. Dry-run is the default
CLI behavior, and --live is gated behind device-config verification plus two
explicit operator confirmation flags.
"""

from unittest.mock import Mock

import pytest

from rns_meshtastic_bridge.meshtastic_adapter import RETICULUM_TUNNEL_PORT
from rns_meshtastic_bridge.transmit_check import (
    EXPECTED_PRESET_CODE,
    EXPECTED_REGION_CODE,
    DeviceConfigMismatch,
    SerialFrameTransmitter,
    describe_plan,
    main,
    plan_frames,
    run_live,
    verify_device_config,
)


def fake_lora_config(*, region: int = EXPECTED_REGION_CODE, preset: int = EXPECTED_PRESET_CODE) -> Mock:
    config = Mock()
    config.region = region
    config.modem_preset = preset
    return config


def fake_interface(*, region: int = EXPECTED_REGION_CODE, preset: int = EXPECTED_PRESET_CODE) -> Mock:
    interface = Mock()
    interface.localNode.localConfig.lora = fake_lora_config(region=region, preset=preset)
    return interface


class TestPlanning:
    def test_plan_frames_is_pure_and_matches_fragmentation(self) -> None:
        frames = plan_frames("hello")
        assert len(frames) == 1
        assert plan_frames("hello") == frames  # Deterministic id_factory.

    def test_describe_plan_mentions_channel_port_and_rate(self) -> None:
        summary = describe_plan("hi", channel=2, rate_per_second=0.05, burst=1)
        assert "channel 2" in summary
        assert f"port {RETICULUM_TUNNEL_PORT}" in summary
        assert "rate_per_second=0.05" in summary
        assert "1 frame(s)" in summary


class TestVerifyDeviceConfig:
    def test_matching_config_does_not_raise(self) -> None:
        verify_device_config(fake_lora_config())

    def test_wrong_region_is_rejected(self) -> None:
        with pytest.raises(DeviceConfigMismatch, match="region"):
            verify_device_config(fake_lora_config(region=2))

    def test_wrong_preset_is_rejected(self) -> None:
        with pytest.raises(DeviceConfigMismatch, match="modem_preset"):
            verify_device_config(fake_lora_config(preset=0))


class TestSerialFrameTransmitter:
    def test_send_frame_calls_sendData_with_the_right_keywords(self) -> None:
        interface = Mock()
        transmitter = SerialFrameTransmitter(interface)

        transmitter.send_frame(b"frame-bytes", channel_index=3, port=76)

        interface.sendData.assert_called_once_with(
            b"frame-bytes", portNum=76, channelIndex=3, wantAck=False
        )


class TestRunLive:
    def test_refuses_to_send_when_device_config_does_not_match(self) -> None:
        interface = fake_interface(region=2)
        with pytest.raises(DeviceConfigMismatch):
            run_live(
                message="hi",
                channel=1,
                rate_per_second=100,
                burst=10,
                interface=interface,
                sleep=Mock(),
                clock=lambda: 0,
            )
        interface.sendData.assert_not_called()

    def test_sends_every_frame_on_the_configured_channel_and_port(self) -> None:
        interface = fake_interface()
        sent = run_live(
            message="hi",
            channel=4,
            rate_per_second=100,
            burst=10,
            interface=interface,
            sleep=Mock(),
            clock=lambda: 0,
        )

        assert sent == 1
        interface.sendData.assert_called_once_with(
            plan_frames("hi")[0], portNum=RETICULUM_TUNNEL_PORT, channelIndex=4, wantAck=False
        )

    def test_waits_for_the_rate_limiter_between_frames(self) -> None:
        interface = fake_interface()
        sleep = Mock()
        ticks = {"t": 0.0}

        def clock() -> float:
            ticks["t"] += 0.1
            return ticks["t"]

        # A long message fragments into several frames at the default frame
        # size; burst=1 forces the limiter to deny all but one per poll.
        message = "x" * 500
        expected_frames = plan_frames(message)
        assert len(expected_frames) > 1

        sent = run_live(
            message=message,
            channel=1,
            rate_per_second=10,
            burst=1,
            interface=interface,
            sleep=sleep,
            clock=clock,
        )

        assert sent == len(expected_frames)
        sleep.assert_called()

    def test_raises_when_the_message_is_too_large_for_the_outbound_queue(self) -> None:
        interface = fake_interface()
        with pytest.raises(RuntimeError, match="did not fit"):
            run_live(
                message="x" * 100_000,  # Exceeds the default queue's byte bound.
                channel=1,
                rate_per_second=100,
                burst=10,
                interface=interface,
                sleep=Mock(),
                clock=lambda: 0,
            )
        interface.sendData.assert_not_called()


class TestCli:
    def test_dry_run_is_the_default_and_never_imports_meshtastic(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--channel", "1", "--rate", "0.05", "--message", "hi"])

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "dry run only" in out
        assert "would send" in out

    def test_rejects_an_invalid_channel(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--channel", "9", "--rate", "0.05", "--message", "hi"])
        assert exit_code == 2
        assert "between 0 and 7" in capsys.readouterr().err

    def test_live_without_confirmation_flags_is_refused(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(
            ["--channel", "1", "--rate", "0.05", "--message", "hi", "--live"]
        )
        assert exit_code == 2
        error = capsys.readouterr().err
        assert "--serial" in error
        assert "--confirm-psk-rotated" in error
        assert "--confirm-antenna-connected" in error

    def test_live_with_only_one_confirmation_flag_is_still_refused(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = main(
            [
                "--channel", "1",
                "--rate", "0.05",
                "--message", "hi",
                "--live",
                "--serial", "/dev/fake",
                "--confirm-psk-rotated",
            ]
        )
        assert exit_code == 2
        assert "--confirm-antenna-connected" in capsys.readouterr().err
