"""The only domain entry point for changing persistent task state."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

from tinyclaw.contracts import TraceContext
from tinyclaw.contracts._common import require_text, utc_now
from tinyclaw.contracts.interaction import InteractionEventType, TaskInstance, TaskState
from tinyclaw.interaction.task_store import (
    SQLiteTaskStore,
    TaskRevisionConflictError,
)
from tinyclaw.observability import TraceRecorder


class InvalidTaskTransitionError(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.QUEUED: frozenset({TaskState.RUNNING, TaskState.CANCELLED}),
    TaskState.RUNNING: frozenset(
        {
            TaskState.WAITING_USER,
            TaskState.WAITING_CONFIRMATION,
            TaskState.WAITING_TOOL,
            TaskState.PAUSED,
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.RECOVERY_REQUIRED,
        }
    ),
    TaskState.WAITING_USER: frozenset({TaskState.RUNNING, TaskState.CANCELLED, TaskState.EXPIRED}),
    TaskState.WAITING_CONFIRMATION: frozenset(
        {TaskState.RUNNING, TaskState.CANCELLED, TaskState.EXPIRED}
    ),
    TaskState.WAITING_TOOL: frozenset(
        {
            TaskState.RUNNING,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.RECOVERY_REQUIRED,
        }
    ),
    TaskState.PAUSED: frozenset({TaskState.RUNNING, TaskState.CANCELLED}),
    TaskState.FAILED: frozenset({TaskState.RUNNING}),
    TaskState.RECOVERY_REQUIRED: frozenset(
        {TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.COMPLETED: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.EXPIRED: frozenset(),
}

_MUTABLE_FIELDS = frozenset(
    {
        "user_goal",
        "runtime_ref",
        "checkpoint_ref",
        "pending_request_ref",
        "cancellation_token",
        "result_ref",
        "failure",
    }
)


class TaskStateMachine:
    def __init__(
        self,
        store: SQLiteTaskStore,
        *,
        trace_recorder: TraceRecorder | None = None,
    ) -> None:
        self.store = store
        self.trace_recorder = trace_recorder

    def create_task(
        self,
        *,
        session_id: str,
        user_goal: str,
        actor: str,
        runtime_ref: str | None = None,
        task_id: str | None = None,
        trace_id: str | None = None,
    ) -> TaskInstance:
        task = TaskInstance(
            task_id=task_id or uuid.uuid4().hex,
            session_id=session_id,
            state=TaskState.QUEUED,
            user_goal=user_goal,
            runtime_ref=runtime_ref,
        )
        self.store.create_task(task, actor=actor, trace_id=trace_id)
        self._trace(
            task,
            event_type="task_created",
            actor=actor,
            trace_id=trace_id,
            payload={"state": task.state.value, "revision": task.revision},
        )
        return task

    def transition(
        self,
        task_id: str,
        target_state: TaskState,
        *,
        actor: str,
        reason: str,
        expected_revision: int | None = None,
        trace_id: str | None = None,
        **changes: Any,
    ) -> TaskInstance:
        require_text(actor, "actor")
        require_text(reason, "reason")
        unknown_fields = set(changes) - _MUTABLE_FIELDS
        if unknown_fields:
            names = ", ".join(sorted(unknown_fields))
            raise ValueError(f"unsupported task update fields: {names}")

        current = self.store.get_task(task_id)
        if expected_revision is not None and current.revision != expected_revision:
            raise TaskRevisionConflictError(
                f"task {task_id!r} expected revision {expected_revision}, found {current.revision}"
            )
        if target_state not in ALLOWED_TRANSITIONS[current.state]:
            raise InvalidTaskTransitionError(
                f"invalid task transition: {current.state.value} -> {target_state.value}"
            )

        updates = dict(changes)
        if target_state == TaskState.RUNNING:
            updates.setdefault("failure", None)
            updates.setdefault("result_ref", None)
        updated = replace(
            current,
            state=target_state,
            revision=current.revision + 1,
            updated_at=utc_now(),
            **updates,
        )
        self.store.compare_and_set(
            updated,
            expected_revision=current.revision,
            previous_state=current.state,
            actor=actor,
            reason=reason,
            trace_id=trace_id,
        )
        self._trace(
            updated,
            event_type="task_state_changed",
            actor=actor,
            trace_id=trace_id,
            payload={
                "from_state": current.state.value,
                "to_state": updated.state.value,
                "reason": reason,
                "revision": updated.revision,
            },
        )
        return updated

    def revise(
        self,
        task_id: str,
        *,
        actor: str,
        reason: str,
        expected_revision: int | None = None,
        trace_id: str | None = None,
        event_type: InteractionEventType = InteractionEventType.CONTROL_APPLIED,
        event_payload: dict[str, object] | None = None,
        **changes: Any,
    ) -> TaskInstance:
        """Create a new task revision without changing its current state."""
        require_text(actor, "actor")
        require_text(reason, "reason")
        unknown_fields = set(changes) - _MUTABLE_FIELDS
        if unknown_fields:
            names = ", ".join(sorted(unknown_fields))
            raise ValueError(f"unsupported task update fields: {names}")
        if not changes:
            raise ValueError("task revision requires at least one changed field")

        current = self.store.get_task(task_id)
        if expected_revision is not None and current.revision != expected_revision:
            raise TaskRevisionConflictError(
                f"task {task_id!r} expected revision {expected_revision}, found {current.revision}"
            )
        updated = replace(
            current,
            revision=current.revision + 1,
            updated_at=utc_now(),
            **changes,
        )
        self.store.compare_and_set(
            updated,
            expected_revision=current.revision,
            previous_state=current.state,
            actor=actor,
            reason=reason,
            trace_id=trace_id,
            event_type=event_type,
            event_payload=event_payload
            if event_payload is not None
            else {
                "state": current.state.value,
                "reason": reason,
                "revision": updated.revision,
                "changed_fields": sorted(changes),
            },
        )
        self._trace(
            updated,
            event_type="task_revised",
            actor=actor,
            trace_id=trace_id,
            payload={
                "state": updated.state.value,
                "reason": reason,
                "revision": updated.revision,
                "changed_fields": sorted(changes),
            },
        )
        return updated

    def _trace(
        self,
        task: TaskInstance,
        *,
        event_type: str,
        actor: str,
        trace_id: str | None,
        payload: dict[str, object],
    ) -> None:
        if self.trace_recorder is None:
            return
        context = (
            TraceContext(trace_id=trace_id, span_id=uuid.uuid4().hex[:16]) if trace_id else None
        )
        try:
            self.trace_recorder.record(
                event_type=event_type,
                producer="task-state-machine",
                producer_version="interaction-v1",
                session_id=task.session_id,
                task_id=task.task_id,
                trace_context=context,
                payload={"actor": actor, **payload},
            )
        except Exception:
            pass

    def recover_interrupted(
        self,
        *,
        actor: str = "system",
        trace_id: str | None = None,
    ) -> list[TaskInstance]:
        """Move non-resumable in-process states into explicit recovery."""
        interrupted = self.store.list_tasks(states=(TaskState.RUNNING, TaskState.WAITING_TOOL))
        recovered: list[TaskInstance] = []
        for task in interrupted:
            try:
                recovered.append(
                    self.transition(
                        task.task_id,
                        TaskState.RECOVERY_REQUIRED,
                        actor=actor,
                        reason="process_restart",
                        expected_revision=task.revision,
                        trace_id=trace_id,
                    )
                )
            except TaskRevisionConflictError:
                continue
        return recovered
