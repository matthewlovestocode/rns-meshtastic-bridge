"""Wire-envelope tests use only bytes, so no network mocking is needed."""

import pytest

from rns_meshtastic_bridge.envelope import BridgeEnvelope, MAX_PAYLOAD_BYTES
from rns_meshtastic_bridge.errors import MalformedEnvelope


MESSAGE_ID = b"m" * 16
ORIGIN_ID = b"o" * 16


def envelope(**changes: object) -> BridgeEnvelope:
    """Construct a valid baseline while allowing one field to be varied."""
    values: dict[str, object] = {
        "message_id": MESSAGE_ID,
        "origin_id": ORIGIN_ID,
        "payload": b"hello",
        "hops": 1,
        "max_hops": 4,
    }
    values.update(changes)
    return BridgeEnvelope(**values)  # type: ignore[arg-type]


def test_envelope_round_trip_and_advance() -> None:
    original = envelope(payload=b"\x00binary\xff")
    decoded = BridgeEnvelope.decode(original.encode())

    assert decoded == original
    assert decoded.advanced().hops == 2
    assert decoded.advanced().payload == original.payload


@pytest.mark.parametrize("field", ["message_id", "origin_id"])
def test_ids_must_be_exactly_sixteen_bytes(field: str) -> None:
    with pytest.raises(MalformedEnvelope, match=field):
        envelope(**{field: b"short"})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"max_hops": 0}, "max_hops"),
        ({"max_hops": 256}, "max_hops"),
        ({"hops": -1}, "hops"),
        ({"hops": 5, "max_hops": 4}, "hops"),
        ({"payload": b"x" * (MAX_PAYLOAD_BYTES + 1)}, "payload"),
    ],
)
def test_constructor_rejects_invalid_limits(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(MalformedEnvelope, match=message):
        envelope(**changes)


def test_exhausted_envelope_cannot_advance() -> None:
    with pytest.raises(MalformedEnvelope, match="exhausted"):
        envelope(hops=4).advanced()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda wire: wire[:10], "shorter"),
        (lambda wire: b"XX" + wire[2:], "magic"),
        (lambda wire: wire[:2] + b"\x02" + wire[3:], "version"),
        (lambda wire: wire[:3] + b"\x01" + wire[4:], "flags"),
        (lambda wire: wire[:-1], "length"),
        (lambda wire: wire + b"x", "length"),
    ],
)
def test_decode_rejects_malformed_wire(mutation: object, message: str) -> None:
    wire = envelope().encode()
    with pytest.raises(MalformedEnvelope, match=message):
        BridgeEnvelope.decode(mutation(wire))  # type: ignore[operator]


def test_decode_rejects_oversized_declared_payload() -> None:
    wire = bytearray(envelope().encode())
    wire[38:42] = (MAX_PAYLOAD_BYTES + 1).to_bytes(4, "big")
    with pytest.raises(MalformedEnvelope, match="declared payload"):
        BridgeEnvelope.decode(bytes(wire))
