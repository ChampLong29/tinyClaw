"""Clarification request lifecycle bound to task state."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

from tinyclaw.contracts._common import parse_datetime, utc_now
from tinyclaw.contracts.interaction import TaskState
from tinyclaw.interaction.request_store import (
    ClarificationRequest,
    RequestState,
    SQLiteRequestStore,
)
from tinyclaw.interaction.state_machine import TaskStateMachine


class RequestAuthorizationError(PermissionError):
    pass


class RequestExpiredError(RuntimeError):
    pass


class RequestAlreadyResolvedError(RuntimeError):
    pass


class ClarificationService:
    def __init__(
        self,
        request_store: SQLiteRequestStore,
        state_machine: TaskStateMachine,
    ) -> None:
        self.request_store = request_store
        self.state_machine = state_machine

    def open(
        self,
        *,
        task_id: str,
        global_user_id: str,
        question: str,
        required_fields: tuple[str, ...],
        actor: str,
        expires_at: datetime | str | None = None,
        default_action: str = "cancel",
    ) -> ClarificationRequest:
        task = self.state_machine.store.get_task(task_id)
        request = ClarificationRequest(
            task_id=task.task_id,
            session_id=task.session_id,
            global_user_id=global_user_id,
            question=question,
            required_fields=required_fields,
            expires_at=parse_datetime(expires_at) or (utc_now() + timedelta(minutes=15)),
            default_action=default_action,
        )
        self.request_store.create(request)
        try:
            self.state_machine.transition(
                task.task_id,
                TaskState.WAITING_USER,
                actor=actor,
                reason="clarification_opened",
                expected_revision=task.revision,
                pending_request_ref=request.request_id,
            )
        except Exception:
            self.request_store.resolve(
                request,
                state=RequestState.CANCELLED,
                expected_revision=request.revision,
            )
            raise
        return request

    def answer(
        self,
        request_id: str,
        *,
        session_id: str,
        global_user_id: str,
        answer: Mapping[str, Any],
        actor: str,
    ) -> ClarificationRequest:
        request = self.request_store.get(request_id)
        if not isinstance(request, ClarificationRequest):
            raise TypeError("request is not a clarification")
        self._authorize(request, session_id, global_user_id)
        self._ensure_open(request)
        if request.expires_at <= utc_now():
            self._expire(request, actor=actor)
            raise RequestExpiredError(request.request_id)
        missing = [
            field_name
            for field_name in request.required_fields
            if field_name not in answer or answer[field_name] in (None, "")
        ]
        if missing:
            raise ValueError(f"missing required fields: {', '.join(missing)}")

        resolved = self.request_store.resolve(
            request,
            state=RequestState.ANSWERED,
            expected_revision=request.revision,
            answer=dict(answer),
        )
        task = self.state_machine.store.get_task(request.task_id)
        self.state_machine.transition(
            task.task_id,
            TaskState.RUNNING,
            actor=actor,
            reason="clarification_answered",
            expected_revision=task.revision,
            pending_request_ref=None,
        )
        return resolved

    def expire(self, request_id: str, *, actor: str = "system") -> ClarificationRequest:
        request = self.request_store.get(request_id)
        if not isinstance(request, ClarificationRequest):
            raise TypeError("request is not a clarification")
        return self._expire(request, actor=actor)

    def _expire(self, request: ClarificationRequest, *, actor: str) -> ClarificationRequest:
        self._ensure_open(request)
        expired = self.request_store.resolve(
            request,
            state=RequestState.EXPIRED,
            expected_revision=request.revision,
        )
        task = self.state_machine.store.get_task(request.task_id)
        if task.state == TaskState.WAITING_USER:
            self.state_machine.transition(
                task.task_id,
                TaskState.EXPIRED,
                actor=actor,
                reason=f"clarification_expired:{request.default_action}",
                expected_revision=task.revision,
                pending_request_ref=None,
            )
        return expired

    @staticmethod
    def _authorize(
        request: ClarificationRequest,
        session_id: str,
        global_user_id: str,
    ) -> None:
        if request.session_id != session_id or request.global_user_id != global_user_id:
            raise RequestAuthorizationError("clarification identity/session mismatch")

    @staticmethod
    def _ensure_open(request: ClarificationRequest) -> None:
        if request.state != RequestState.OPEN:
            raise RequestAlreadyResolvedError(request.request_id)
