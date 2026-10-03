"""Atomic, bounded on-disk persistence for a duplicate-cache snapshot.

This exists so a future live bridge process can survive a restart without
either replaying stale traffic (an empty cache after every restart) or
growing a state file without bound. It is deliberately independent of
Reticulum, Meshtastic, and any live service: nothing here is wired into a
running process yet, matching the project's staged rollout. A later milestone
calls ``save_snapshot``/``load_snapshot`` around whatever process eventually
hosts a live ``BridgeEngine``.

The file format is a flat sequence of fixed-size records (16-byte message ID,
8-byte big-endian double wall-clock deadline). Capacity is bounded by
``DuplicateCache`` itself before a snapshot is ever taken, so a well-formed
file is already small; ``MAX_RECORDS`` additionally refuses to load a file
that has grown implausibly large, rather than trusting its size blindly.
"""

from __future__ import annotations

import os
import struct
import tempfile

from rns_meshtastic_bridge.deduplication import DuplicateCacheSnapshot
from rns_meshtastic_bridge.envelope import ID_LENGTH


_RECORD = struct.Struct(f">{ID_LENGTH}sd")

# Comfortably above any configured DuplicateCache capacity in this project;
# guards against loading a truncated/corrupted or tampered-with file as if it
# were an enormous legitimate snapshot.
MAX_RECORDS = 1_000_000


class CorruptSnapshot(RuntimeError):
    """The on-disk snapshot file is not a whole number of valid records."""


def save_snapshot(path: str, snapshot: DuplicateCacheSnapshot) -> None:
    """Atomically replace the file at ``path`` with ``snapshot``'s contents.

    Writing to a temporary file in the same directory and then renaming it
    into place means a reader never observes a partially written file, and a
    crash mid-write leaves the previous snapshot (or no file) intact instead
    of a corrupt one.
    """
    if len(snapshot.entries) > MAX_RECORDS:
        raise ValueError("snapshot exceeds the maximum persisted record count")

    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".dedup-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            for message_id, deadline in snapshot.entries:
                if len(message_id) != ID_LENGTH:
                    raise ValueError(f"message_id must be exactly {ID_LENGTH} bytes")
                handle.write(_RECORD.pack(message_id, deadline))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
        raise


def load_snapshot(path: str) -> DuplicateCacheSnapshot:
    """Read a snapshot written by ``save_snapshot``.

    A missing file is treated as an empty snapshot: the normal case for a
    bridge's very first start, not an error. A present-but-corrupt file
    raises instead of silently returning partial data, so a caller can choose
    whether to start with an empty cache and log a warning.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        return DuplicateCacheSnapshot(entries=())

    if len(data) % _RECORD.size != 0:
        raise CorruptSnapshot(
            f"snapshot file size {len(data)} is not a multiple of the "
            f"{_RECORD.size}-byte record size"
        )
    record_count = len(data) // _RECORD.size
    if record_count > MAX_RECORDS:
        raise CorruptSnapshot(
            f"snapshot contains {record_count} records, exceeding the "
            f"{MAX_RECORDS} safety limit"
        )

    entries = tuple(
        _RECORD.unpack_from(data, offset * _RECORD.size)
        for offset in range(record_count)
    )
    return DuplicateCacheSnapshot(entries=entries)
