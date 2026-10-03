## Summary

Describe the behavior changed and why.

## Verification

- [ ] `pytest`
- [ ] `pyright src tests`
- [ ] No payloads, private keys, channel PSKs, or device-specific identifiers were added
- [ ] Transmission remains disabled by default
- [ ] Hardware or on-air testing is clearly identified and was explicitly authorized

## Safety impact

Explain any effect on authorization, rate limiting, channel-utilization gates,
deduplication, persistence, logging, radio configuration, or transmission.
