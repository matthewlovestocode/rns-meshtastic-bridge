"""Atomic file persistence is tested entirely against a scratch directory."""

from pathlib import Path

import pytest

from rns_meshtastic_bridge.dedup_persistence import (
    CorruptSnapshot,
    MAX_RECORDS,
    load_snapshot,
    save_snapshot,
)
from rns_meshtastic_bridge.deduplication import DuplicateCacheSnapshot


def test_load_missing_file_returns_an_empty_snapshot(tmp_path: Path) -> None:
    snapshot = load_snapshot(str(tmp_path / "missing.bin"))
    assert snapshot.entries == ()


def test_save_then_load_round_trips_entries(tmp_path: Path) -> None:
    path = str(tmp_path / "dedup.bin")
    original = DuplicateCacheSnapshot(
        entries=((b"a" * 16, 100.5), (b"b" * 16, 200.25))
    )

    save_snapshot(path, original)
    loaded = load_snapshot(path)

    assert loaded == original


def test_save_overwrites_an_existing_file_atomically(tmp_path: Path) -> None:
    path = str(tmp_path / "dedup.bin")
    save_snapshot(path, DuplicateCacheSnapshot(entries=((b"a" * 16, 1.0),)))
    save_snapshot(path, DuplicateCacheSnapshot(entries=((b"b" * 16, 2.0),)))

    assert load_snapshot(path).entries == ((b"b" * 16, 2.0),)


def test_save_does_not_leave_a_temp_file_behind(tmp_path: Path) -> None:
    path = str(tmp_path / "dedup.bin")
    save_snapshot(path, DuplicateCacheSnapshot(entries=()))

    assert set(p.name for p in tmp_path.iterdir()) == {"dedup.bin"}


def test_save_rejects_a_snapshot_over_the_record_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rns_meshtastic_bridge.dedup_persistence as module

    monkeypatch.setattr(module, "MAX_RECORDS", 1)
    path = str(tmp_path / "dedup.bin")
    oversized = DuplicateCacheSnapshot(
        entries=((b"a" * 16, 0.0), (b"b" * 16, 0.0))
    )
    with pytest.raises(ValueError, match="exceeds the maximum"):
        save_snapshot(path, oversized)


def test_max_records_default_is_generous(tmp_path: Path) -> None:
    assert MAX_RECORDS >= 4096  # At least the project's default cache capacity.


def test_save_rejects_a_malformed_message_id(tmp_path: Path) -> None:
    path = str(tmp_path / "dedup.bin")
    with pytest.raises(ValueError, match="16 bytes"):
        save_snapshot(path, DuplicateCacheSnapshot(entries=((b"short", 0.0),)))


def test_load_rejects_a_truncated_file(tmp_path: Path) -> None:
    path = tmp_path / "dedup.bin"
    path.write_bytes(b"\x00" * 7)  # Not a multiple of the 24-byte record size.

    with pytest.raises(CorruptSnapshot, match="not a multiple"):
        load_snapshot(str(path))


def test_load_rejects_a_file_over_the_record_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rns_meshtastic_bridge.dedup_persistence as module

    monkeypatch.setattr(module, "MAX_RECORDS", 1)
    path = tmp_path / "dedup.bin"
    save_snapshot(str(path), DuplicateCacheSnapshot(entries=()))
    # Bypass save_snapshot's own guard to write two records directly.
    with open(path, "wb") as handle:
        handle.write(module._RECORD.pack(b"a" * 16, 1.0))
        handle.write(module._RECORD.pack(b"b" * 16, 2.0))

    with pytest.raises(CorruptSnapshot, match="exceeding"):
        load_snapshot(str(path))
