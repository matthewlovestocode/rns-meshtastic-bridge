"""Public, side-effect-free API for the Reticulum/Meshtastic bridge.

Importing this package never opens a serial device or sends a radio packet.
Live I/O is confined to explicit console-service entry points, while the
objects exported here remain suitable for tests with controllable clocks and
mocked network boundaries.
"""

from rns_meshtastic_bridge.airtime import BoundedFrameQueue, TokenBucketLimiter, next_ready_frame
from rns_meshtastic_bridge.channel_util import ChannelUtilizationGate, ChannelUtilizationReading
from rns_meshtastic_bridge.dedup_persistence import load_snapshot, save_snapshot
from rns_meshtastic_bridge.deduplication import DuplicateCache, DuplicateCacheSnapshot
from rns_meshtastic_bridge.engine import BridgeAction, BridgeDecision, BridgeEngine, Ingress
from rns_meshtastic_bridge.envelope import BridgeEnvelope
from rns_meshtastic_bridge.fragments import FragmentReassembler, fragment_message
from rns_meshtastic_bridge.local_sink import EnvelopeSink, InMemorySink, UnixSocketSink
from rns_meshtastic_bridge.meshtastic_adapter import MeshtasticReceiveAdapter
from rns_meshtastic_bridge.meshtastic_transmit import FrameTransmitter, MeshtasticTransmitAdapter
from rns_meshtastic_bridge.recipients import RecipientList, RecipientResolver, fanout_forward
from rns_meshtastic_bridge.reticulum_adapter import (
    ReticulumEgressAdapter,
    ReticulumIngressAdapter,
)
from rns_meshtastic_bridge.sender_auth import SenderAllowlist, sign_envelope

__all__ = [
    "BoundedFrameQueue",
    "BridgeAction",
    "BridgeDecision",
    "BridgeEngine",
    "BridgeEnvelope",
    "ChannelUtilizationGate",
    "ChannelUtilizationReading",
    "DuplicateCache",
    "DuplicateCacheSnapshot",
    "EnvelopeSink",
    "FragmentReassembler",
    "FrameTransmitter",
    "Ingress",
    "InMemorySink",
    "MeshtasticReceiveAdapter",
    "MeshtasticTransmitAdapter",
    "RecipientList",
    "RecipientResolver",
    "ReticulumEgressAdapter",
    "ReticulumIngressAdapter",
    "SenderAllowlist",
    "TokenBucketLimiter",
    "UnixSocketSink",
    "fanout_forward",
    "fragment_message",
    "load_snapshot",
    "next_ready_frame",
    "save_snapshot",
    "sign_envelope",
]
