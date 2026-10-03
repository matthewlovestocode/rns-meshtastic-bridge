"""Local sink framing is tested over a real Unix domain socket pair.

``socket.socketpair`` gives two connected, in-process Unix domain sockets, so
these tests exercise the exact bytes ``UnixSocketSink`` would put on the wire
to a separate local consumer process without needing to spawn one.
"""

from pathlib import Path
import socket
import tempfile

import pytest

from rns_meshtastic_bridge.envelope import MAX_PAYLOAD_BYTES
from rns_meshtastic_bridge.local_sink import (
    InMemorySink,
    MAX_FRAME_BYTES,
    SinkDeliveryError,
    UnixSocketSink,
    read_frame,
)


def test_unix_socket_sink_round_trips_a_frame() -> None:
    writer_sock, reader_sock = socket.socketpair()
    try:
        sink = UnixSocketSink(writer_sock)
        sink.send(b"hello envelope bytes")

        assert read_frame(reader_sock) == b"hello envelope bytes"
    finally:
        writer_sock.close()
        reader_sock.close()


def test_unix_socket_sink_round_trips_multiple_frames_in_order() -> None:
    writer_sock, reader_sock = socket.socketpair()
    try:
        sink = UnixSocketSink(writer_sock)
        sink.send(b"first")
        sink.send(b"second")

        assert read_frame(reader_sock) == b"first"
        assert read_frame(reader_sock) == b"second"
    finally:
        writer_sock.close()
        reader_sock.close()


def test_read_frame_returns_none_on_clean_shutdown() -> None:
    writer_sock, reader_sock = socket.socketpair()
    try:
        writer_sock.close()
        assert read_frame(reader_sock) is None
    finally:
        reader_sock.close()


def test_read_frame_raises_when_closed_mid_header() -> None:
    writer_sock, reader_sock = socket.socketpair()
    try:
        writer_sock.sendall(b"\x00\x00")  # Half of a 4-byte length prefix.
        writer_sock.close()
        with pytest.raises(SinkDeliveryError, match="closed mid-frame"):
            read_frame(reader_sock)
    finally:
        reader_sock.close()


def test_read_frame_raises_when_closed_mid_body() -> None:
    writer_sock, reader_sock = socket.socketpair()
    try:
        writer_sock.sendall((5).to_bytes(4, "big") + b"ab")
        writer_sock.close()
        with pytest.raises(SinkDeliveryError, match="closed mid-frame"):
            read_frame(reader_sock)
    finally:
        reader_sock.close()


def test_read_frame_raises_when_closed_before_any_body_byte() -> None:
    writer_sock, reader_sock = socket.socketpair()
    try:
        writer_sock.sendall((5).to_bytes(4, "big"))
        writer_sock.close()
        with pytest.raises(SinkDeliveryError, match="closed mid-frame"):
            read_frame(reader_sock)
    finally:
        reader_sock.close()


def test_read_frame_handles_a_zero_length_frame() -> None:
    writer_sock, reader_sock = socket.socketpair()
    try:
        sink = UnixSocketSink(writer_sock)
        sink.send(b"")
        assert read_frame(reader_sock) == b""
    finally:
        writer_sock.close()
        reader_sock.close()


def test_read_frame_rejects_an_oversized_declared_length() -> None:
    writer_sock, reader_sock = socket.socketpair()
    try:
        writer_sock.sendall((MAX_FRAME_BYTES + 1).to_bytes(4, "big"))
        with pytest.raises(SinkDeliveryError, match="larger than the safety limit"):
            read_frame(reader_sock)
    finally:
        writer_sock.close()
        reader_sock.close()


def test_sink_send_rejects_oversized_payload_before_writing() -> None:
    writer_sock, reader_sock = socket.socketpair()
    try:
        sink = UnixSocketSink(writer_sock)
        with pytest.raises(SinkDeliveryError, match="exceeds the maximum"):
            sink.send(b"x" * (MAX_FRAME_BYTES + 1))
    finally:
        writer_sock.close()
        reader_sock.close()


def test_sink_send_wraps_os_errors() -> None:
    writer_sock, reader_sock = socket.socketpair()
    sink = UnixSocketSink(writer_sock)
    writer_sock.close()
    reader_sock.close()

    with pytest.raises(SinkDeliveryError):
        sink.send(b"data after close")


def test_connect_dials_a_unix_domain_socket() -> None:
    # AF_UNIX paths are limited to roughly 104 bytes on macOS/BSD, well below
    # what pytest's per-test tmp_path can produce, so this uses a short path
    # directly under /tmp instead.
    with tempfile.TemporaryDirectory(dir="/tmp") as short_dir:
        socket_path = Path(short_dir) / "s"
        _dial_through_unix_socket(socket_path)


def _dial_through_unix_socket(socket_path: Path) -> None:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    try:
        sink = UnixSocketSink.connect(str(socket_path))
        try:
            server_conn, _ = listener.accept()
            try:
                sink.send(b"payload")
                assert read_frame(server_conn) == b"payload"
            finally:
                server_conn.close()
        finally:
            sink.close()
    finally:
        listener.close()


class TestInMemorySink:
    def test_drain_returns_frames_in_order_and_clears_them(self) -> None:
        sink = InMemorySink()
        sink.send(b"a")
        sink.send(b"b")

        assert sink.drain() == [b"a", b"b"]
        assert sink.drain() == []

    def test_capacity_bounds_memory_by_dropping_the_oldest(self) -> None:
        sink = InMemorySink(capacity=2)
        sink.send(b"a")
        sink.send(b"b")
        sink.send(b"c")

        assert sink.drain() == [b"b", b"c"]

    def test_rejects_oversized_payload(self) -> None:
        sink = InMemorySink()
        with pytest.raises(SinkDeliveryError):
            sink.send(b"x" * (MAX_FRAME_BYTES + 1))

    @pytest.mark.parametrize("capacity", [0, -1])
    def test_capacity_must_be_positive(self, capacity: int) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            InMemorySink(capacity=capacity)


def test_max_frame_bytes_covers_the_largest_possible_envelope() -> None:
    assert MAX_FRAME_BYTES > MAX_PAYLOAD_BYTES
