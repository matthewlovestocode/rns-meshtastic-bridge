"""A local-only sink the receive monitor can hand validated envelopes to.

This is a way for a valid, reassembled bridge envelope to cross the
receive-only Meshtastic process boundary so a separate local process can
consume it, without that envelope ever reaching a Reticulum destination or
the public gateway. Nothing in this
module imports Reticulum, opens a TCP socket, or can become a public listener;
``UnixSocketSink`` only ever talks over a Unix domain socket, which the
operating system confines to the local filesystem.

Wiring a sink into ``rns_meshtastic_bridge.receive_service.ReceiveMonitor`` is intentionally
opt-in. The live `rns-meshtastic-receive.service` monitor keeps discarding
validated envelopes after logging until an operator deliberately configures a
sink path, matching the project's staged safety model.
"""

from __future__ import annotations

from collections import deque
import socket
import struct
from typing import Protocol

from rns_meshtastic_bridge.envelope import MAX_PAYLOAD_BYTES


# 4-byte big-endian length prefix ahead of each encoded envelope. Framing is
# needed because stream sockets do not preserve message boundaries.
_LENGTH_PREFIX = struct.Struct(">I")

# An encoded BridgeEnvelope cannot exceed its header plus the payload safety
# limit; rejecting anything larger stops a corrupt or hostile peer from
# forcing an unbounded read.
MAX_FRAME_BYTES = MAX_PAYLOAD_BYTES + 64


class SinkDeliveryError(RuntimeError):
    """Raised when a sink cannot accept an envelope right now."""


class EnvelopeSink(Protocol):
    """Anything that can accept one encoded envelope without blocking long."""

    def send(self, wire: bytes) -> None: ...


class UnixSocketSink:
    """Write length-prefixed envelopes to a connected Unix domain socket.

    A Unix domain socket exists only as a filesystem path; it has no network
    interface and cannot be reached from another host. That makes it a safe
    boundary for moving validated data out of the receive-only process while
    this project is still staged well short of live Reticulum forwarding.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._socket = sock

    @classmethod
    def connect(cls, path: str) -> UnixSocketSink:
        """Connect to a sink consumer already listening at ``path``."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(path)
        return cls(sock)

    def send(self, wire: bytes) -> None:
        if len(wire) > MAX_FRAME_BYTES:
            raise SinkDeliveryError("envelope exceeds the maximum sink frame size")
        try:
            self._socket.sendall(_LENGTH_PREFIX.pack(len(wire)) + wire)
        except OSError as error:
            raise SinkDeliveryError(str(error)) from error

    def close(self) -> None:
        self._socket.close()


class InMemorySink:
    """A bounded, dependency-free sink useful for tests and local tooling.

    Delivery is synchronous and keeps only the most recent ``capacity``
    envelopes, so a consumer that stops reading cannot make this sink grow
    without limit.
    """

    def __init__(self, *, capacity: int = 256) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._items: deque[bytes] = deque(maxlen=capacity)

    def send(self, wire: bytes) -> None:
        if len(wire) > MAX_FRAME_BYTES:
            raise SinkDeliveryError("envelope exceeds the maximum sink frame size")
        self._items.append(wire)

    def drain(self) -> list[bytes]:
        """Remove and return every envelope collected so far, oldest first."""
        collected = list(self._items)
        self._items.clear()
        return collected


def read_frame(sock: socket.socket) -> bytes | None:
    """Read one length-prefixed envelope; a sink consumer's half of the protocol.

    Returns ``None`` on a clean peer shutdown instead of raising, so a
    consumer loop can treat it as ordinary end-of-stream.
    """
    header = _recv_exact(sock, _LENGTH_PREFIX.size)
    if header is None:
        return None
    (length,) = _LENGTH_PREFIX.unpack(header)
    if length > MAX_FRAME_BYTES:
        raise SinkDeliveryError("peer declared a frame larger than the safety limit")
    body = _recv_exact(sock, length)
    if body is None:
        raise SinkDeliveryError("connection closed mid-frame")
    return body


def _recv_exact(sock: socket.socket, size: int) -> bytes | None:
    """Read exactly ``size`` bytes, or None if the peer closed first."""
    if size == 0:
        return b""
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            return None if not chunks else _raise_short_read()
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _raise_short_read() -> None:
    raise SinkDeliveryError("connection closed mid-frame")
