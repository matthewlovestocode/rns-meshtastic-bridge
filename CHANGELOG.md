# Changelog

All notable changes will be documented in this file. The project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and intends to use
[Semantic Versioning](https://semver.org/) once releases begin.

## [Unreleased]

### Added

- Standalone `rns_meshtastic_bridge` package extracted from its original
  development repository.
- Bidirectional bridge engine, bounded fragmentation and reassembly, duplicate
  suppression with restart persistence, and hop/reflection controls.
- Explicit sender authorization and recipient fan-out for Reticulum traffic.
- Default-off Meshtastic transmission with bounded queues, rate limiting,
  channel-utilization gating, and live region/modem-preset verification.
- Combined service, receive-only monitor, Reticulum destination, signed sender,
  controlled transmit-check, and local simulation command-line tools.
- Automated tests with enforced branch coverage and static type checking.
- GNU General Public License version 3 and public contribution/security
  documentation.
- Explicit alpha-version packaging and pre-stable project status warnings.

### Security

- Hardware diagnostics redact private device and channel material.
- Logging is restricted to structural metadata rather than payload contents or
  credentials.
