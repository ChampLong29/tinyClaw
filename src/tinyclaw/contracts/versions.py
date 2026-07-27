"""Schema version constants for public gateway contracts."""

INBOUND_ENVELOPE_V1 = "inbound_envelope.v1"
OUTBOUND_INTENT_V1 = "outbound_intent.v1"
GLOBAL_IDENTITY_V1 = "global_identity.v1"
SESSION_DESCRIPTOR_V1 = "session_descriptor.v1"
TASK_INSTANCE_V1 = "task_instance.v1"
INTERACTION_EVENT_V1 = "interaction_event.v1"
DELIVERY_RECORD_V1 = "delivery_record.v1"
INTERACTION_TRACE_V1 = "interaction_trace.v1"

SUPPORTED_SCHEMA_VERSIONS = frozenset(
    {
        INBOUND_ENVELOPE_V1,
        OUTBOUND_INTENT_V1,
        GLOBAL_IDENTITY_V1,
        SESSION_DESCRIPTOR_V1,
        TASK_INSTANCE_V1,
        INTERACTION_EVENT_V1,
        DELIVERY_RECORD_V1,
        INTERACTION_TRACE_V1,
    }
)
