"""Routing tests demonstrate the seam future network mocks will call."""

import pytest

from rns_meshtastic_bridge.engine import BridgeAction, BridgeEngine, Ingress
from rns_meshtastic_bridge.envelope import BridgeEnvelope


LOCAL_ID = b"l" * 16
REMOTE_ID = b"r" * 16
MESSAGE_ID = b"m" * 16


def remote_wire(*, hops: int = 0, max_hops: int = 4) -> bytes:
    return BridgeEnvelope(
        message_id=MESSAGE_ID,
        origin_id=REMOTE_ID,
        payload=b"payload",
        hops=hops,
        max_hops=max_hops,
    ).encode()


def test_originated_message_uses_injected_id_factory() -> None:
    engine = BridgeEngine(LOCAL_ID, id_factory=lambda length: b"i" * length)
    envelope = engine.originate(b"hello", now=1)

    assert envelope.message_id == b"i" * 16
    assert envelope.origin_id == LOCAL_ID
    assert envelope.payload == b"hello"

    reflected = engine.inspect(
        envelope.encode(), ingress=Ingress.MESHTASTIC, now=2
    )
    assert reflected.action is BridgeAction.DROP_REFLECTION


@pytest.mark.parametrize("bridge_id", [b"short", b"x" * 17])
def test_engine_requires_sixteen_byte_bridge_id(bridge_id: bytes) -> None:
    with pytest.raises(ValueError, match="bridge_id"):
        BridgeEngine(bridge_id)


@pytest.mark.parametrize("max_hops", [0, 256])
def test_engine_validates_default_hop_limit(max_hops: int) -> None:
    with pytest.raises(ValueError, match="max_hops"):
        BridgeEngine(LOCAL_ID, max_hops=max_hops)


def test_engine_validates_generated_message_id() -> None:
    engine = BridgeEngine(LOCAL_ID, id_factory=lambda _: b"bad")
    with pytest.raises(ValueError, match="id_factory"):
        engine.originate(b"hello", now=0)


@pytest.mark.parametrize(
    ("ingress", "egress"),
    [
        (Ingress.RETICULUM, Ingress.MESHTASTIC),
        (Ingress.MESHTASTIC, Ingress.RETICULUM),
    ],
)
def test_valid_message_advances_to_opposite_network(
    ingress: Ingress, egress: Ingress
) -> None:
    decision = BridgeEngine(LOCAL_ID).inspect(
        remote_wire(), ingress=ingress, now=0
    )

    assert decision.action is BridgeAction.FORWARD
    assert decision.egress is egress
    forwarded = BridgeEnvelope.decode(decision.wire or b"")
    assert forwarded.hops == 1
    assert forwarded.payload == b"payload"


def test_repeat_is_dropped() -> None:
    engine = BridgeEngine(LOCAL_ID)
    assert engine.inspect(
        remote_wire(), ingress=Ingress.RETICULUM, now=0
    ).action is BridgeAction.FORWARD
    assert engine.inspect(
        remote_wire(), ingress=Ingress.RETICULUM, now=1
    ).action is BridgeAction.DROP_DUPLICATE


def test_exhausted_hop_limit_is_dropped() -> None:
    decision = BridgeEngine(LOCAL_ID).inspect(
        remote_wire(hops=4), ingress=Ingress.RETICULUM, now=0
    )
    assert decision.action is BridgeAction.DROP_HOP_LIMIT
    assert decision.egress is None
    assert decision.wire is None


def test_duplicate_suppression_survives_a_snapshot_and_restore_round_trip() -> None:
    before = BridgeEngine(LOCAL_ID)
    assert before.inspect(
        remote_wire(), ingress=Ingress.RETICULUM, now=0
    ).action is BridgeAction.FORWARD
    snapshot = before.snapshot_duplicates(now=1, wall_clock_now=1001)

    # A fresh engine stands in for the process that starts after a restart.
    after = BridgeEngine(LOCAL_ID)
    after.restore_duplicates(snapshot, now=0, wall_clock_now=1001)

    assert after.inspect(
        remote_wire(), ingress=Ingress.RETICULUM, now=0
    ).action is BridgeAction.DROP_DUPLICATE
