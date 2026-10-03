"""The channel-utilization gate is tested as pure policy, no pubsub involved.

Thresholds are checked against meshtastic/firmware's own constants
(airtime.h) rather than values this project invented, so tests assert
against the named constants, not hardcoded numbers, to stay honest about
what they're actually verifying.
"""

import pytest

from rns_meshtastic_bridge.channel_util import (
    MAX_CHANNEL_UTIL_PERCENT,
    POLITE_CHANNEL_UTIL_PERCENT,
    POLITE_DUTY_CYCLE_PERCENT,
    ChannelUtilizationGate,
)


class TestConfiguration:
    def test_rejects_an_out_of_range_duty_cycle(self) -> None:
        with pytest.raises(ValueError, match="region_duty_cycle_percent"):
            ChannelUtilizationGate(region_duty_cycle_percent=0)
        with pytest.raises(ValueError, match="region_duty_cycle_percent"):
            ChannelUtilizationGate(region_duty_cycle_percent=101)

    def test_rejects_a_non_positive_max_age(self) -> None:
        with pytest.raises(ValueError, match="max_age_seconds"):
            ChannelUtilizationGate(max_age_seconds=0)


class TestFailsClosed:
    def test_denies_sending_with_no_reading_yet(self) -> None:
        gate = ChannelUtilizationGate()
        assert gate.is_send_allowed(now=0) is False

    def test_denies_sending_once_the_reading_goes_stale(self) -> None:
        gate = ChannelUtilizationGate(max_age_seconds=60)
        gate.record_telemetry(channel_utilization_percent=1, air_util_tx_percent=1, now=0)

        assert gate.is_send_allowed(now=59) is True
        assert gate.is_send_allowed(now=61) is False


class TestChannelUtilizationThreshold:
    def test_allows_below_the_polite_threshold(self) -> None:
        gate = ChannelUtilizationGate(polite=True)
        gate.record_telemetry(
            channel_utilization_percent=POLITE_CHANNEL_UTIL_PERCENT - 1,
            air_util_tx_percent=0,
            now=0,
        )
        assert gate.is_send_allowed(now=0) is True

    def test_denies_at_or_above_the_polite_threshold(self) -> None:
        gate = ChannelUtilizationGate(polite=True)
        gate.record_telemetry(
            channel_utilization_percent=POLITE_CHANNEL_UTIL_PERCENT,
            air_util_tx_percent=0,
            now=0,
        )
        assert gate.is_send_allowed(now=0) is False

    def test_impolite_mode_uses_the_higher_max_threshold(self) -> None:
        gate = ChannelUtilizationGate(polite=False)
        # Between the two thresholds: denied politely, allowed impolitely.
        between = (POLITE_CHANNEL_UTIL_PERCENT + MAX_CHANNEL_UTIL_PERCENT) / 2
        gate.record_telemetry(
            channel_utilization_percent=between, air_util_tx_percent=0, now=0
        )
        assert gate.is_send_allowed(now=0) is True

    def test_denies_at_or_above_the_max_threshold_even_when_impolite(self) -> None:
        gate = ChannelUtilizationGate(polite=False)
        gate.record_telemetry(
            channel_utilization_percent=MAX_CHANNEL_UTIL_PERCENT,
            air_util_tx_percent=0,
            now=0,
        )
        assert gate.is_send_allowed(now=0) is False


class TestAirUtilizationThreshold:
    def test_unlimited_region_never_applies_an_air_util_limit(self) -> None:
        gate = ChannelUtilizationGate(region_duty_cycle_percent=100)
        gate.record_telemetry(
            channel_utilization_percent=0, air_util_tx_percent=99.9, now=0
        )
        assert gate.is_send_allowed(now=0) is True

    def test_limited_region_denies_at_half_its_duty_cycle(self) -> None:
        # EU_868-shaped region: duty_cycle=10, polite half is 5%.
        gate = ChannelUtilizationGate(region_duty_cycle_percent=10)
        limit = 10 * POLITE_DUTY_CYCLE_PERCENT / 100
        gate.record_telemetry(
            channel_utilization_percent=0, air_util_tx_percent=limit - 0.1, now=0
        )
        assert gate.is_send_allowed(now=0) is True

        gate.record_telemetry(
            channel_utilization_percent=0, air_util_tx_percent=limit, now=0
        )
        assert gate.is_send_allowed(now=0) is False


def test_latest_exposes_the_most_recent_reading() -> None:
    gate = ChannelUtilizationGate()
    assert gate.latest is None

    gate.record_telemetry(channel_utilization_percent=5, air_util_tx_percent=2, now=10)

    assert gate.latest is not None
    assert gate.latest.channel_utilization_percent == 5
    assert gate.latest.air_util_tx_percent == 2
    assert gate.latest.observed_at == 10
