"""Hardware-free integration scenarios for the complete pure bridge path."""

import pytest

from rns_meshtastic_bridge.local_simulation import SimulationOutcome, simulate_pipeline


@pytest.mark.integration
@pytest.mark.parametrize("payload_size", [0, 1, 173, 1024, 8192])
def test_payload_reaches_reticulum_decision_across_fragment_boundaries(
    payload_size: int,
) -> None:
    report = simulate_pipeline(bytes(payload_size))

    assert report.outcome is SimulationOutcome.FORWARDED
    assert report.payload_bytes == payload_size
    assert report.generated_frames >= 1
    assert report.delivered_frames == report.generated_frames
    assert report.forwarded_hops == 1


@pytest.mark.integration
def test_identical_radio_fragment_duplicate_is_harmless() -> None:
    report = simulate_pipeline(b"x" * 1024, duplicate_index=1)
    assert report.outcome is SimulationOutcome.FORWARDED
    assert report.delivered_frames == report.generated_frames + 1


@pytest.mark.integration
def test_lost_fragment_never_produces_forwarding_decision() -> None:
    report = simulate_pipeline(b"x" * 1024, drop_index=1)
    assert report.outcome is SimulationOutcome.INCOMPLETE
    assert report.forwarded_hops is None


@pytest.mark.integration
def test_corruption_never_produces_forwarding_decision() -> None:
    report = simulate_pipeline(b"x" * 1024, corrupt_index=1)
    assert report.outcome is SimulationOutcome.MALFORMED
    assert report.forwarded_hops is None


@pytest.mark.integration
def test_wrong_channel_never_produces_forwarding_decision() -> None:
    report = simulate_pipeline(b"x" * 1024, channel_index=1, delivered_channel=0)
    assert report.outcome is SimulationOutcome.INCOMPLETE
    assert report.forwarded_hops is None
