"""Specific failures callers can handle without parsing error messages."""


class BridgeProtocolError(ValueError):
    """Base class for malformed or unsupported bridge data."""


class MalformedEnvelope(BridgeProtocolError):
    """An envelope is truncated, inconsistent, or outside safety limits."""


class MalformedFragment(BridgeProtocolError):
    """A fragment header or fragment sequence is invalid."""


class ReassemblyCapacityExceeded(BridgeProtocolError):
    """Untrusted fragments exceeded a configured memory bound."""
