"""Versioned, platform-neutral contracts used across the gateway."""

from tinyclaw.contracts.delivery import DeliveryRecord, DeliveryState, DeliveryTarget
from tinyclaw.contracts.envelope import (
    Attachment,
    ContentBlock,
    ContentBlockType,
    DeliveryPriority,
    EventType,
    InboundEnvelope,
    OutboundIntent,
    OutboundTarget,
    SemanticType,
    SenderIdentity,
    inbound_message_to_envelope,
)
from tinyclaw.contracts.identity import (
    ChannelIdentityLink,
    GlobalIdentity,
    IdentityStatus,
    SessionDescriptor,
    SessionScope,
    build_route_key,
)
from tinyclaw.contracts.interaction import (
    CancellationToken,
    FailureInfo,
    InteractionEvent,
    InteractionEventType,
    TaskInstance,
    TaskState,
)
from tinyclaw.contracts.trace import TraceContext, TraceEvent

__all__ = [
    "Attachment",
    "CancellationToken",
    "ChannelIdentityLink",
    "ContentBlock",
    "ContentBlockType",
    "DeliveryPriority",
    "DeliveryRecord",
    "DeliveryState",
    "DeliveryTarget",
    "EventType",
    "FailureInfo",
    "GlobalIdentity",
    "IdentityStatus",
    "InboundEnvelope",
    "InteractionEvent",
    "InteractionEventType",
    "OutboundIntent",
    "OutboundTarget",
    "SemanticType",
    "SenderIdentity",
    "SessionDescriptor",
    "SessionScope",
    "TaskInstance",
    "TaskState",
    "TraceContext",
    "TraceEvent",
    "build_route_key",
    "inbound_message_to_envelope",
]
