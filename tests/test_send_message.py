"""Unit tests mirror tests/test_client.py's style closely, since send_message
is structurally the same shape (resolve a destination, send, wait for
proof) with one addition: signing. build_signed_envelope and
load_or_create_identity are tested with real RNS.Identity objects, no
mocking of the crypto.
"""

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import RNS

from rns_meshtastic_bridge import send_message as app
from rns_meshtastic_bridge.envelope import BridgeEnvelope
from rns_meshtastic_bridge.sender_auth import SenderAllowlist, unwrap_and_verify
from rns_meshtastic_bridge.protocol import decode_message


def test_parse_destination_accepts_reticulum_hash() -> None:
    destination = "01" * (app.RNS.Reticulum.TRUNCATED_HASHLENGTH // 8)
    assert app.parse_destination(destination) == bytes.fromhex(destination)


@pytest.mark.parametrize("destination", ["abcd", "z" * 32])
def test_parse_destination_rejects_invalid_input(destination: str) -> None:
    with pytest.raises(ValueError):
        app.parse_destination(destination)


class TestLoadOrCreateIdentity:
    def test_reuses_existing_identity(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        existing = object()
        monkeypatch.setattr(
            app.RNS, "Identity", type("I", (), {"from_file": staticmethod(lambda _p: existing)})
        )
        assert app.load_or_create_identity(str(tmp_path)) is existing

    def test_writes_new_identity(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        identity_path = tmp_path / app.IDENTITY_FILENAME

        class FakeIdentity:
            @staticmethod
            def from_file(_path: str) -> None:
                return None

            def to_file(self, path: str) -> bool:
                Path(path).write_text("private identity", encoding="utf-8")
                return True

        monkeypatch.setattr(app, "RNS", SimpleNamespace(Identity=FakeIdentity))
        assert isinstance(app.load_or_create_identity(str(tmp_path)), FakeIdentity)
        assert identity_path.exists()

    def test_reports_save_failure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        class FakeIdentity:
            @staticmethod
            def from_file(_path: str) -> None:
                return None

            def to_file(self, _path: str) -> bool:
                return False

        monkeypatch.setattr(app, "RNS", SimpleNamespace(Identity=FakeIdentity))
        with pytest.raises(RuntimeError, match="Could not save identity"):
            app.load_or_create_identity(str(tmp_path))


class TestBuildSignedEnvelope:
    def test_produces_a_verifiable_authorized_envelope(self) -> None:
        identity = RNS.Identity()
        assert identity.hash is not None
        allowlist = SenderAllowlist([identity.hash])

        wire = app.build_signed_envelope(identity, b"hello bridge", max_hops=4)

        verified = unwrap_and_verify(wire, allowlist)
        assert verified.identity_hash == identity.hash
        envelope = BridgeEnvelope.decode(verified.envelope_bytes)
        assert decode_message(envelope.payload) == "hello bridge"
        assert envelope.origin_id == identity.hash
        assert envelope.hops == 0
        assert envelope.max_hops == 4

    def test_two_calls_produce_different_message_ids(self) -> None:
        identity = RNS.Identity()
        assert identity.hash is not None
        allowlist = SenderAllowlist([identity.hash])

        first = BridgeEnvelope.decode(
            unwrap_and_verify(app.build_signed_envelope(identity, b"x"), allowlist).envelope_bytes
        )
        second = BridgeEnvelope.decode(
            unwrap_and_verify(app.build_signed_envelope(identity, b"x"), allowlist).envelope_bytes
        )

        assert first.message_id != second.message_id

    def test_rejects_an_identity_without_a_hash(self) -> None:
        bare = RNS.Identity(create_keys=False)
        with pytest.raises(ValueError, match="no usable hash"):
            app.build_signed_envelope(bare, b"hello")


def _prepare_client(monkeypatch: pytest.MonkeyPatch, *, has_path: bool = True) -> None:
    truncated_hash_length = app.RNS.Reticulum.TRUNCATED_HASHLENGTH

    class FakeReticulum:
        TRUNCATED_HASHLENGTH = truncated_hash_length

        def __init__(self, _config: str) -> None:
            pass

    monkeypatch.setattr(app.RNS, "Reticulum", FakeReticulum)
    monkeypatch.setattr(
        app.RNS,
        "Transport",
        SimpleNamespace(
            has_path=lambda _hash: has_path,
            request_path=lambda _hash: None,
        ),
    )


def _real_sender_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> RNS.Identity:
    """Use a real identity for run()'s own load_or_create_identity call,
    since build_signed_envelope needs real signing underneath it.
    """
    identity = RNS.Identity()
    monkeypatch.setattr(app, "load_or_create_identity", lambda _config: identity)
    return identity


class TestRun:
    def test_reports_missing_bridge_identity(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _prepare_client(monkeypatch)
        _real_sender_identity(monkeypatch, tmp_path)
        monkeypatch.setattr(app.RNS, "Identity", SimpleNamespace(recall=lambda _hash: None))

        assert app.run(str(tmp_path), "01" * 16, "hello", 1) == 1

    def test_reports_path_timeout(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _prepare_client(monkeypatch, has_path=False)
        _real_sender_identity(monkeypatch, tmp_path)
        times = iter([10.0, 11.1])
        monkeypatch.setattr(app.time, "monotonic", lambda: next(times))
        monkeypatch.setattr(app.time, "sleep", lambda _seconds: None)

        assert app.run(str(tmp_path), "01" * 16, "hello", 1) == 1

    def test_continues_when_requested_path_arrives(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        truncated_hash_length = app.RNS.Reticulum.TRUNCATED_HASHLENGTH

        class FakeReticulum:
            TRUNCATED_HASHLENGTH = truncated_hash_length

            def __init__(self, _config: str) -> None:
                pass

        path_states = iter([False, False, True])
        monkeypatch.setattr(app.RNS, "Reticulum", FakeReticulum)
        monkeypatch.setattr(
            app.RNS,
            "Transport",
            SimpleNamespace(
                has_path=lambda _hash: next(path_states),
                request_path=lambda _hash: None,
            ),
        )
        _real_sender_identity(monkeypatch, tmp_path)
        monkeypatch.setattr(app.RNS, "Identity", SimpleNamespace(recall=lambda _hash: None))
        monkeypatch.setattr(app.time, "monotonic", lambda: 10.0)
        monkeypatch.setattr(app.time, "sleep", lambda _seconds: None)

        assert app.run(str(tmp_path), "01" * 16, "hello", 1) == 1

    def test_reports_send_failure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _prepare_client(monkeypatch)
        _real_sender_identity(monkeypatch, tmp_path)
        monkeypatch.setattr(app.RNS, "Identity", SimpleNamespace(recall=lambda _hash: object()))

        class FakeDestination:
            OUT = 1
            SINGLE = 2

            def __init__(self, *_args: object) -> None:
                pass

        monkeypatch.setattr(app.RNS, "Destination", FakeDestination)
        monkeypatch.setattr(app.RNS, "Packet", lambda *_args: SimpleNamespace(send=lambda: False))

        assert app.run(str(tmp_path), "01" * 16, "hello", 1) == 1

    def test_accepts_valid_delivery_proof(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _prepare_client(monkeypatch)
        _real_sender_identity(monkeypatch, tmp_path)
        monkeypatch.setattr(app.RNS, "Identity", SimpleNamespace(recall=lambda _hash: object()))

        class FakeDestination:
            OUT = 1
            SINGLE = 2

            def __init__(self, *_args: object) -> None:
                pass

        class FakeReceipt:
            SENT = 1
            DELIVERED = 2

            def __init__(self) -> None:
                self.status = self.DELIVERED

            def set_timeout(self, _timeout: float) -> None:
                pass

            def get_rtt(self) -> float:
                return 0.05

        receipt = FakeReceipt()
        sent_wire: list[bytes] = []
        monkeypatch.setattr(app.RNS, "Destination", FakeDestination)
        monkeypatch.setattr(app.RNS, "PacketReceipt", FakeReceipt)

        def fake_packet(_destination: object, wire: bytes) -> SimpleNamespace:
            sent_wire.append(wire)
            return SimpleNamespace(send=lambda: receipt)

        monkeypatch.setattr(app.RNS, "Packet", fake_packet)

        assert app.run(str(tmp_path), "01" * 16, "hello", 1) == 0
        # The wire payload is a signed, framed envelope, not a bare string;
        # Reticulum's own destination encryption (mocked away here) is what
        # actually keeps it confidential in transit, not this signing step.
        assert len(sent_wire) == 1
        assert len(sent_wire[0]) > len(b"hello")

    def test_reports_receipt_timeout(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _prepare_client(monkeypatch)
        _real_sender_identity(monkeypatch, tmp_path)
        monkeypatch.setattr(app.RNS, "Identity", SimpleNamespace(recall=lambda _hash: object()))

        class FakeDestination:
            OUT = 1
            SINGLE = 2

            def __init__(self, *_args: object) -> None:
                pass

        class FakeReceipt:
            SENT = 1
            DELIVERED = 2
            status = SENT

            def set_timeout(self, _timeout: float) -> None:
                pass

        receipt = FakeReceipt()
        times = iter([10.0, 11.1])
        monkeypatch.setattr(app.RNS, "Destination", FakeDestination)
        monkeypatch.setattr(app.RNS, "PacketReceipt", FakeReceipt)
        monkeypatch.setattr(app.RNS, "Packet", lambda *_args: SimpleNamespace(send=lambda: receipt))
        monkeypatch.setattr(app.time, "monotonic", lambda: next(times))

        assert app.run(str(tmp_path), "01" * 16, "hello", 1) == 1

    def test_reports_a_sender_identity_without_a_hash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _prepare_client(monkeypatch)
        hashless = RNS.Identity(create_keys=False)
        monkeypatch.setattr(app, "load_or_create_identity", lambda _config: hashless)

        assert app.run(str(tmp_path), "01" * 16, "hello", 1) == 1


def test_main_passes_cli_arguments_to_run(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[tuple[str, str, str, float, int]] = []
    monkeypatch.setattr(
        app,
        "run",
        lambda config, destination, message, timeout, *, max_hops: (
            received.append((config, destination, message, timeout, max_hops)) or 7
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["send_message", "--config", "custom", "--timeout", "3", "ab" * 16, "hello"],
    )

    with pytest.raises(SystemExit) as exit_info:
        app.main()

    assert exit_info.value.code == 7
    assert received == [("custom", "ab" * 16, "hello", 3.0, app.DEFAULT_MAX_HOPS)]
