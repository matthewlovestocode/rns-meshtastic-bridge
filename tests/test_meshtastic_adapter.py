"""The Meshtastic boundary is tested entirely with callback-shaped mocks."""

from unittest.mock import Mock

import pytest

from rns_meshtastic_bridge.envelope import BridgeEnvelope
from rns_meshtastic_bridge.fragments import fragment_message
from rns_meshtastic_bridge.meshtastic_adapter import (
    MeshtasticReceiveAdapter,
    PacketDisposition,
    RETICULUM_TUNNEL_NAME,
    RETICULUM_TUNNEL_PORT,
    TransmissionDisabled,
)


MESSAGE_ID = b"m" * 16
ORIGIN_ID = b"o" * 16


def envelope(payload: bytes = b"hello") -> BridgeEnvelope:
    return BridgeEnvelope(
        message_id=MESSAGE_ID,
        origin_id=ORIGIN_ID,
        payload=payload,
    )


def packet(
    payload: object,
    *,
    port: object = RETICULUM_TUNNEL_NAME,
    channel: object = 2,
) -> dict[str, object]:
    return {
        "channel": channel,
        "decoded": {"portnum": port, "payload": payload},
    }


@pytest.mark.parametrize("channel", [-1, 8])
def test_channel_index_must_name_a_real_slot(channel: int) -> None:
    with pytest.raises(ValueError, match="between 0 and 7"):
        MeshtasticReceiveAdapter(channel_index=channel)


@pytest.mark.parametrize("port", [1, "TEXT_MESSAGE_APP", None])
def test_unrelated_application_ports_are_ignored(port: object) -> None:
    result = MeshtasticReceiveAdapter(channel_index=2).receive(
        packet(b"not a bridge frame", port=port), now=0
    )
    assert result.disposition is PacketDisposition.IGNORED_PORT
    assert result.error is None


def test_numeric_reticulum_port_is_accepted() -> None:
    frame = fragment_message(MESSAGE_ID, envelope().encode())[0]
    result = MeshtasticReceiveAdapter(channel_index=2).receive(
        packet(frame, port=RETICULUM_TUNNEL_PORT), now=0
    )
    assert result.disposition is PacketDisposition.COMPLETE


def test_wrong_channel_is_ignored_before_payload_parsing() -> None:
    result = MeshtasticReceiveAdapter(channel_index=2).receive(
        packet("not bytes", channel=1), now=0
    )
    assert result.disposition is PacketDisposition.IGNORED_CHANNEL


def test_missing_channel_means_primary_channel_zero() -> None:
    frame = fragment_message(MESSAGE_ID, envelope().encode())[0]
    callback = packet(frame, channel=0)
    del callback["channel"]

    result = MeshtasticReceiveAdapter(channel_index=0).receive(callback, now=0)
    assert result.disposition is PacketDisposition.COMPLETE


@pytest.mark.parametrize(
    ("callback", "error"),
    [
        (None, "packet must be a mapping"),
        ({}, "packet.decoded"),
        ({"decoded": []}, "packet.decoded"),
        (packet(b"frame", channel="2"), "packet.channel"),
        (packet("base64 is not accepted"), "payload must contain bytes"),
    ],
)
def test_malformed_callback_shapes_are_reported(
    callback: object, error: str
) -> None:
    result = MeshtasticReceiveAdapter(channel_index=2).receive(callback, now=0)
    assert result.disposition is PacketDisposition.MALFORMED
    assert error in (result.error or "")


def test_bad_fragment_is_reported_without_raising() -> None:
    result = MeshtasticReceiveAdapter(channel_index=2).receive(
        packet(b"bad frame"), now=0
    )
    assert result.disposition is PacketDisposition.MALFORMED
    assert "shorter than its header" in (result.error or "")


def test_fragmented_envelope_completes_out_of_order() -> None:
    original = envelope(b"large payload" * 100)
    frames = fragment_message(MESSAGE_ID, original.encode())
    adapter = MeshtasticReceiveAdapter(channel_index=2)

    for frame in reversed(frames[1:]):
        assert adapter.receive(packet(frame), now=1).disposition is PacketDisposition.PARTIAL
    result = adapter.receive(packet(frames[0]), now=1)

    assert result.disposition is PacketDisposition.COMPLETE
    assert result.envelope == original


def test_reassembled_non_envelope_is_malformed() -> None:
    frame = fragment_message(MESSAGE_ID, b"not an envelope")[0]
    result = MeshtasticReceiveAdapter(channel_index=2).receive(packet(frame), now=0)
    assert result.disposition is PacketDisposition.MALFORMED
    assert "shorter than its header" in (result.error or "")


def test_frames_for_round_trip_through_another_adapter() -> None:
    original = envelope(b"x" * 500)
    sender = MeshtasticReceiveAdapter(channel_index=2)
    receiver = MeshtasticReceiveAdapter(channel_index=2)

    result = None
    for frame in sender.frames_for(original):
        result = receiver.receive(packet(bytearray(frame)), now=0)

    assert result is not None
    assert result.disposition is PacketDisposition.COMPLETE
    assert result.envelope == original


def test_send_is_enforced_receive_only() -> None:
    adapter = MeshtasticReceiveAdapter(channel_index=2)
    fake_client = Mock()

    with pytest.raises(TransmissionDisabled, match="transmission is disabled"):
        adapter.send(envelope())
    fake_client.sendData.assert_not_called()
