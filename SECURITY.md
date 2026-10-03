# Security policy

## Supported versions

This project is pre-release alpha software. Security fixes are applied to the
latest commit only; no released version currently receives backports.

## Reporting a vulnerability

Once this repository is hosted on GitHub, use GitHub's private vulnerability
reporting feature rather than a public issue. Until a private reporting channel
is configured, do not publish exploit details or credentials; contact the
repository owner privately through the hosting platform.

Never include Reticulum identity files, private keys, Meshtastic channel PSKs,
complete `meshtastic --info` output, private message payloads, or precise private
deployment details in a report. Redact logs to structural facts such as packet
lengths, message IDs, dispositions, and software versions.

Please include the affected commit/version, impact, minimal reproduction, and
whether transmission had been explicitly enabled. You should receive an
acknowledgment within seven days after a private channel is available.
