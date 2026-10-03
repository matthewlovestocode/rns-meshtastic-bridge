# rns-meshtastic-bridge

A safety-staged bridge between the [Reticulum Network Stack](https://reticulum.network/)
and [Meshtastic](https://meshtastic.org/) LoRa mesh radios.

This project lets a Reticulum network and a Meshtastic mesh exchange
messages through one host that is physically connected to a Meshtastic
radio over USB. It is built in deliberately small, independently tested
layers, and **transmission onto the Meshtastic radio is disabled by default
everywhere in this codebase** — you opt into it explicitly, after reading
the Safety model section below.

This is early-stage software. Read the whole README, especially "Safety
model" and "What this does not do yet," before connecting it to a real
radio.

> **Project status:** alpha (`0.1.0a1`). This package is not a stable release
> and is not ready for production use. The wire format, configuration, and CLI
> may change incompatibly before `1.0.0`. Transmission remains off unless
> explicitly enabled by an operator.

## What it does

```text
Reticulum network  <-- TCP/your transport -->  this host  <-- USB -->  Meshtastic radio
                                                   |
                                          one BridgeEngine
                                       (loop prevention, dedup,
                                        hop limits) shared by
                                          both directions
```

- **Reticulum → Meshtastic**: a Reticulum destination receives signed,
  authorized messages, validates and deduplicates them, and (only once you
  enable it) fragments and transmits them onto a Meshtastic channel.
- **Meshtastic → Reticulum**: messages received on a specific Meshtastic
  port/channel are validated, deduplicated, and fanned out as Reticulum
  packets to a small list of authorized recipients.
- A single shared `BridgeEngine` prevents loops and duplicate delivery
  across both directions, keyed by message ID and origin, with hop limits.

## Why one process

A Meshtastic `SerialInterface` only supports one exclusive owner of the USB
connection, and receiving (via its pubsub callbacks) and transmitting (via
`sendData()`) both go through that same connection. This package's combined
service (`rns_meshtastic_bridge.bridge_service`) is therefore a single
process that owns the serial connection, the Reticulum destination, and the
one shared `BridgeEngine`. It cannot be split into a separate "receive
process" and "transmit process" without breaking that exclusivity.

## Safety model

This is the most important section. Read it before deploying anything.

- **Transmission is off by default, everywhere.** `MeshtasticTransmitAdapter`
  must be explicitly constructed with `enabled=True`, with no code path that
  defaults it to on. The combined service (`rns-bridge-service`) only does
  so when you pass **both** `--enable-transmit` and `--rate`.
- **Sender authorization mirrors Meshtastic firmware's own `admin_key`
  model.** Meshtastic restricts privileged operations to senders who
  cryptographically prove possession of a private key on a short,
  operator-edited allowlist. Reticulum has no equivalent "prove it by
  decrypting successfully" step for a plain destination, so senders instead
  sign their message with their own `RNS.Identity`
  (`rns_meshtastic_bridge.sender_auth.sign_envelope`), and the bridge
  verifies that signature before checking the signer's identity hash
  against `--allowlist-file`. An unauthorized or forged message never even
  reaches envelope parsing.
- **Meshtastic-forward recipients are an explicit, small, local allowlist
  too.** Reticulum has no broadcast primitive, so a message approved for the
  Reticulum side is fanned out as one packet per entry in
  `--recipients-file`, not sent to "everyone."
- **The channel-utilization gate mirrors Meshtastic firmware's own
  self-throttling exactly** (`rns_meshtastic_bridge.channel_util`,
  thresholds copied from `meshtastic/firmware`'s `airtime.cpp`: 25%/40%
  channel utilization, half of a region's duty cycle for airtime
  transmitted). Meshtastic firmware does **not** apply these limits to an
  external API client's `sendData()` calls — only to its own background
  telemetry/position modules — so this package applies them voluntarily
  instead of inventing its own numbers.
- **The rate limiter (`rns_meshtastic_bridge.airtime.TokenBucketLimiter`)
  has no built-in default rate.** On-air time depends on your radio's
  spreading factor, bandwidth, and coding rate, and on your local spectrum
  regulations — none of which this package can see or assume. You must pick
  a `--rate` appropriate for your own deployment. As a reference point: a
  single 200-byte frame at Meshtastic's `MEDIUM_FAST` preset (SF9/250kHz)
  takes roughly 0.87 seconds of airtime; at `LONG_FAST` (SF11/250kHz,
  firmware's regional default) it's roughly 2.9 seconds. Size your rate
  accordingly, and err conservative.
- **The device's live region/modem-preset configuration is verified before
  any transmission.** `rns_meshtastic_bridge.transmit_check.verify_device_config()`
  hardcodes an *expected* region and preset (edit these constants for your
  own deployment, documented in that module) and refuses to transmit if the
  connected device reports anything else.
- **Nothing in this codebase logs payload contents, private keys, or
  channel PSKs.** Error paths log only structural facts (lengths, IDs,
  dispositions). If you extend this project, keep that invariant.
- **This has been tested against real hardware exactly once** (one
  operator-approved manual frame, via `rns-bridge-transmit-check --live`,
  during this package's original development). Treat continuous,
  unattended transmission as a decision you make deliberately, not a
  default state to arrive at by configuration drift.

## Installation

```sh
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"            # core + test tooling, any host
pip install -e ".[edge,dev]"       # add this on the host with the Meshtastic radio
```

`[edge]` pulls in the pinned official `meshtastic` Python client. It's kept
as an optional extra so every other part of this package — the protocol
core, the Reticulum destination, all tests — installs and runs on a host
with no Meshtastic hardware or driver dependencies at all.

## Quickstart

1. **On the host with the Meshtastic radio**, find its stable serial path
   (Linux: prefer `/dev/serial/by-id/...` over `/dev/ttyACM0`, which can
   change across reboots):

   ```sh
   ls /dev/serial/by-id/
   ```

2. **Check the device, read-only** (never prints secrets):

   ```sh
   PORT=/dev/serial/by-id/your-device-here deploy/check-hardware.sh
   ```

3. **Create the sender and recipient allowlists** (see Configuration below).
   An empty file is valid — it just means nobody is authorized yet.

   ```sh
   touch bridge-allowlist.txt bridge-recipients.txt
   ```

4. **Run the combined service**, transmission off:

   ```sh
   rns-bridge-service \
     --serial /dev/serial/by-id/your-device-here \
     --channel 1 \
     --allowlist-file bridge-allowlist.txt \
     --recipients-file bridge-recipients.txt \
     --dedup-snapshot ~/.reticulum-bridge/dedup-snapshot.bin
   ```

   It prints the bridge's own Reticulum destination hash on startup — that's
   the address a sender needs.

5. **From another host, with this package installed** (the `[edge]` extra is
   not needed here — `rns-bridge-send-message` is pure Reticulum), send a
   message:

   ```sh
   rns-bridge-send-message <bridge-destination-hash-from-step-4> "hello mesh"
   ```

   It prints *its own* sender identity hash. Add that hash to the bridge's
   `bridge-allowlist.txt` (one hex hash per line) and re-run the send — only
   then will the bridge forward it onto Meshtastic, and only once you've
   separately enabled transmission (step 6).

6. **Enable transmission**, only when you're ready, with a rate you've
   deliberately chosen (see Safety model):

   ```sh
   rns-bridge-service \
     --serial /dev/serial/by-id/your-device-here \
     --channel 1 \
     --allowlist-file bridge-allowlist.txt \
     --recipients-file bridge-recipients.txt \
     --enable-transmit --rate 0.05
   ```

## Configuration

### Sender allowlist (`--allowlist-file`)

One hex-encoded Reticulum identity hash per line. `#`-prefixed lines and
blank lines are ignored.

```text
# Alice's sender identity
6d6d6d6d6d6d6d6d6d6d6d6d6d6d6d6d
```

A sender's own identity hash is printed every time `rns-bridge-send-message`
runs. There is no self-service enrollment; adding a line to this file is the
entire authorization mechanism today (see "What this does not do yet").

### Recipient list (`--recipients-file`)

Same format, but these are Reticulum *destination* hashes that receive a
copy of every Meshtastic-originated message the bridge forwards.

```text
# Bob's receiving destination
7261646961746f726a6f626a6f626a6f
```

### Region and modem preset

`rns_meshtastic_bridge.transmit_check` hardcodes the expected region/preset
as plain integers (not Meshtastic protobuf imports, so this module stays
testable without the `[edge]` extra installed). Edit
`EXPECTED_REGION_CODE`/`EXPECTED_PRESET_CODE` in that file for your own
deployment before ever passing `--enable-transmit`. The values are
Meshtastic's own protobuf enum integers (`Config.LoRaConfig.RegionCode`,
`Config.LoRaConfig.ModemPreset` in `meshtastic/protobufs`).

### systemd

`deploy/rns-meshtastic-bridge.service` is a template unit. Copy it to
`~/.config/systemd/user/`, edit the paths/serial device/channel for your
deployment, then:

```sh
systemctl --user daemon-reload
systemctl --user enable --now rns-meshtastic-bridge.service
journalctl --user -u rns-meshtastic-bridge.service -f
```

## Command-line tools

All installed as console scripts once this package is `pip install`ed:

| Command | What it does |
| --- | --- |
| `rns-bridge-service` | The combined live process: owns the Meshtastic connection, the Reticulum destination, and the shared `BridgeEngine`. Transmit off by default. |
| `rns-bridge-receive` | A standalone, receive-only Meshtastic monitor. Validates and discards (or forwards to a local sink) completed envelopes; never transmits. Useful for soak-testing the hardware/serial link in isolation from Reticulum. |
| `rns-bridge-reticulum-destination` | A standalone Reticulum destination that validates, authorizes, and logs forward decisions, without a live Meshtastic connection. Useful for testing the Reticulum-facing half in isolation. |
| `rns-bridge-transmit-check` | A one-shot operator tool for a controlled on-air test: dry-run by default, `--live` requires explicit confirmation flags and verifies the device's region/preset before sending one message. |
| `rns-bridge-send-message` | Sends one signed, authorized message to a bridge's Reticulum destination and waits for delivery proof. |

Every tool supports `--help`.

## Architecture reference

| Module | Responsibility |
| --- | --- |
| `envelope.py` | Versioned binary envelope: message/origin IDs, hop counts, payload. |
| `fragments.py` | Splits an envelope into Meshtastic-sized frames; reassembles them, bounded against memory/time exhaustion. |
| `deduplication.py` / `dedup_persistence.py` | TTL duplicate-suppression cache; atomic snapshot/restore across restarts. |
| `engine.py` | `BridgeEngine`: the pure forward/drop decision (origin check, dedup, hop limit). No I/O. |
| `meshtastic_adapter.py` | Receive-only Meshtastic boundary: port/channel filtering, reassembly. Its `send()` always raises. |
| `meshtastic_transmit.py` | The only code that can transmit a Meshtastic frame. Disabled unless explicitly constructed otherwise. |
| `airtime.py` | Token-bucket rate limiter and a bounded, byte-and-frame-limited outbound queue. |
| `channel_util.py` | Voluntary self-throttling matching Meshtastic firmware's own channel/air-utilization gates. |
| `reticulum_adapter.py` | Owns `RNS.Packet` construction/classification for the Reticulum egress and ingress boundary. |
| `reticulum_destination.py` | The live Reticulum destination: identity, destination setup, authorization gate, forward decision. |
| `sender_auth.py` | Signature-based sender authorization (mirrors Meshtastic's `admin_key`). |
| `recipients.py` | Resolves and fans a Meshtastic-originated message out to authorized Reticulum recipients. |
| `local_sink.py` | An opt-in local hand-off point (Unix domain socket or in-memory) for debugging/inspection, not a production data path. |
| `bridge_service.py` | The combined live process tying everything above together. |
| `transmit_check.py`, `send_message.py` | Operator CLI tools. |
| `local_simulation.py` | Hardware-free, in-memory simulation of the full receive/fragment/dedup/forward pipeline. Run it with `python -m rns_meshtastic_bridge.local_simulation --payload-bytes 4096`. |

## Development

```sh
pip install -e ".[dev]"
pytest                 # runs the full suite with branch coverage, 90% floor enforced
uvx pyright --pythonpath .venv/bin/python src tests
```

Tests never touch real hardware. RNS (Reticulum) is a normal dependency
exercised directly in tests (including real cryptographic signing in
`sender_auth`'s tests — no mocked crypto); Meshtastic's client library is
only imported dynamically at the outer edge of live-process entry points, so
the full suite runs without the `[edge]` extra installed.

See [CONTRIBUTING.md](CONTRIBUTING.md) before proposing changes and
[SECURITY.md](SECURITY.md) for private vulnerability-reporting guidance.
Notable changes are tracked in [CHANGELOG.md](CHANGELOG.md).

## License

Copyright 2026 Matthew Stewart.

This project is licensed under the
[GNU General Public License v3.0](LICENSE). You may use, study, modify, and
redistribute it under the license's terms; distributed modified or combined
versions must provide corresponding source as required by GPLv3. Reticulum and
Meshtastic remain governed by their respective upstream licenses.

## What this does not do yet

- **No web-based or self-service authorization.** The sender allowlist and
  recipient list are flat, hand-edited files. They're intentionally shaped
  (one hash per line) so a future identity-management system could populate
  them some other way without changing how a message is verified — but that
  system doesn't exist here.
- **No second-radio RF validation.** The one real on-air test this project
  has seen confirmed the software path end-to-end against real hardware,
  not that another node received it over the air.
- **No rate-limit or channel-utilization enforcement across multiple bridge
  instances.** Each instance's gates are local to itself.
- **No license has been chosen yet.** Add one (and a `LICENSE` file) before
  treating this as usable by others under specific terms.

## Acknowledgments

The channel-utilization and sender-authorization designs are deliberately
modeled on mechanisms already present in
[`meshtastic/firmware`](https://github.com/meshtastic/firmware) — see the
Safety model section for the specific files/functions referenced. This
project is not affiliated with the Meshtastic or Reticulum projects.
