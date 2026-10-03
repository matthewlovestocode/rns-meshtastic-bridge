"""Voluntary channel/air-utilization gate, mirroring Meshtastic firmware's own.

Meshtastic devices self-throttle their *own* background traffic (telemetry,
node-info, position broadcasts) against two measurements firmware tracks:
``channel_utilization`` (% of the last 60 seconds any traffic, not just the
device's own, occupied the channel) and ``air_util_tx`` (% of the last hour
*this device* transmitted). See ``meshtastic/firmware``'s ``airtime.cpp``:
``isTxAllowedChannelUtil()`` compares channel utilization against a 25%
"polite" or 40% "less polite" threshold; ``isTxAllowedAirUtil()`` compares
air-time-transmitted against half the region's duty cycle, and is a no-op for
duty-cycle-unlimited regions such as US (``effectiveDutyCycle`` of 100).

Critically, those firmware gates only protect the device's own background
modules — nothing in firmware throttles a ``sendData()`` call an external API
client makes, which is exactly how this bridge sends. This module lets the
bridge hold itself to the same standard the device already applies to its
own traffic, fed by the device's own self-reported ``DeviceMetrics``
telemetry rather than a number this project invented. It is pure policy: it
has no pubsub subscription and no knowledge of Meshtastic's wire formats.
Feeding it live telemetry is the caller's job.
"""

from __future__ import annotations

from dataclasses import dataclass


# These three numbers are not configurable choices of this project; they are
# meshtastic/firmware's own constants (airtime.h: max_channel_util_percent,
# polite_channel_util_percent, polite_duty_cycle_percent). Keeping this gate
# at parity with firmware, rather than picking different numbers, is the
# entire point of this module.
POLITE_CHANNEL_UTIL_PERCENT = 25.0
MAX_CHANNEL_UTIL_PERCENT = 40.0
POLITE_DUTY_CYCLE_PERCENT = 50.0


@dataclass(frozen=True, slots=True)
class ChannelUtilizationReading:
    """One self-reported DeviceMetrics sample and when the gate learned of it."""

    channel_utilization_percent: float
    air_util_tx_percent: float
    observed_at: float


class ChannelUtilizationGate:
    """Decide whether this bridge may send, the way firmware decides for itself.

    Fails closed: with no reading yet, or one older than ``max_age_seconds``,
    sending is not allowed. A device that has gone quiet (crashed, lost its
    serial connection, stopped reporting telemetry) must not be mistaken for
    a device reporting a clear channel.
    """

    def __init__(
        self,
        *,
        region_duty_cycle_percent: float = 100.0,
        polite: bool = True,
        max_age_seconds: float = 600.0,
    ) -> None:
        if not 0 < region_duty_cycle_percent <= 100:
            raise ValueError("region_duty_cycle_percent must be in (0, 100]")
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        self._region_duty_cycle = region_duty_cycle_percent
        self._polite = polite
        self._max_age = max_age_seconds
        self._latest: ChannelUtilizationReading | None = None

    def record_telemetry(
        self, *, channel_utilization_percent: float, air_util_tx_percent: float, now: float
    ) -> None:
        """Record the local device's self-reported DeviceMetrics telemetry.

        The caller is responsible for confirming this telemetry came from the
        bridge's own device and not some other node on the mesh; utilization
        percentages reflect RF conditions at whichever node measured them.
        """
        self._latest = ChannelUtilizationReading(
            channel_utilization_percent=channel_utilization_percent,
            air_util_tx_percent=air_util_tx_percent,
            observed_at=now,
        )

    def is_send_allowed(self, *, now: float) -> bool:
        """True only if a fresh-enough reading exists and is under both gates."""
        latest = self._latest
        if latest is None:
            return False
        if now - latest.observed_at > self._max_age:
            return False

        channel_limit = (
            POLITE_CHANNEL_UTIL_PERCENT if self._polite else MAX_CHANNEL_UTIL_PERCENT
        )
        if latest.channel_utilization_percent >= channel_limit:
            return False

        # isTxAllowedAirUtil() is a no-op whenever the region has no duty
        # cycle (effectiveDutyCycle == 100); mirror that exactly rather than
        # applying an air-time limit firmware itself would not enforce here.
        if self._region_duty_cycle < 100:
            air_limit = self._region_duty_cycle * POLITE_DUTY_CYCLE_PERCENT / 100
            if latest.air_util_tx_percent >= air_limit:
                return False

        return True

    @property
    def latest(self) -> ChannelUtilizationReading | None:
        return self._latest
