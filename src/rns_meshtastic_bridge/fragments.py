"""Fragment large envelopes into Meshtastic-sized application payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import struct
import zlib

from rns_meshtastic_bridge.envelope import ID_LENGTH
from rns_meshtastic_bridge.errors import MalformedFragment, ReassemblyCapacityExceeded


FRAGMENT_MAGIC = b"RF"
FRAGMENT_VERSION = 1
DEFAULT_FRAME_BYTES = 200

# magic, version, message ID, zero-based index, count, whole-message CRC32
_HEADER = struct.Struct(">2sB16sHHI")


def fragment_message(
    message_id: bytes,
    message: bytes,
    *,
    max_frame_bytes: int = DEFAULT_FRAME_BYTES,
) -> list[bytes]:
    """Split one encoded envelope into independently identifiable frames."""
    if len(message_id) != ID_LENGTH:
        raise MalformedFragment("message_id must be exactly 16 bytes")
    chunk_size = max_frame_bytes - _HEADER.size
    if chunk_size <= 0:
        raise MalformedFragment("frame size is too small for the fragment header")

    count = max(1, math.ceil(len(message) / chunk_size))
    if count > 65535:
        raise MalformedFragment("message requires more than 65535 fragments")
    checksum = zlib.crc32(message)

    return [
        _HEADER.pack(
            FRAGMENT_MAGIC,
            FRAGMENT_VERSION,
            message_id,
            index,
            count,
            checksum,
        )
        + message[index * chunk_size : (index + 1) * chunk_size]
        for index in range(count)
    ]


@dataclass(slots=True)
class _Assembly:
    """Internal bounded state for one partially received message."""

    count: int
    checksum: int
    updated_at: float
    chunks: dict[int, bytes] = field(default_factory=dict)
    byte_count: int = 0


class FragmentReassembler:
    """Reassemble out-of-order frames while bounding time and memory."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 120,
        max_inflight: int = 64,
        max_fragments: int = 4096,
        max_message_bytes: int = 1024 * 1024,
    ) -> None:
        if ttl_seconds <= 0 or max_inflight <= 0:
            raise ValueError("ttl_seconds and max_inflight must be positive")
        if max_fragments <= 0 or max_message_bytes < 0:
            raise ValueError("fragment and byte limits must be non-negative")
        self._ttl = ttl_seconds
        self._max_inflight = max_inflight
        self._max_fragments = max_fragments
        self._max_message_bytes = max_message_bytes
        self._assemblies: dict[bytes, _Assembly] = {}

    def add(self, frame: bytes, *, now: float) -> bytes | None:
        """Accept one frame and return the complete message when available."""
        self.expire(now=now)
        message_id, index, count, checksum, chunk = _decode_frame(frame)
        if count > self._max_fragments:
            raise ReassemblyCapacityExceeded("fragment count exceeds configured limit")

        assembly = self._assemblies.get(message_id)
        if assembly is None:
            if len(self._assemblies) >= self._max_inflight:
                raise ReassemblyCapacityExceeded("too many messages are in flight")
            assembly = _Assembly(count=count, checksum=checksum, updated_at=now)
            self._assemblies[message_id] = assembly
        elif assembly.count != count or assembly.checksum != checksum:
            raise MalformedFragment("fragment metadata conflicts with earlier frames")

        existing = assembly.chunks.get(index)
        if existing is not None:
            if existing != chunk:
                raise MalformedFragment("duplicate fragment contains different bytes")
            assembly.updated_at = now
            return None

        if assembly.byte_count + len(chunk) > self._max_message_bytes:
            del self._assemblies[message_id]
            raise ReassemblyCapacityExceeded("message exceeds configured byte limit")

        assembly.chunks[index] = chunk
        assembly.byte_count += len(chunk)
        assembly.updated_at = now
        if len(assembly.chunks) != assembly.count:
            return None

        message = b"".join(assembly.chunks[position] for position in range(count))
        del self._assemblies[message_id]
        if zlib.crc32(message) != checksum:
            raise MalformedFragment("reassembled message failed its checksum")
        return message

    def expire(self, *, now: float) -> int:
        """Discard stale partial messages and return how many were removed."""
        stale = [
            message_id
            for message_id, assembly in self._assemblies.items()
            if assembly.updated_at + self._ttl <= now
        ]
        for message_id in stale:
            del self._assemblies[message_id]
        return len(stale)


def _decode_frame(frame: bytes) -> tuple[bytes, int, int, int, bytes]:
    """Validate a single untrusted fragment frame."""
    if len(frame) < _HEADER.size:
        raise MalformedFragment("fragment is shorter than its header")
    magic, version, message_id, index, count, checksum = _HEADER.unpack_from(frame)
    if magic != FRAGMENT_MAGIC:
        raise MalformedFragment("unknown fragment magic")
    if version != FRAGMENT_VERSION:
        raise MalformedFragment(f"unsupported fragment version {version}")
    if count == 0 or index >= count:
        raise MalformedFragment("fragment index is outside its declared count")
    chunk = frame[_HEADER.size :]
    if not chunk and not (count == 1 and index == 0):
        raise MalformedFragment("only an empty one-fragment message is valid")
    return message_id, index, count, checksum, chunk
