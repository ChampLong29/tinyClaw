"""Interaction orchestrator connecting persistent tasks to a Runtime Port."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Mapping

from tinyclaw.contracts.interaction import (
    FailureInfo,
    InteractionEventType,
    TaskInstance,
    TaskState,
)
from tinyclaw.interaction.clarification import ClarificationService
from tinyclaw.interaction.confirmation import ConfirmationService
from tinyclaw.interaction.progress import (
    ProgressCoalescer,
    ProgressEvent,
    ProgressEventType,
)
from tinyclaw.interaction.request_store import RiskLevel
from tinyclaw.interaction.state_machine import TaskStateMachine
from tinyclaw.runtime.port import RuntimeEvent, RuntimeEventType, RuntimePort, TaskContext

ProgressSink = Callable[[ProgressEvent], None]
ConfirmationSink = Callable[[str, str], None]


class InteractionOrchestrator:
    """Owns task lifecycle while the Runtime owns reasoning and tools."""

    def __init__(
        self,
        *,
        state_machine: TaskStateMachine,
        runtime: RuntimePort,
        clarification_service: ClarificationService | None = None,
        confirmation_service: ConfirmationService | None = None,
        progress_coalescer: ProgressCoalescer | None = None,
        progress_sink: ProgressSink | None = None,
        confirmation_sink: ConfirmationSink | None = None,
    ) -> None:
        self.state_machine = state_machine
        self.runtime = runtime
        self.clarification_service = clarification_service
        self.confirmation_service = confirmation_service
        self.progress_coalescer = progress_coalescer or ProgressCoalescer()
        self.progress_sink = progress_sink
        self.confirmation_sink = confirmation_sink

    def start(
        self,
        *,
        session_id: str,
        global_user_id: str,
        user_goal: str,
        runtime_ref: str,
        task_id: str | None = None,
        trace_id: str | None = None,
    ) -> TaskInstance:
        task = self.state_machine.create_task(
            session_id=session_id,
            user_goal=user_goal,
            actor=global_user_id,
            runtime_ref=runtime_ref,
            task_id=task_id,
            trace_id=trace_id,
        )
        task = self.state_machine.transition(
            task.task_id,
            TaskState.RUNNING,
            actor="orchestrator",
            reason="runtime_started",
            expected_revision=task.revision,
            trace_id=trace_id,
        )
        context = self._context(task, trace_id)
        try:
            for event in self.runtime.start(context):
                task = self._handle_event(
                    task,
                    event,
                    global_user_id=global_user_id,
                    trace_id=trace_id,
                )
                if task.state != TaskState.RUNNING:
                    break
        except Exception as exc:
            current = self.state_machine.store.get_task(task.task_id)
            if current.state == TaskState.RUNNING:
                return self.state_machine.transition(
                    current.task_id,
                    TaskState.FAILED,
                    actor="orchestrator",
                    reason="runtime_exception",
                    expected_revision=current.revision,
                    trace_id=trace_id,
                    failure=FailureInfo(
                        code="fatal",
                        message=str(exc),
                        retryable=False,
                    ),
                )
            raise

        current = self.state_machine.store.get_task(task.task_id)
        if current.state == TaskState.RUNNING:
            current = self.state_machine.transition(
                current.task_id,
                TaskState.FAILED,
                actor="orchestrator",
                reason="runtime_ended_without_result",
                expected_revision=current.revision,
                trace_id=trace_id,
                failure=FailureInfo(
                    code="fatal",
                    message="runtime event stream ended without a terminal result",
                    retryable=False,
                ),
            )
        return current

    def _handle_event(
        self,
        task: TaskInstance,
        event: RuntimeEvent,
        *,
        global_user_id: str,
        trace_id: str | None,
    ) -> TaskInstance:
        task = self.state_machine.store.get_task(task.task_id)
        if event.type == RuntimeEventType.PROGRESS:
            self._record_progress(task, event.payload, trace_id=trace_id)
            return self.state_machine.store.get_task(task.task_id)
        if event.type == RuntimeEventType.CHECKPOINT:
            checkpoint_ref = str(event.payload["checkpoint_ref"])
            return self.state_machine.revise(
                task.task_id,
                actor="runtime",
                reason="checkpoint_saved",
                expected_revision=task.revision,
                trace_id=trace_id,
                event_type=InteractionEventType.PROGRESS,
                event_payload={
                    "runtime_event": "checkpoint",
                    "checkpoint_ref": checkpoint_ref,
                },
                checkpoint_ref=checkpoint_ref,
            )
        if event.type == RuntimeEventType.CLARIFICATION:
            if self.clarification_service is None:
                raise RuntimeError("runtime requested clarification but no service is configured")
            self.clarification_service.open(
                task_id=task.task_id,
                global_user_id=global_user_id,
                question=str(event.payload["question"]),
                required_fields=tuple(event.payload["required_fields"]),
                actor="runtime",
                expires_at=event.payload.get("expires_at"),
                default_action=str(event.payload.get("default_action") or "cancel"),
            )
            return self.state_machine.store.get_task(task.task_id)
        if event.type == RuntimeEventType.CONFIRMATION:
            if self.confirmation_service is None:
                raise RuntimeError("runtime requested confirmation but no service is configured")
            request, token = self.confirmation_service.open(
                task_id=task.task_id,
                global_user_id=global_user_id,
                action_summary=str(event.payload["action_summary"]),
                action=self._mapping(event.payload["action"], "action"),
                risk_level=RiskLevel(event.payload["risk_level"]),
                scope=self._mapping(event.payload.get("scope") or {}, "scope"),
                actor="runtime",
                expires_at=event.payload.get("expires_at"),
            )
            if self.confirmation_sink:
                self.confirmation_sink(request.request_id, token)
            return self.state_machine.store.get_task(task.task_id)
        if event.type == RuntimeEventType.RESULT:
            return self.state_machine.transition(
                task.task_id,
                TaskState.COMPLETED,
                actor="runtime",
                reason="runtime_completed",
                expected_revision=task.revision,
                trace_id=trace_id,
                result_ref=str(event.payload["result_ref"]),
            )
        if event.type == RuntimeEventType.CANCELLED:
            return self.state_machine.transition(
                task.task_id,
                TaskState.CANCELLED,
                actor="runtime",
                reason=str(event.payload.get("reason") or "runtime_cancelled"),
                expected_revision=task.revision,
                trace_id=trace_id,
            )
        if event.type == RuntimeEventType.ERROR:
            return self.state_machine.transition(
                task.task_id,
                TaskState.FAILED,
                actor="runtime",
                reason="runtime_failed",
                expected_revision=task.revision,
                trace_id=trace_id,
                failure=FailureInfo(
                    code=str(event.payload.get("code") or "fatal"),
                    message=str(event.payload.get("message") or "runtime failed"),
                    retryable=bool(event.payload.get("retryable", False)),
                    details_ref=event.payload.get("details_ref"),
                ),
            )
        raise ValueError(f"unsupported runtime event: {event.type.value}")

    def _record_progress(
        self,
        task: TaskInstance,
        payload: Mapping[str, Any],
        *,
        trace_id: str | None,
    ) -> None:
        progress = ProgressEvent(
            task_id=task.task_id,
            type=ProgressEventType(payload["type"]),
            phase=str(payload["phase"]),
            message=str(payload["message"]),
            occurred_at=payload.get("occurred_at"),
            completed_units=payload.get("completed_units"),
            total_units=payload.get("total_units"),
            metadata=self._mapping(payload.get("metadata") or {}, "metadata"),
        )
        self.state_machine.store.append_event(
            task.task_id,
            event_type=InteractionEventType.PROGRESS,
            actor="runtime",
            trace_id=trace_id,
            payload={
                "type": progress.type.value,
                "phase": progress.phase,
                "message": progress.message,
                "completed_units": progress.completed_units,
                "total_units": progress.total_units,
            },
        )
        if self.progress_sink and self.progress_coalescer.should_emit(progress):
            self.progress_sink(progress)

    @staticmethod
    def _context(task: TaskInstance, trace_id: str | None) -> TaskContext:
        return TaskContext(
            task_id=task.task_id,
            session_id=task.session_id,
            user_goal=task.user_goal,
            task_revision=task.revision,
            trace_id=trace_id,
            checkpoint_ref=task.checkpoint_ref,
        )

    @staticmethod
    def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise TypeError(f"{field_name} must be a mapping")
        return value
