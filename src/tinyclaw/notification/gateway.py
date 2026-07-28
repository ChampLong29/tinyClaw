"""Policy-gated entry point for all proactive notifications."""

from __future__ import annotations

from typing import Any, Protocol

from tinyclaw.notification.policy import (
    NotificationDecision,
    NotificationRequest,
    SQLiteNotificationPolicy,
)
from tinyclaw.observability import TraceRecorder


class NotificationQueue(Protocol):
    def enqueue(
        self,
        channel: str,
        to: str,
        text: str,
        meta: dict[str, Any] | None = None,
    ) -> str: ...


class NotificationGateway:
    def __init__(
        self,
        *,
        policy: SQLiteNotificationPolicy,
        queue: NotificationQueue,
        trace_recorder: TraceRecorder | None = None,
    ) -> None:
        self.policy = policy
        self.queue = queue
        self.trace_recorder = trace_recorder

    def notify(self, request: NotificationRequest) -> NotificationDecision:
        decision = self.policy.evaluate(request)
        if not decision.allowed:
            self._trace(request, decision)
            return decision
        stable_identity = request.dedupe_key or request.request_id
        try:
            self.queue.enqueue(
                request.channel,
                request.peer_id,
                request.text,
                meta={
                    "account_id": request.account_id,
                    "session_id": request.session_id,
                    "intent_id": f"notification:{stable_identity}",
                    "idempotency_key": f"notification:{stable_identity}",
                    "semantic_type": "notification",
                    "topic": request.topic,
                    "dedupe_key": request.dedupe_key,
                    "notification_request_id": request.request_id,
                },
            )
        except Exception:
            failed = self.policy.fail_enqueue(decision)
            self._trace(request, failed)
            raise
        committed = self.policy.commit(decision)
        self._trace(request, committed)
        return committed

    def _trace(
        self,
        request: NotificationRequest,
        decision: NotificationDecision,
    ) -> None:
        if self.trace_recorder is None:
            return
        try:
            self.trace_recorder.record(
                event_type=f"notification_{decision.kind.value}",
                producer="notification-policy",
                producer_version=decision.policy_version,
                session_id=request.session_id,
                payload={
                    "request_id": request.request_id,
                    "topic": request.topic,
                    "dedupe_key": request.dedupe_key,
                    "priority": request.priority.value,
                    "decision": decision.kind.value,
                    "reason": decision.reason.value,
                },
            )
        except Exception:
            pass
