"""Rate limiting and queue admission are tested with synthetic clocks only.

Nothing here touches Meshtastic or a real radio; ``rns_meshtastic_bridge.airtime`` is a pure
timing/admission policy with no transmit path behind it yet.
"""

import pytest

from rns_meshtastic_bridge.airtime import BoundedFrameQueue, TokenBucketLimiter, next_ready_frame


ENVELOPE_ID = b"e" * 16


class TestTokenBucketLimiter:
    @pytest.mark.parametrize("kwargs", [{"rate_per_second": 0}, {"burst": 0}])
    def test_rejects_invalid_configuration(self, kwargs: dict[str, float]) -> None:
        defaults = {"rate_per_second": 1.0, "burst": 1}
        with pytest.raises(ValueError):
            TokenBucketLimiter(**{**defaults, **kwargs}, now=0)

    def test_burst_allows_immediate_acquisitions_up_to_capacity(self) -> None:
        limiter = TokenBucketLimiter(rate_per_second=1, burst=3, now=0)

        assert limiter.try_acquire(now=0) is True
        assert limiter.try_acquire(now=0) is True
        assert limiter.try_acquire(now=0) is True
        assert limiter.try_acquire(now=0) is False

    def test_tokens_refill_at_the_configured_rate(self) -> None:
        limiter = TokenBucketLimiter(rate_per_second=2, burst=1, now=0)
        assert limiter.try_acquire(now=0) is True
        assert limiter.try_acquire(now=0.1) is False

        # Two tokens per second means 0.5 seconds refills exactly one token.
        assert limiter.try_acquire(now=0.5) is True

    def test_refill_never_exceeds_burst_capacity(self) -> None:
        limiter = TokenBucketLimiter(rate_per_second=100, burst=2, now=0)
        limiter.try_acquire(now=1000)  # Idle long enough to overflow if unbounded.
        assert limiter.available_tokens == 1.0

    def test_time_may_not_move_backwards(self) -> None:
        limiter = TokenBucketLimiter(rate_per_second=1, burst=1, now=10)
        with pytest.raises(ValueError, match="backwards"):
            limiter.try_acquire(now=9)


class TestBoundedFrameQueue:
    def test_offer_rejects_an_empty_frame_list(self) -> None:
        queue = BoundedFrameQueue(max_frames=10, max_bytes=1000)
        with pytest.raises(ValueError, match="must not be empty"):
            queue.offer_envelope(ENVELOPE_ID, [])

    @pytest.mark.parametrize("kwargs", [{"max_frames": 0}, {"max_bytes": 0}])
    def test_rejects_invalid_configuration(self, kwargs: dict[str, int]) -> None:
        defaults = {"max_frames": 1, "max_bytes": 1}
        with pytest.raises(ValueError):
            BoundedFrameQueue(**{**defaults, **kwargs})

    def test_frames_are_popped_in_fifo_order(self) -> None:
        queue = BoundedFrameQueue(max_frames=10, max_bytes=1000)
        queue.offer_envelope(ENVELOPE_ID, [b"a", b"b"])

        first = queue.pop()
        second = queue.pop()
        assert first is not None and first.frame == b"a"
        assert second is not None and second.frame == b"b"
        assert queue.pop() is None

    def test_frame_and_byte_counts_track_the_queue(self) -> None:
        queue = BoundedFrameQueue(max_frames=10, max_bytes=1000)
        queue.offer_envelope(ENVELOPE_ID, [b"ab", b"cde"])

        assert queue.frame_count == 2
        assert queue.byte_count == 5

        queue.pop()
        assert queue.frame_count == 1
        assert queue.byte_count == 3

    def test_offer_is_all_or_nothing_against_the_frame_count_limit(self) -> None:
        queue = BoundedFrameQueue(max_frames=2, max_bytes=1000)
        assert queue.offer_envelope(ENVELOPE_ID, [b"a", b"b", b"c"]) is False
        assert queue.frame_count == 0
        assert queue.byte_count == 0

    def test_offer_is_all_or_nothing_against_the_byte_limit(self) -> None:
        queue = BoundedFrameQueue(max_frames=10, max_bytes=4)
        assert queue.offer_envelope(ENVELOPE_ID, [b"ab", b"cde"]) is False
        assert queue.frame_count == 0

    def test_offer_rejects_a_single_envelope_that_can_never_fit(self) -> None:
        # Even against an empty queue, one envelope cannot exceed capacity.
        queue = BoundedFrameQueue(max_frames=1, max_bytes=1000)
        assert queue.offer_envelope(ENVELOPE_ID, [b"a", b"b"]) is False

    def test_offer_succeeding_leaves_room_tracked_correctly(self) -> None:
        queue = BoundedFrameQueue(max_frames=3, max_bytes=100)
        assert queue.offer_envelope(ENVELOPE_ID, [b"a", b"b"]) is True
        assert queue.offer_envelope(b"other-envelope!!", [b"c"]) is True
        assert queue.offer_envelope(b"third-envelope!!", [b"d"]) is False

    def test_peek_does_not_remove_the_frame(self) -> None:
        queue = BoundedFrameQueue(max_frames=10, max_bytes=1000)
        queue.offer_envelope(ENVELOPE_ID, [b"a"])

        assert queue.peek() is not None
        assert queue.frame_count == 1

    def test_pop_on_empty_queue_returns_none(self) -> None:
        queue = BoundedFrameQueue(max_frames=10, max_bytes=1000)
        assert queue.pop() is None
        assert queue.peek() is None


class TestNextReadyFrame:
    def test_returns_none_when_queue_is_empty(self) -> None:
        queue = BoundedFrameQueue(max_frames=10, max_bytes=1000)
        limiter = TokenBucketLimiter(rate_per_second=1, burst=5, now=0)

        assert next_ready_frame(queue, limiter, now=0) is None

    def test_pops_a_frame_when_the_limiter_permits_it(self) -> None:
        queue = BoundedFrameQueue(max_frames=10, max_bytes=1000)
        queue.offer_envelope(ENVELOPE_ID, [b"frame-one"])
        limiter = TokenBucketLimiter(rate_per_second=1, burst=1, now=0)

        result = next_ready_frame(queue, limiter, now=0)

        assert result is not None
        assert result.frame == b"frame-one"
        assert result.envelope_id == ENVELOPE_ID
        assert queue.frame_count == 0

    def test_leaves_the_frame_queued_when_the_limiter_denies_it(self) -> None:
        queue = BoundedFrameQueue(max_frames=10, max_bytes=1000)
        queue.offer_envelope(ENVELOPE_ID, [b"frame-one"])
        limiter = TokenBucketLimiter(rate_per_second=1, burst=1, now=0)
        limiter.try_acquire(now=0)  # Exhaust the only token.

        assert next_ready_frame(queue, limiter, now=0) is None
        assert queue.frame_count == 1

    def test_a_denied_frame_can_be_sent_once_tokens_refill(self) -> None:
        queue = BoundedFrameQueue(max_frames=10, max_bytes=1000)
        queue.offer_envelope(ENVELOPE_ID, [b"frame-one"])
        limiter = TokenBucketLimiter(rate_per_second=1, burst=1, now=0)
        limiter.try_acquire(now=0)

        assert next_ready_frame(queue, limiter, now=0.5) is None
        result = next_ready_frame(queue, limiter, now=1.0)
        assert result is not None
        assert result.frame == b"frame-one"
