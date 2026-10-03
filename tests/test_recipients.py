"""Recipient fan-out is tested with monkeypatched RNS, the same style
``tests/test_reticulum_destination.py`` uses — RNS is a normal dependency
everywhere, not edge-only hardware.
"""

from unittest.mock import Mock, call

import pytest
import RNS

from rns_meshtastic_bridge import recipients as module
from rns_meshtastic_bridge.engine import BridgeAction, BridgeEngine, Ingress
from rns_meshtastic_bridge.envelope import BridgeEnvelope
from rns_meshtastic_bridge.recipients import (
    RecipientList,
    RecipientResolver,
    fanout_forward,
    make_meshtastic_on_forward,
)


LOCAL_ID = b"l" * 16
REMOTE_ID = b"r" * 16
MESSAGE_ID = b"m" * 16
RECIPIENT_A = b"a" * 16
RECIPIENT_B = b"b" * 16


def remote_envelope(*, hops: int = 0, payload: bytes = b"payload") -> BridgeEnvelope:
    return BridgeEnvelope(
        message_id=MESSAGE_ID, origin_id=REMOTE_ID, payload=payload, hops=hops
    )


class TestRecipientList:
    def test_rejects_a_wrong_length_hash(self) -> None:
        with pytest.raises(ValueError, match="exactly 16 bytes"):
            RecipientList([b"short"])

    def test_deduplicates(self) -> None:
        assert len(RecipientList([RECIPIENT_A, RECIPIENT_A])) == 1

    def test_from_hex_lines_ignores_blanks_and_comments(self) -> None:
        text = f"""
        # trusted recipients
        {RECIPIENT_A.hex()}

        {RECIPIENT_B.hex()}
        """
        recipients = RecipientList.from_hex_lines(text)
        assert set(recipients) == {RECIPIENT_A, RECIPIENT_B}


class TestRecipientResolver:
    def test_resolves_immediately_when_a_path_is_already_known(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_identity = object()
        fake_destination = object()
        request_path = Mock()
        monkeypatch.setattr(module.RNS.Transport, "has_path", lambda _h: True)
        monkeypatch.setattr(module.RNS.Transport, "request_path", request_path)
        monkeypatch.setattr(module.RNS.Identity, "recall", lambda _h: fake_identity)
        monkeypatch.setattr(module.RNS, "Destination", Mock(return_value=fake_destination))

        resolver = RecipientResolver(RecipientList([RECIPIENT_A]))
        resolver.refresh()

        request_path.assert_not_called()
        assert resolver.resolved_count == 1
        assert resolver.destinations() == [fake_destination]

    def test_requests_a_path_and_waits_when_none_is_known_yet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path_known = {"value": False}
        monkeypatch.setattr(module.RNS.Transport, "has_path", lambda _h: path_known["value"])
        request_path = Mock(side_effect=lambda _h: path_known.update(value=True))
        monkeypatch.setattr(module.RNS.Transport, "request_path", request_path)
        monkeypatch.setattr(module.RNS.Identity, "recall", lambda _h: object())
        monkeypatch.setattr(module.RNS, "Destination", Mock(return_value=object()))

        resolver = RecipientResolver(
            RecipientList([RECIPIENT_A]),
            path_timeout=5,
            sleep=Mock(),
            clock=lambda: 0,
        )
        resolver.refresh()

        request_path.assert_called_once_with(RECIPIENT_A)
        assert resolver.resolved_count == 1

    def test_gives_up_after_the_path_timeout_and_can_retry_later(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(module.RNS.Transport, "has_path", lambda _h: False)
        monkeypatch.setattr(module.RNS.Transport, "request_path", Mock())
        clock_values = iter([0.0, 0.0, 6.0])  # deadline calc, loop-check(ok), loop-check(expired)

        resolver = RecipientResolver(
            RecipientList([RECIPIENT_A]),
            path_timeout=5,
            sleep=Mock(),
            clock=lambda: next(clock_values),
        )
        resolver.refresh()

        assert resolver.resolved_count == 0
        assert resolver.destinations() == []

    def test_unrecallable_identity_leaves_the_recipient_unresolved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(module.RNS.Transport, "has_path", lambda _h: True)
        monkeypatch.setattr(module.RNS.Identity, "recall", lambda _h: None)

        resolver = RecipientResolver(RecipientList([RECIPIENT_A]))
        resolver.refresh()

        assert resolver.resolved_count == 0

    def test_refresh_does_not_re_resolve_already_resolved_recipients(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        has_path = Mock(return_value=True)
        monkeypatch.setattr(module.RNS.Transport, "has_path", has_path)
        monkeypatch.setattr(module.RNS.Identity, "recall", lambda _h: object())
        monkeypatch.setattr(module.RNS, "Destination", Mock(return_value=object()))

        resolver = RecipientResolver(RecipientList([RECIPIENT_A]))
        resolver.refresh()
        resolver.refresh()

        assert has_path.call_count == 1


class TestFanoutForward:
    def test_queues_one_packet_per_resolved_recipient(self) -> None:
        destination_a, destination_b = object(), object()
        resolver = Mock()
        resolver.destinations.return_value = [destination_a, destination_b]
        egress = Mock()
        egress.offer.return_value = True
        envelope = remote_envelope()

        sent = fanout_forward(envelope.encode(), resolver=resolver, egress=egress)

        assert sent == 2
        egress.offer.assert_has_calls(
            [call(destination_a, envelope), call(destination_b, envelope)],
            any_order=True,
        )

    def test_a_full_queue_for_one_recipient_does_not_stop_the_others(self) -> None:
        destination_a, destination_b = object(), object()
        resolver = Mock()
        resolver.destinations.return_value = [destination_a, destination_b]
        egress = Mock()
        egress.offer.side_effect = [False, True]

        sent = fanout_forward(remote_envelope().encode(), resolver=resolver, egress=egress)

        assert sent == 1
        assert egress.offer.call_count == 2

    def test_no_resolved_recipients_sends_nothing(self) -> None:
        resolver = Mock()
        resolver.destinations.return_value = []
        egress = Mock()

        sent = fanout_forward(remote_envelope().encode(), resolver=resolver, egress=egress)

        assert sent == 0
        egress.offer.assert_not_called()


class TestMakeMeshtasticOnForward:
    def test_forwards_a_valid_envelope_to_the_fanout(self) -> None:
        engine = BridgeEngine(LOCAL_ID)
        resolver = Mock()
        destination = object()
        resolver.destinations.return_value = [destination]
        egress = Mock()
        egress.offer.return_value = True
        on_forward = make_meshtastic_on_forward(
            engine=engine, resolver=resolver, egress=egress, clock=lambda: 0
        )

        on_forward(remote_envelope())

        egress.offer.assert_called_once()

    def test_a_dropped_decision_never_reaches_the_fanout(self) -> None:
        engine = BridgeEngine(LOCAL_ID, id_factory=lambda length: b"i" * length)
        originated = engine.originate(b"hello", now=0)
        resolver = Mock()
        egress = Mock()
        on_forward = make_meshtastic_on_forward(
            engine=engine, resolver=resolver, egress=egress, clock=lambda: 1
        )

        on_forward(originated)  # Reflects back; BridgeEngine drops it.

        egress.offer.assert_not_called()

    def test_forward_action_is_the_real_engine_decision_not_a_stub(self) -> None:
        engine = BridgeEngine(LOCAL_ID)
        resolver = Mock()
        resolver.destinations.return_value = []
        egress = Mock()
        on_forward = make_meshtastic_on_forward(
            engine=engine, resolver=resolver, egress=egress, clock=lambda: 0
        )
        envelope = remote_envelope()

        on_forward(envelope)
        on_forward(envelope)  # Second time is now a duplicate.

        assert resolver.destinations.call_count == 1
