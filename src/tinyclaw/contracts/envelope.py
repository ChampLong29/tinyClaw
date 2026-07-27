"""Platform-neutral inbound and outbound gateway contracts."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from tinyclaw.contracts._common import parse_datetime, require_text, to_primitive, utc_now
from tinyclaw.contracts.versions import INBOUND_ENVELOPE_V1, OUTBOUND_INTENT_V1

if TYPE_CHECKING:
    from tinyclaw.channel.base import InboundMessage


class EventType(str, Enum):
    MESSAGE = "message"
    INTERACTION = "interaction"
    MEMBERSHIP = "membership"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class ContentBlockType(str, Enum):
    TEXT = "text"
    MARKDOWN = "markdown"
    IMAGE = "image"
    FILE = "file"
    LINK = "link"
    ACTIONS = "actions"


class SemanticType(str, Enum):
    PROGRESS = "progress"
    QUESTION = "question"
    CONFIRMATION = "confirmation"
    RESULT = "result"
    ERROR = "error"
    NOTIFICATION = "notification"


class DeliveryPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


@dataclass(frozen=True, kw_only=True)
class ContentBlock:
    type: ContentBlockType
    text: str | None = None
    artifact_ref: str | None = None
    mime_type: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type in (ContentBlockType.TEXT, ContentBlockType.MARKDOWN) and self.text is None:
            raise ValueError(f"{self.type.value} content block requires text")
        if self.type in (ContentBlockType.IMAGE, ContentBlockType.FILE) and not self.artifact_ref:
            raise ValueError(f"{self.type.value} content block requires artifact_ref")

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ContentBlock":
        return cls(
            type=ContentBlockType(data["type"]),
            text=data.get("text"),
            artifact_ref=data.get("artifact_ref"),
            mime_type=data.get("mime_type"),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, kw_only=True)
class Attachment:
    attachment_id: str
    kind: str
    artifact_ref: str
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_text(self.attachment_id, "attachment_id")
        require_text(self.kind, "kind")
        require_text(self.artifact_ref, "artifact_ref")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Attachment":
        return cls(
            attachment_id=str(data["attachment_id"]),
            kind=str(data["kind"]),
            artifact_ref=str(data["artifact_ref"]),
            filename=data.get("filename"),
            mime_type=data.get("mime_type"),
            size_bytes=data.get("size_bytes"),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, kw_only=True)
class SenderIdentity:
    platform_user_id: str
    display_name: str | None = None
    global_user_id: str | None = None
    identity_type: str = "user"

    def __post_init__(self) -> None:
        require_text(self.platform_user_id, "platform_user_id")

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SenderIdentity":
        return cls(
            platform_user_id=str(data["platform_user_id"]),
            display_name=data.get("display_name"),
            global_user_id=data.get("global_user_id"),
            identity_type=str(data.get("identity_type") or "user"),
        )


@dataclass(frozen=True, kw_only=True)
class InboundEnvelope:
    event_id: str
    channel: str
    account_id: str
    peer_id: str
    sender: SenderIdentity
    dedupe_key: str
    content_blocks: tuple[ContentBlock, ...]
    thread_id: str | None = None
    platform_message_id: str | None = None
    event_type: EventType = EventType.MESSAGE
    attachments: tuple[Attachment, ...] = ()
    reply_to: str | None = None
    received_at: datetime = field(default_factory=utc_now)
    channel_metadata: Mapping[str, Any] = field(default_factory=dict)
    raw_artifact_ref: str | None = None
    schema_version: str = INBOUND_ENVELOPE_V1

    def __post_init__(self) -> None:
        for value, name in (
            (self.event_id, "event_id"),
            (self.channel, "channel"),
            (self.account_id, "account_id"),
            (self.peer_id, "peer_id"),
            (self.dedupe_key, "dedupe_key"),
        ):
            require_text(value, name)
        if not isinstance(self.sender, SenderIdentity):
            raise TypeError("sender must be a SenderIdentity")
        if self.schema_version != INBOUND_ENVELOPE_V1:
            raise ValueError(f"unsupported inbound schema: {self.schema_version}")
        object.__setattr__(self, "received_at", parse_datetime(self.received_at, default_now=True))
        if not self.content_blocks and not self.attachments:
            raise ValueError("inbound envelope requires content_blocks or attachments")

    @property
    def text(self) -> str:
        return "\n".join(
            block.text or ""
            for block in self.content_blocks
            if block.type in (ContentBlockType.TEXT, ContentBlockType.MARKDOWN)
        )

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "InboundEnvelope":
        return cls(
            schema_version=str(data.get("schema_version") or INBOUND_ENVELOPE_V1),
            event_id=str(data["event_id"]),
            channel=str(data["channel"]),
            account_id=str(data["account_id"]),
            peer_id=str(data["peer_id"]),
            thread_id=data.get("thread_id"),
            platform_message_id=data.get("platform_message_id"),
            sender=SenderIdentity.from_dict(data["sender"]),
            event_type=EventType(data.get("event_type") or EventType.MESSAGE),
            content_blocks=tuple(
                ContentBlock.from_dict(item) for item in data.get("content_blocks") or ()
            ),
            attachments=tuple(
                Attachment.from_dict(item) for item in data.get("attachments") or ()
            ),
            reply_to=data.get("reply_to"),
            received_at=parse_datetime(data.get("received_at"), default_now=True),
            channel_metadata=dict(data.get("channel_metadata") or {}),
            dedupe_key=str(data["dedupe_key"]),
            raw_artifact_ref=data.get("raw_artifact_ref"),
        )


@dataclass(frozen=True, kw_only=True)
class OutboundTarget:
    channel: str
    account_id: str
    peer_id: str
    thread_id: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.channel, "channel"),
            (self.account_id, "account_id"),
            (self.peer_id, "peer_id"),
        ):
            require_text(value, name)

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OutboundTarget":
        return cls(
            channel=str(data["channel"]),
            account_id=str(data["account_id"]),
            peer_id=str(data["peer_id"]),
            thread_id=data.get("thread_id"),
        )


@dataclass(frozen=True, kw_only=True)
class OutboundIntent:
    session_id: str
    semantic_type: SemanticType
    target: OutboundTarget
    content_blocks: tuple[ContentBlock, ...]
    intent_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    task_id: str | None = None
    priority: DeliveryPriority = DeliveryPriority.NORMAL
    presentation_hints: Mapping[str, Any] = field(default_factory=dict)
    dedupe_key: str | None = None
    expires_at: datetime | None = None
    trace_context: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = OUTBOUND_INTENT_V1

    def __post_init__(self) -> None:
        require_text(self.intent_id, "intent_id")
        require_text(self.session_id, "session_id")
        if self.schema_version != OUTBOUND_INTENT_V1:
            raise ValueError(f"unsupported outbound schema: {self.schema_version}")
        if not self.content_blocks:
            raise ValueError("outbound intent requires at least one content block")
        object.__setattr__(self, "expires_at", parse_datetime(self.expires_at))

    def to_dict(self) -> dict[str, Any]:
        return to_primitive(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OutboundIntent":
        return cls(
            schema_version=str(data.get("schema_version") or OUTBOUND_INTENT_V1),
            intent_id=str(data["intent_id"]),
            session_id=str(data["session_id"]),
            task_id=data.get("task_id"),
            semantic_type=SemanticType(data["semantic_type"]),
            target=OutboundTarget.from_dict(data["target"]),
            content_blocks=tuple(ContentBlock.from_dict(item) for item in data["content_blocks"]),
            priority=DeliveryPriority(data.get("priority") or DeliveryPriority.NORMAL),
            presentation_hints=dict(data.get("presentation_hints") or {}),
            dedupe_key=data.get("dedupe_key"),
            expires_at=parse_datetime(data.get("expires_at")),
            trace_context=dict(data.get("trace_context") or {}),
        )


_EVENT_ID_PATHS = (
    ("header", "event_id"),
    ("event_id",),
    ("update_id",),
)

_MESSAGE_ID_PATHS = (
    ("event", "message", "message_id"),
    ("message", "message_id"),
    ("message", "msgid"),
    ("message", "msg_id"),
    ("message", "id"),
    ("message_id",),
    ("msgid",),
    ("msg_id",),
    ("msgId",),
)


def _nested_value(data: Mapping[str, Any], item_path: tuple[str, ...]) -> Any:
    current: Any = data
    for key in item_path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _first_value(data: Mapping[str, Any], paths: Iterable[tuple[str, ...]]) -> str | None:
    for item_path in paths:
        value = _nested_value(data, item_path)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _legacy_attachments(media: list[Any]) -> tuple[Attachment, ...]:
    attachments: list[Attachment] = []
    for index, item in enumerate(media):
        data = item if isinstance(item, Mapping) else {"artifact_ref": str(item)}
        artifact_ref = str(
            data.get("artifact_ref")
            or data.get("url")
            or data.get("path")
            or data.get("file_key")
            or data.get("image_key")
            or f"legacy-media:{_stable_digest(data)}"
        )
        attachments.append(
            Attachment(
                attachment_id=str(data.get("id") or f"media-{index}-{_stable_digest(data)[:12]}"),
                kind=str(data.get("type") or data.get("kind") or "file"),
                artifact_ref=artifact_ref,
                filename=data.get("filename") or data.get("name"),
                mime_type=data.get("mime_type") or data.get("mime"),
                size_bytes=data.get("size_bytes") or data.get("size"),
            )
        )
    return tuple(attachments)


def inbound_message_to_envelope(
    message: "InboundMessage",
    *,
    raw_artifact_ref: str | None = None,
    received_at: datetime | str | None = None,
) -> InboundEnvelope:
    """Adapt the current channel DTO without leaking its private raw payload."""
    raw = message.raw if isinstance(message.raw, Mapping) else {}
    platform_message_id = _first_value(raw, _MESSAGE_ID_PATHS)
    event_id = (
        _first_value(raw, _EVENT_ID_PATHS)
        or platform_message_id
        or f"legacy-{_stable_digest(raw or vars(message))[:24]}"
    )
    account_id = (message.account_id or "default").strip()
    identity = {
        "channel": message.channel,
        "account_id": account_id,
        "platform_message_id": platform_message_id,
        "event_id": event_id,
        "peer_id": message.peer_id,
        "sender_id": message.sender_id,
        "text": message.text,
    }
    event_type_raw = _first_value(raw, (("header", "event_type"), ("event_type",)))
    try:
        event_type = EventType(event_type_raw or EventType.MESSAGE)
    except ValueError:
        event_type = EventType.UNKNOWN

    return InboundEnvelope(
        event_id=event_id,
        channel=message.channel,
        account_id=account_id,
        peer_id=message.peer_id,
        thread_id=_first_value(
            raw,
            (
                ("event", "message", "thread_id"),
                ("message", "message_thread_id"),
                ("thread_id",),
            ),
        ),
        platform_message_id=platform_message_id,
        sender=SenderIdentity(platform_user_id=message.sender_id),
        event_type=event_type,
        content_blocks=(ContentBlock(type=ContentBlockType.TEXT, text=message.text),),
        attachments=_legacy_attachments(message.media),
        reply_to=_first_value(
            raw,
            (("event", "message", "parent_id"), ("message", "reply_to_message", "message_id")),
        ),
        received_at=parse_datetime(received_at, default_now=True),
        channel_metadata={"is_group": bool(message.is_group), "source": "legacy_inbound_message"},
        dedupe_key=f"inbound:{_stable_digest(identity)}",
        raw_artifact_ref=raw_artifact_ref,
    )
