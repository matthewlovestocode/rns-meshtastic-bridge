# Contributing

Thank you for helping improve `rns-meshtastic-bridge`. This is alpha software
that can interact with real radios, so small and reviewable changes are strongly
preferred.

## Development setup

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pytest
uvx pyright --pythonpath .venv/bin/python src tests
```

Install `.[edge,dev]` only on a development host that needs the official
Meshtastic client. The automated test suite never requires or accesses radio
hardware.

## Safety requirements

- Transmission must remain disabled by default. Enabling it must require an
  explicit operator action and retain all authorization, configuration,
  utilization, and rate-limit gates.
- Never commit or log private keys, channel PSKs, private payloads, complete
  `meshtastic --info` output, or stable device identifiers.
- Treat data from either network as untrusted. Bound memory, time, retries,
  queues, and parser inputs, and fail closed when state is missing or stale.
- Do not make tests depend on a serial device, radio reception, public network,
  sleep timing, or another person's infrastructure.
- Keep raw wire bytes as the value passed between network-facing layers.

## Pull requests

Include tests for success and failure paths, describe any safety impact, and
update the README when CLI behavior or deployment changes. Run the full test
and type-check commands above before requesting review.

Do not include on-air test output unless the test was deliberately authorized
and the output has been checked for secrets. Software-only tests are sufficient
for ordinary contributions.
