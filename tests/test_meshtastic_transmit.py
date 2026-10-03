"""The transmit adapter is tested entirely against a mocked FrameTransmitter.

No test here, and nothing in ``rns_meshtastic_bridge.meshtastic_transmit`` itself, ever
touches a serial device or the real Meshtastic client. These tests exist to
prove the adapter stays inert unless explicitly enabled, and that it
correctly fragments, rate-limits, and labels frames with port 76 and the
configured channel once it is.
"""

from unittest.mock import Mock

import pytest

from rns_meshtastic_bridge.airtime import BoundedFrameQueue, TokenBucketLimiter
from rns_meshtastic_bridge.envelope import BridgeEnvelope
from rns_meshtastic_bridge.fragments import fragment_message
from rns_meshtastic_bridge.meshtastic_adapter import RETICULUM_TUNNEL_PORT, TransmissionDisabled
from rns_meshtastic_bridge.meshtastic_transmit import MeshtasticTransmitAdapter


MESSAGE_ID = b"m" * 16
ORIGIN_ID = b"o" * 16


def envelope(payload: bytes = b"hello") -> BridgeEnvelope:
    return BridgeEnvelope(message_id=MESSAGE_ID, origin_id=ORIGIN_ID, payload=payload)


def limiter(*, rate: float = 100, burst: int = 100, now: float = 0) -> TokenBucketLimiter:
    return TokenBucketLimiter(rate_per_second=rate, burst=burst, now=now)


class TestDisabledByDefault:
    def test_adapter_defaults_to_disabled(self) -> None:
        adapter = MeshtasticTransmitAdapter(channel_index=1, limiter=limiter())
        assert adapter.enabled is False

    def test_enqueue_raises_when_disabled(self) -> None:
        adapter = MeshtasticTransmitAdapter(channel_index=1, limiter=limiter())
        with pytest.raises(TransmissionDisabled, match="disabled"):
            adapter.enqueue(envelope())
        assert adapter.queued_frames == 0

    def test_send_ready_raises_when_disabled(self) -> None:
        adapter = MeshtasticTransmitAdapter(channel_index=1, limiter=limiter())
        transmitter = Mock()
        with pytest.raises(TransmissionDisabled, match="disabled"):
            adapter.send_ready(now=0, transmitter=transmitter)
        transmitter.send_frame.assert_not_called()

    def test_there_is_no_way_to_enable_after_construction(self) -> None:
        adapter = MeshtasticTransmitAdapter(channel_index=1, limiter=limiter())
        assert not hasattr(adapter, "enable")
        assert not hasattr(adapter, "set_enabled")
        with pytest.raises(AttributeError):
            adapter.enabled = True  # type: ignore[misc]


class TestEnabledOperation:
    def test_enqueue_fragments_and_admits_frames(self) -> None:
        adapter = MeshtasticTransmitAdapter(
            channel_index=1, limiter=limiter(), enabled=True
        )
        message = envelope(b"x" * 500)
        expected_frames = len(fragment_message(message.message_id, message.encode()))

        assert adapter.enqueue(message) is True
        assert adapter.queued_frames == expected_frames

    def test_enqueue_respects_the_bounded_queue(self) -> None:
        tiny_queue = BoundedFrameQueue(max_frames=1, max_bytes=10_000)
        adapter = MeshtasticTransmitAdapter(
            channel_index=1, limiter=limiter(), queue=tiny_queue, enabled=True
        )
        # One envelope fragments to more than one frame at the default size.
        assert adapter.enqueue(envelope(b"x" * 500)) is False
        assert adapter.queued_frames == 0

    def test_send_ready_transmits_on_port_76_and_the_configured_channel(self) -> None:
        transmitter = Mock()
        adapter = MeshtasticTransmitAdapter(
            channel_index=3, limiter=limiter(), enabled=True
        )
        adapter.enqueue(envelope(b"small"))

        sent = adapter.send_ready(now=0, transmitter=transmitter)

        assert sent == 1
        transmitter.send_frame.assert_called_once()
        _, kwargs = transmitter.send_frame.call_args
        assert kwargs["channel_index"] == 3
        assert kwargs["port"] == RETICULUM_TUNNEL_PORT

    def test_send_ready_sends_every_frame_of_a_fragmented_envelope_in_order(self) -> None:
        transmitter = Mock()
        adapter = MeshtasticTransmitAdapter(
            channel_index=1, limiter=limiter(), enabled=True
        )
        message = envelope(b"x" * 500)
        expected_frames = fragment_message(message.message_id, message.encode())
        adapter.enqueue(message)

        sent = adapter.send_ready(now=0, transmitter=transmitter)

        assert sent == len(expected_frames)
        sent_frames = [call.args[0] for call in transmitter.send_frame.call_args_list]
        assert sent_frames == expected_frames

    def test_send_ready_is_gated_by_the_rate_limiter(self) -> None:
        transmitter = Mock()
        adapter = MeshtasticTransmitAdapter(
            channel_index=1,
            limiter=limiter(rate=1, burst=1),
            enabled=True,
        )
        adapter.enqueue(envelope(b"a"))
        adapter.enqueue(envelope(b"b"))

        first_pass = adapter.send_ready(now=0, transmitter=transmitter)
        assert first_pass == 1
        assert adapter.queued_frames == 1

        second_pass = adapter.send_ready(now=1, transmitter=transmitter)
        assert second_pass == 1
        assert adapter.queued_frames == 0

    def test_send_ready_returns_zero_when_queue_is_empty(self) -> None:
        adapter = MeshtasticTransmitAdapter(
            channel_index=1, limiter=limiter(), enabled=True
        )
        assert adapter.send_ready(now=0, transmitter=Mock()) == 0


@pytest.mark.parametrize("channel", [-1, 8])
def test_channel_index_must_name_a_real_slot(channel: int) -> None:
    with pytest.raises(ValueError, match="between 0 and 7"):
        MeshtasticTransmitAdapter(channel_index=channel, limiter=limiter())
