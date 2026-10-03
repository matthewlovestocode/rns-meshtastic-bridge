#!/bin/sh

# Perform a read-only Meshtastic board check.
# This asks the device for its information; it does not flash firmware, change a
# channel, change a region/modem preset, or send a mesh message.

set -eu

# Point this at wherever you installed this package's virtual environment.
VENV_DIR=${VENV_DIR:-$HOME/rns-meshtastic-bridge/.venv}
MESHTASTIC="$VENV_DIR/bin/meshtastic"

# /dev/serial/by-id is stable across reboots and USB enumeration-order changes,
# unlike /dev/ttyACM0. Override PORT for your own device:
#
#   PORT=/dev/serial/by-id/... ./check-hardware.sh
PORT=${PORT:?Set PORT to your Meshtastic device's serial path first}

if [ ! -x "$MESHTASTIC" ]; then
    echo "Missing $MESHTASTIC; pip install this package with the [edge] extra first" >&2
    exit 1
fi

if [ ! -e "$PORT" ]; then
    echo "Meshtastic serial device not found: $PORT" >&2
    exit 1
fi

# The stock --info report also prints channel keys and the device private key.
# Those values must not end up in terminal scrollback, CI logs, or bug reports.
# awk consumes the report but emits only the three non-secret summary records.
# If Meshtastic changes these headings, the final count check fails instead of
# silently claiming that an empty report was successful.
SUMMARY=$(
    "$MESHTASTIC" --port "$PORT" --info |
        awk '/^(Owner|My info|Metadata):/ { print; found += 1 } END { if (found != 3) exit 1 }'
)

printf '%s\n' "$SUMMARY"
