"""The Reticulum egress/ingress boundary is tested entirely with mocks.

No test here opens a live Reticulum instance. ``ReticulumEgressAdapter`` is
exercised with a fake packet factory that returns bare receipt doubles, which
is enough to prove packet ownership, delivery-failure handling, and
backpressure without any network stack.
"""

from unittest.mock import Mock, patch

import pytest

from rns_meshtastic_bridge.envelope import BridgeEnvelope
from rns_meshtastic_bridge.reticulum_adapter import (
    DeliveryDisposition,
    IngressDisposition,
    ReticulumEgressAdapter,
    ReticulumIngressAdapter,
    SendDisposition,
)


MESSAGE_ID = b"m" * 16
ORIGIN_ID = b"o" * 16


def envelope(payload: bytes = b"hello") -> BridgeEnvelope:
    return BridgeEnvelope(message_id=MESSAGE_ID, origin_id=ORIGIN_ID, payload=payload)


def fake_receipt() -> Mock:
    """A receipt double exposing only the two callback setters we use."""
    return Mock(spec=["set_delivery_callback", "set_timeout_callback"])


class TestPacketOwnership:
    def test_default_factory_builds_a_real_rns_packet(self) -> None:
        import RNS

        with patch.object(RNS, "Packet", Mock(return_value=Mock(send=Mock(return_value=False)))) as packet_cls:
            adapter = ReticulumEgressAdapter()
            message = envelope()
            adapter.offer(destination="dest", envelope=message)

            result = adapter.send_next()

        packet_cls.assert_called_once_with("dest", message.encode())
        assert result is not None
        assert result.disposition is SendDisposition.REJECTED

    def test_offer_queues_without_touching_reticulum(self) -> None:
        factory = Mock()
        adapter = ReticulumEgressAdapter(packet_factory=factory)

        assert adapter.offer(destination="dest", envelope=envelope()) is True
        assert adapter.pending == 1
        factory.assert_not_called()

    def test_send_next_builds_exactly_one_packet_with_encoded_envelope(self) -> None:
        packet = Mock()
        packet.send.return_value = fake_receipt()
        factory = Mock(return_value=packet)
        adapter = ReticulumEgressAdapter(packet_factory=factory)
        message = envelope()
        adapter.offer(destination="dest", envelope=message)

        result = adapter.send_next()

        factory.assert_called_once_with("dest", message.encode())
        packet.send.assert_called_once_with()
        assert result is not None
        assert result.disposition is SendDisposition.IN_FLIGHT
        assert result.envelope == message

    def test_send_next_returns_none_when_queue_is_empty(self) -> None:
        adapter = ReticulumEgressAdapter(packet_factory=Mock())
        assert adapter.send_next() is None

    def test_queue_is_first_in_first_out(self) -> None:
        packet = Mock()
        packet.send.return_value = fake_receipt()
        adapter = ReticulumEgressAdapter(packet_factory=Mock(return_value=packet))
        first, second = envelope(b"first"), envelope(b"second")
        adapter.offer(destination="dest", envelope=first)
        adapter.offer(destination="dest", envelope=second)

        assert adapter.send_next().envelope == first  # type: ignore[union-attr]
        assert adapter.send_next().envelope == second  # type: ignore[union-attr]


class TestDeliveryFailures:
    def test_send_returning_false_is_rejected_with_no_interface(self) -> None:
        packet = Mock()
        packet.send.return_value = False
        adapter = ReticulumEgressAdapter(packet_factory=Mock(return_value=packet))
        adapter.offer(destination="dest", envelope=envelope())

        result = adapter.send_next()

        assert result is not None
        assert result.disposition is SendDisposition.REJECTED
        assert adapter.in_flight == 0

    def test_send_returning_none_is_untracked(self) -> None:
        packet = Mock()
        packet.send.return_value = None
        adapter = ReticulumEgressAdapter(packet_factory=Mock(return_value=packet))
        adapter.offer(destination="dest", envelope=envelope())

        result = adapter.send_next()

        assert result is not None
        assert result.disposition is SendDisposition.UNTRACKED
        assert adapter.in_flight == 0

    def test_timeout_callback_reports_failure_and_clears_in_flight(self) -> None:
        receipt = fake_receipt()
        packet = Mock()
        packet.send.return_value = receipt
        delivered = Mock()
        failed = Mock()
        adapter = ReticulumEgressAdapter(
            packet_factory=Mock(return_value=packet),
            on_delivered=delivered,
            on_failed=failed,
        )
        message = envelope()
        adapter.offer(destination="dest", envelope=message)
        adapter.send_next()
        assert adapter.in_flight == 1

        timeout_callback = receipt.set_timeout_callback.call_args.args[0]
        timeout_callback(receipt)

        failed.assert_called_once_with(message)
        delivered.assert_not_called()
        assert adapter.in_flight == 0

    def test_delivery_callback_reports_success(self) -> None:
        receipt = fake_receipt()
        packet = Mock()
        packet.send.return_value = receipt
        delivered = Mock()
        adapter = ReticulumEgressAdapter(
            packet_factory=Mock(return_value=packet), on_delivered=delivered
        )
        message = envelope()
        adapter.offer(destination="dest", envelope=message)
        adapter.send_next()

        delivery_callback = receipt.set_delivery_callback.call_args.args[0]
        delivery_callback(receipt)

        delivered.assert_called_once_with(message)
        assert adapter.in_flight == 0

    def test_late_duplicate_callback_is_ignored(self) -> None:
        receipt = fake_receipt()
        packet = Mock()
        packet.send.return_value = receipt
        failed = Mock()
        delivered = Mock()
        adapter = ReticulumEgressAdapter(
            packet_factory=Mock(return_value=packet),
            on_delivered=delivered,
            on_failed=failed,
        )
        adapter.offer(destination="dest", envelope=envelope())
        adapter.send_next()
        timeout_callback = receipt.set_timeout_callback.call_args.args[0]
        delivery_callback = receipt.set_delivery_callback.call_args.args[0]

        timeout_callback(receipt)
        delivery_callback(receipt)  # A late, redundant Reticulum callback.

        failed.assert_called_once()
        delivered.assert_not_called()

    def test_callbacks_are_optional(self) -> None:
        receipt = fake_receipt()
        packet = Mock()
        packet.send.return_value = receipt
        adapter = ReticulumEgressAdapter(packet_factory=Mock(return_value=packet))
        adapter.offer(destination="dest", envelope=envelope())
        adapter.send_next()

        timeout_callback = receipt.set_timeout_callback.call_args.args[0]
        timeout_callback(receipt)  # Must not raise without on_failed set.


class TestBackpressure:
    def test_offer_rejects_once_capacity_is_reached(self) -> None:
        adapter = ReticulumEgressAdapter(capacity=2, packet_factory=Mock())

        assert adapter.offer(destination="dest", envelope=envelope(b"a")) is True
        assert adapter.offer(destination="dest", envelope=envelope(b"b")) is True
        assert adapter.offer(destination="dest", envelope=envelope(b"c")) is False
        assert adapter.pending == 2

    def test_draining_the_queue_makes_room_for_more_offers(self) -> None:
        packet = Mock()
        packet.send.return_value = fake_receipt()
        adapter = ReticulumEgressAdapter(
            capacity=1, packet_factory=Mock(return_value=packet)
        )

        assert adapter.offer(destination="dest", envelope=envelope(b"a")) is True
        assert adapter.offer(destination="dest", envelope=envelope(b"b")) is False
        adapter.send_next()
        assert adapter.offer(destination="dest", envelope=envelope(b"b")) is True

    @pytest.mark.parametrize("capacity", [0, -1])
    def test_capacity_must_be_positive(self, capacity: int) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            ReticulumEgressAdapter(capacity=capacity)


class TestIngress:
    def test_valid_envelope_is_decoded(self) -> None:
        message = envelope(b"payload")
        result = ReticulumIngressAdapter().receive(message.encode())

        assert result.disposition is IngressDisposition.COMPLETE
        assert result.envelope == message
        assert result.error is None

    def test_malformed_bytes_are_reported_without_raising(self) -> None:
        result = ReticulumIngressAdapter().receive(b"not an envelope")

        assert result.disposition is IngressDisposition.MALFORMED
        assert result.envelope is None
        assert "shorter than its header" in (result.error or "")


def test_delivery_disposition_values_stay_descriptive() -> None:
    assert {d.value for d in DeliveryDisposition} == {"delivered", "failed"}
