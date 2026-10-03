"""The small set of names and codecs this package's Reticulum destination uses.

Reticulum derives a destination's address from textual application and
aspect names. Keeping those two values here, instead of inline at each call
site, gives every module in this package a single definition of the bridge's
own Reticulum identity.

Changing either name changes the destination hash, even when the same
private identity file is reused — treat them as part of this package's wire
protocol, not configuration to casually edit on a running deployment.
"""

# These two names become part of the bridge's Reticulum destination hash.
APP_NAME = "rns_meshtastic_bridge"
BRIDGE_DESTINATION_ASPECT = "bridge"

# Announce data is public metadata. It helps a browsing client recognize what
# the destination offers, but it is not used to calculate the destination hash.
BRIDGE_ANNOUNCE_APP_DATA = b"rns-meshtastic-bridge"


def encode_message(message: str) -> bytes:
    """Encode user-visible text into the bytes a BridgeEnvelope payload carries."""
    return message.encode("utf-8")


def decode_message(payload: bytes) -> str:
    """Decode payload bytes without letting malformed text raise.

    Network input is untrusted. Replacing invalid sequences keeps a
    long-running process alive and still gives an operator readable log
    output.
    """
    return payload.decode("utf-8", errors="replace")
