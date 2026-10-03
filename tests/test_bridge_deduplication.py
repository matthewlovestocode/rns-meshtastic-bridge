"""Duplicate suppression tests use explicit timestamps instead of sleeping."""

import pytest

from rns_meshtastic_bridge.deduplication import DuplicateCache, DuplicateCacheSnapshot


@pytest.mark.parametrize(
    "arguments",
    [{"ttl_seconds": 0}, {"capacity": 0}],
)
def test_cache_requires_positive_limits(arguments: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="positive"):
        DuplicateCache(**arguments)


def test_duplicate_is_seen_until_ttl_expires() -> None:
    cache = DuplicateCache(ttl_seconds=10)

    assert cache.seen_or_add(b"one", now=1) is False
    assert cache.seen_or_add(b"one", now=5) is True
    assert cache.seen_or_add(b"one", now=11) is False


def test_capacity_evicts_least_recently_used_id() -> None:
    cache = DuplicateCache(capacity=2)
    cache.seen_or_add(b"one", now=0)
    cache.seen_or_add(b"two", now=0)
    assert cache.seen_or_add(b"one", now=1) is True

    cache.seen_or_add(b"three", now=1)
    assert cache.seen_or_add(b"two", now=2) is False


def test_snapshot_and_restore_preserve_duplicate_suppression_across_a_restart() -> None:
    before = DuplicateCache(ttl_seconds=10)
    before.seen_or_add(b"one", now=0)  # Expires at monotonic 10 / wall 1010.

    snapshot = before.snapshot(now=5, wall_clock_now=1005)
    # The restarted process's monotonic clock resumes at zero, but five
    # seconds of its ten-second TTL were already used up before the restart.
    after = DuplicateCache.restore(snapshot, now=0, wall_clock_now=1005, ttl_seconds=10)

    assert after.seen_or_add(b"one", now=4) is True
    assert after.seen_or_add(b"one", now=6) is False


def test_restore_drops_entries_that_expired_during_downtime() -> None:
    before = DuplicateCache(ttl_seconds=10)
    before.seen_or_add(b"one", now=0)
    snapshot = before.snapshot(now=1, wall_clock_now=1001)

    # The process was down for an hour: the entry's wall-clock deadline has
    # long since passed, so it must not be restored as still-live.
    after = DuplicateCache.restore(snapshot, now=0, wall_clock_now=1001 + 3600)

    assert after.seen_or_add(b"one", now=0) is False


def test_restore_honors_capacity_by_keeping_the_longest_lived_entries() -> None:
    snapshot = DuplicateCacheSnapshot(
        entries=((b"a" * 16, 100.0), (b"b" * 16, 300.0), (b"c" * 16, 200.0))
    )
    restored = DuplicateCache.restore(
        snapshot, now=0, wall_clock_now=0, capacity=2
    )

    # "a" expires soonest, so restore's capacity trim evicts it, keeping only
    # the two longer-lived entries. Querying "a" here would itself insert it
    # (seen_or_add always adds a miss), so only the two survivors are checked.
    assert restored.seen_or_add(b"b" * 16, now=0) is True
    assert restored.seen_or_add(b"c" * 16, now=0) is True


def test_restore_with_an_empty_snapshot_behaves_like_a_fresh_cache() -> None:
    restored = DuplicateCache.restore(
        DuplicateCacheSnapshot(entries=()), now=0, wall_clock_now=0
    )
    assert restored.seen_or_add(b"one", now=0) is False


def test_snapshot_excludes_already_expired_entries() -> None:
    cache = DuplicateCache(ttl_seconds=5)
    cache.seen_or_add(b"one", now=0)

    snapshot = cache.snapshot(now=10, wall_clock_now=1010)

    assert snapshot.entries == ()
