"""Unit tests for the bridge's Reticulum destination, run without rnsd.

Identity and ``run()`` tests mirror ``tests/test_server.py``'s monkeypatching
style, since RNS is a normal dependency everywhere (not edge-only hardware).
``BridgeDestination`` itself is tested with a real ``BridgeEngine``, a real
``SenderAllowlist``, and real ``RNS.Identity`` signing (no mocking of the
crypto), the same way ``rns_meshtastic_bridge.local_simulation`` composes real layers.
"""

import logging
from pathlib import Path
import sys
from typing import cast
from unittest.mock import Mock

import pytest
import RNS

from rns_meshtastic_bridge import reticulum_destination as module
from rns_meshtastic_bridge.engine import BridgeEngine, Ingress
from rns_meshtastic_bridge.envelope import BridgeEnvelope
from rns_meshtastic_bridge.reticulum_destination import BridgeDestination
from rns_meshtastic_bridge.sender_auth import SenderAllowlist, sign_envelope


LOCAL_ID = b"l" * 16
REMOTE_ID = b"r" * 16
MESSAGE_ID = b"m" * 16


def remote_envelope(*, hops: int = 0, payload: bytes = b"payload") -> BridgeEnvelope:
    return BridgeEnvelope(
        message_id=MESSAGE_ID, origin_id=REMOTE_ID, payload=payload, hops=hops
    )


@pytest.fixture(scope="module")
def sender_identity() -> RNS.Identity:
    return RNS.Identity()


@pytest.fixture(scope="module")
def stranger_identity() -> RNS.Identity:
    return RNS.Identity()


@pytest.fixture
def allowlist(sender_identity: RNS.Identity) -> SenderAllowlist:
    assert sender_identity.hash is not None
    return SenderAllowlist([sender_identity.hash])


def signed(envelope: BridgeEnvelope, identity: RNS.Identity) -> bytes:
    return sign_envelope(identity, envelope.encode())


class TestLoadOrCreateIdentity:
    def test_reuses_existing_identity(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        existing = object()
        monkeypatch.setattr(
            module.RNS, "Identity", type("I", (), {"from_file": staticmethod(lambda _p: existing)})
        )
        assert module.load_or_create_identity(str(tmp_path)) is existing

    def test_writes_new_identity(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        identity_path = tmp_path / module.IDENTITY_FILENAME

        class FakeIdentity:
            @staticmethod
            def from_file(_path: str) -> None:
                return None

            def to_file(self, path: str) -> bool:
                Path(path).write_text("private identity", encoding="utf-8")
                return True

        monkeypatch.setattr(module, "RNS", Mock(Identity=FakeIdentity))
        assert isinstance(module.load_or_create_identity(str(tmp_path)), FakeIdentity)
        assert identity_path.exists()

    def test_reports_save_failure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        class FakeIdentity:
            @staticmethod
            def from_file(_path: str) -> None:
                return None

            def to_file(self, _path: str) -> bool:
                return False

        monkeypatch.setattr(module, "RNS", Mock(Identity=FakeIdentity))
        with pytest.raises(RuntimeError, match="Could not save identity"):
            module.load_or_create_identity(str(tmp_path))


class TestBridgeDestination:
    def test_forwards_a_valid_signed_envelope_and_logs_metadata_not_payload(
        self,
        sender_identity: RNS.Identity,
        allowlist: SenderAllowlist,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        engine = BridgeEngine(LOCAL_ID)
        on_forward = Mock()
        destination = BridgeDestination(
            engine, allowlist, on_forward=on_forward, clock=lambda: 0
        )
        envelope = remote_envelope(payload=b"secret application data")

        with caplog.at_level(logging.INFO):
            destination.packet_received(signed(envelope, sender_identity), packet=object())

        on_forward.assert_called_once()
        wire = on_forward.call_args.args[0]
        forwarded = BridgeEnvelope.decode(wire)
        assert forwarded.hops == 1
        assert "payload_bytes=23" in caplog.text
        assert "secret application data" not in caplog.text

    def test_drops_a_reflected_envelope_without_forwarding(
        self, sender_identity: RNS.Identity, allowlist: SenderAllowlist
    ) -> None:
        engine = BridgeEngine(LOCAL_ID, id_factory=lambda length: b"i" * length)
        originated = engine.originate(b"hello", now=0)
        on_forward = Mock()
        destination = BridgeDestination(
            engine, allowlist, on_forward=on_forward, clock=lambda: 1
        )

        destination.packet_received(signed(originated, sender_identity), packet=object())

        on_forward.assert_not_called()

    def test_drops_a_duplicate_without_forwarding_twice(
        self, sender_identity: RNS.Identity, allowlist: SenderAllowlist
    ) -> None:
        engine = BridgeEngine(LOCAL_ID)
        on_forward = Mock()
        destination = BridgeDestination(
            engine, allowlist, on_forward=on_forward, clock=lambda: 0
        )
        envelope = remote_envelope()

        destination.packet_received(signed(envelope, sender_identity), packet=object())
        destination.packet_received(signed(envelope, sender_identity), packet=object())

        on_forward.assert_called_once()

    def test_malformed_packet_is_logged_without_raising_or_forwarding(
        self,
        sender_identity: RNS.Identity,
        allowlist: SenderAllowlist,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        engine = BridgeEngine(LOCAL_ID)
        on_forward = Mock()
        destination = BridgeDestination(
            engine, allowlist, on_forward=on_forward, clock=lambda: 0
        )
        # A validly signed wrapper around bytes that are not a BridgeEnvelope.
        bad_wire = sign_envelope(sender_identity, b"not an envelope")

        with caplog.at_level(logging.WARNING):
            destination.packet_received(bad_wire, packet=object())

        on_forward.assert_not_called()
        assert "malformed" in caplog.text

    def test_an_unauthorized_sender_is_rejected_before_envelope_parsing(
        self,
        stranger_identity: RNS.Identity,
        allowlist: SenderAllowlist,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        engine = BridgeEngine(LOCAL_ID)
        on_forward = Mock()
        destination = BridgeDestination(
            engine, allowlist, on_forward=on_forward, clock=lambda: 0
        )

        with caplog.at_level(logging.WARNING):
            destination.packet_received(
                signed(remote_envelope(), stranger_identity), packet=object()
            )

        on_forward.assert_not_called()
        assert "unauthorized" in caplog.text

    def test_an_unsigned_or_forged_packet_is_rejected(
        self, allowlist: SenderAllowlist, caplog: pytest.LogCaptureFixture
    ) -> None:
        engine = BridgeEngine(LOCAL_ID)
        on_forward = Mock()
        destination = BridgeDestination(
            engine, allowlist, on_forward=on_forward, clock=lambda: 0
        )

        with caplog.at_level(logging.WARNING):
            destination.packet_received(remote_envelope().encode(), packet=object())

        on_forward.assert_not_called()
        assert "unverifiable" in caplog.text


class TestRun:
    def test_creates_destination_and_announces(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        created: list[object] = []

        class StopLoop(Exception):
            pass

        class FakeDestination:
            IN = 1
            SINGLE = 2
            PROVE_ALL = 3

            def __init__(self, *_args: object) -> None:
                self.hexhash = "cd" * 16
                self.proof_strategy: int | None = None
                self.callback: object | None = None
                self.announced: bytes | None = None
                created.append(self)

            def set_proof_strategy(self, strategy: int) -> None:
                self.proof_strategy = strategy

            def set_packet_callback(self, callback: object) -> None:
                self.callback = callback

            def announce(self, app_data: bytes) -> None:
                self.announced = app_data

        fake_identity = cast(module.RNS.Identity, type("I", (), {"hash": b"i" * 16})())
        monkeypatch.setattr(module.RNS, "Reticulum", lambda _config: None)
        monkeypatch.setattr(module.RNS, "Destination", FakeDestination)
        monkeypatch.setattr(module.RNS, "log", lambda *_args: None)
        monkeypatch.setattr(module, "load_or_create_identity", lambda _config: fake_identity)
        monkeypatch.setattr(module.time, "sleep", lambda _s: (_ for _ in ()).throw(StopLoop))

        on_forward = Mock()
        with pytest.raises(StopLoop):
            module.run(
                str(tmp_path), 60, allowlist=SenderAllowlist([]), on_forward=on_forward
            )

        destination = cast(FakeDestination, created[0])
        assert destination.proof_strategy == FakeDestination.PROVE_ALL
        assert destination.callback is not None
        assert destination.announced == module.BRIDGE_ANNOUNCE_APP_DATA
        assert (tmp_path / module.DESTINATION_FILENAME).read_text(encoding="utf-8") == (
            "cd" * 16 + "\n"
        )

    def test_the_wired_callback_actually_reaches_on_forward(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        sender_identity: RNS.Identity,
    ) -> None:
        """run() must wire the real BridgeDestination, not a stand-in."""
        captured_callback: dict[str, object] = {}

        class StopLoop(Exception):
            pass

        class FakeDestination:
            IN = 1
            SINGLE = 2
            PROVE_ALL = 3

            def __init__(self, *_args: object) -> None:
                self.hexhash = "ef" * 16

            def set_proof_strategy(self, _strategy: int) -> None:
                pass

            def set_packet_callback(self, callback: object) -> None:
                captured_callback["callback"] = callback

            def announce(self, app_data: bytes) -> None:
                del app_data

        fake_identity = cast(module.RNS.Identity, type("I", (), {"hash": b"i" * 16})())
        monkeypatch.setattr(module.RNS, "Reticulum", lambda _config: None)
        monkeypatch.setattr(module.RNS, "Destination", FakeDestination)
        monkeypatch.setattr(module.RNS, "log", lambda *_args: None)
        monkeypatch.setattr(module, "load_or_create_identity", lambda _config: fake_identity)
        monkeypatch.setattr(module.time, "sleep", lambda _s: (_ for _ in ()).throw(StopLoop))

        assert sender_identity.hash is not None
        on_forward = Mock()
        with pytest.raises(StopLoop):
            module.run(
                str(tmp_path),
                60,
                allowlist=SenderAllowlist([sender_identity.hash]),
                on_forward=on_forward,
            )

        callback = captured_callback["callback"]
        assert callable(callback)
        callback(signed(remote_envelope(), sender_identity), object())  # type: ignore[operator]
        on_forward.assert_called_once()


def test_run_requires_an_identity_with_a_hash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(module.RNS, "Reticulum", lambda _config: None)
    hashless_identity = cast(module.RNS.Identity, type("I", (), {"hash": None})())
    monkeypatch.setattr(module, "load_or_create_identity", lambda _config: hashless_identity)

    with pytest.raises(RuntimeError, match="no hash"):
        module.run(str(tmp_path), 60, allowlist=SenderAllowlist([]), on_forward=Mock())


def test_default_on_forward_logs_and_drops(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        module._log_forward(b"x" * 10)
    assert "no Meshtastic egress" in caplog.text


def test_main_parses_arguments_loads_the_allowlist_and_calls_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sender_identity: RNS.Identity
) -> None:
    assert sender_identity.hash is not None
    allowlist_path = tmp_path / "allowlist.txt"
    allowlist_path.write_text(
        f"# trusted\n{sender_identity.hash.hex()}\n", encoding="utf-8"
    )

    received: list[tuple[str, int, int]] = []
    monkeypatch.setattr(
        module,
        "run",
        lambda config, interval, *, allowlist, on_forward: received.append(
            (config, interval, len(allowlist))
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bridge",
            "--config",
            "/tmp/rns-bridge",
            "--announce-interval",
            "15",
            "--allowlist-file",
            str(allowlist_path),
        ],
    )

    module.main()

    assert received == [("/tmp/rns-bridge", 15, 1)]


def test_cli_requires_allowlist_file() -> None:
    with pytest.raises(SystemExit):
        module._parser().parse_args(["--config", "/tmp/x"])
