"""Fragment tests cover disorder, loss, corruption, expiry, and bounds."""

import pytest

from rns_meshtastic_bridge.errors import MalformedFragment, ReassemblyCapacityExceeded
from rns_meshtastic_bridge.fragments import FragmentReassembler, fragment_message


MESSAGE_ID = b"f" * 16


def test_multi_fragment_message_reassembles_out_of_order() -> None:
    message = bytes(range(256)) * 3
    frames = fragment_message(MESSAGE_ID, message, max_frame_bytes=80)
    reassembler = FragmentReassembler()

    result = None
    for frame in reversed(frames):
        result = reassembler.add(frame, now=1)

    assert len(frames) > 1
    assert all(len(frame) <= 80 for frame in frames)
    assert result == message


def test_empty_message_is_one_valid_fragment() -> None:
    frames = fragment_message(MESSAGE_ID, b"")
    assert FragmentReassembler(max_message_bytes=0).add(frames[0], now=0) == b""


def test_identical_duplicate_fragment_is_ignored() -> None:
    first, second = fragment_message(MESSAGE_ID, b"long enough" * 30, max_frame_bytes=100)[:2]
    reassembler = FragmentReassembler()

    assert reassembler.add(first, now=0) is None
    assert reassembler.add(first, now=1) is None
    assert reassembler.add(second, now=1) is None


def test_expiry_frees_inflight_capacity() -> None:
    first_a = fragment_message(b"a" * 16, b"a" * 500)[0]
    first_b = fragment_message(b"b" * 16, b"b" * 500)[0]
    reassembler = FragmentReassembler(ttl_seconds=5, max_inflight=1)

    reassembler.add(first_a, now=0)
    with pytest.raises(ReassemblyCapacityExceeded, match="in flight"):
        reassembler.add(first_b, now=4)
    assert reassembler.expire(now=5) == 1
    assert reassembler.add(first_b, now=5) is None
    assert reassembler.expire(now=5) == 0


@pytest.mark.parametrize(
    "arguments",
    [
        {"ttl_seconds": 0},
        {"max_inflight": 0},
        {"max_fragments": 0},
        {"max_message_bytes": -1},
    ],
)
def test_reassembler_rejects_invalid_limits(arguments: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        FragmentReassembler(**arguments)


def test_fragmenter_validates_id_and_frame_size() -> None:
    with pytest.raises(MalformedFragment, match="message_id"):
        fragment_message(b"short", b"message")
    with pytest.raises(MalformedFragment, match="too small"):
        fragment_message(MESSAGE_ID, b"message", max_frame_bytes=10)


def test_fragment_count_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    # Avoid allocating gigabytes: replace ceil to exercise the defensive branch.
    monkeypatch.setattr("rns_meshtastic_bridge.fragments.math.ceil", lambda _: 65536)
    with pytest.raises(MalformedFragment, match="65535"):
        fragment_message(MESSAGE_ID, b"message")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda frame: frame[:5], "shorter"),
        (lambda frame: b"XX" + frame[2:], "magic"),
        (lambda frame: frame[:2] + b"\x02" + frame[3:], "version"),
        (lambda frame: frame[:19] + b"\x00\x02" + frame[21:], "index"),
    ],
)
def test_invalid_fragment_header_is_rejected(mutation: object, message: str) -> None:
    frame = fragment_message(MESSAGE_ID, b"hello")[0]
    with pytest.raises(MalformedFragment, match=message):
        FragmentReassembler().add(mutation(frame), now=0)  # type: ignore[operator]


def test_empty_chunk_is_invalid_in_multi_fragment_sequence() -> None:
    frame = fragment_message(MESSAGE_ID, b"x" * 500)[0][:27]
    with pytest.raises(MalformedFragment, match="empty"):
        FragmentReassembler().add(frame, now=0)


def test_conflicting_sequence_metadata_is_rejected() -> None:
    first = fragment_message(MESSAGE_ID, b"a" * 500)[0]
    conflicting = fragment_message(MESSAGE_ID, b"b" * 800)[0]
    reassembler = FragmentReassembler()
    reassembler.add(first, now=0)

    with pytest.raises(MalformedFragment, match="metadata"):
        reassembler.add(conflicting, now=0)


def test_conflicting_duplicate_chunk_is_rejected() -> None:
    first = bytearray(fragment_message(MESSAGE_ID, b"a" * 500)[0])
    reassembler = FragmentReassembler()
    reassembler.add(bytes(first), now=0)
    first[-1] ^= 1

    with pytest.raises(MalformedFragment, match="different bytes"):
        reassembler.add(bytes(first), now=0)


def test_fragment_and_message_limits_are_enforced() -> None:
    frames = fragment_message(MESSAGE_ID, b"x" * 500)
    with pytest.raises(ReassemblyCapacityExceeded, match="fragment count"):
        FragmentReassembler(max_fragments=1).add(frames[0], now=0)
    with pytest.raises(ReassemblyCapacityExceeded, match="byte limit"):
        FragmentReassembler(max_message_bytes=1).add(frames[0], now=0)


def test_checksum_detects_corruption_after_reassembly() -> None:
    frames = fragment_message(MESSAGE_ID, b"x" * 500)
    frames[-1] = frames[-1][:-1] + bytes([frames[-1][-1] ^ 1])
    reassembler = FragmentReassembler()

    for frame in frames[:-1]:
        assert reassembler.add(frame, now=0) is None
    with pytest.raises(MalformedFragment, match="checksum"):
        reassembler.add(frames[-1], now=0)
