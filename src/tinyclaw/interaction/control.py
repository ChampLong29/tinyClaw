"""Authorized control commands for persistent tasks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping

from tinyclaw.contracts._common import require_text, utc_now
from tinyclaw.contracts.interaction import CancellationToken, TaskInstance, TaskState
from tinyclaw.interaction.state_machine import TaskStateMachine


class ControlAction(str, Enum):
    CANCEL = "cancel"
    PAUSE = "pause"
    RESUME = "resume"
    MODIFY = "modify"
    STATUS = "status"


@dataclass(frozen=True, kw_only=True)
class ControlPrincipal:
    session_id: str
    global_user_id: str

    def __post_init__(self) -> None:
        require_text(self.session_id, "session_id")
        require_text(self.global_user_id, "global_user_id")


@dataclass(frozen=True, kw_only=True)
class ControlCommand:
    action: ControlAction
    task_id: str
    expected_revision: int | None = None
    patch: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        require_text(self.task_id, "task_id")
        if self.expected_revision is not None and self.expected_revision < 1:
            raise ValueError("expected_revision must be at least 1")
        if self.action == ControlAction.MODIFY and not self.patch:
            raise ValueError("modify command requires patch")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ControlCommand":
        """Parse only an explicit control envelope, never ordinary chat text."""
        if payload.get("type") != "control":
            raise ValueError("payload is not an explicit control command")
        return cls(
            action=ControlAction(payload["action"]),
            task_id=str(payload["task_id"]),
            expected_revision=payload.get("expected_revision"),
            patch=payload.get("patch"),
        )


class ControlPermissionError(PermissionError):
    pass


class ControlCommandHandler:
    def __init__(
        self,
        state_machine: TaskStateMachine,
        *,
        session_owner_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        self.state_machine = state_machine
        self.session_owner_resolver = session_owner_resolver

    def execute(
        self,
        command: ControlCommand,
        principal: ControlPrincipal,
        *,
        cancellation_pending: bool = False,
    ) -> TaskInstance:
        task = self.state_machine.store.get_task(command.task_id)
        self._authorize(task, principal)
        if command.expected_revision is not None and task.revision != command.expected_revision:
            from tinyclaw.interaction.task_store import TaskRevisionConflictError

            raise TaskRevisionConflictError(
                f"task {task.task_id!r} expected revision {command.expected_revision}, "
                f"found {task.revision}"
            )
        if command.action == ControlAction.STATUS:
            return task
        if command.action == ControlAction.CANCEL:
            token = CancellationToken(
                requested_at=utc_now(),
                requested_by=principal.global_user_id,
                reason="user_cancelled",
                pending=cancellation_pending,
            )
            if cancellation_pending:
                if task.state not in (TaskState.RUNNING, TaskState.WAITING_TOOL):
                    raise ValueError("cancellation_pending requires an executing task")
                return self.state_machine.revise(
                    task.task_id,
                    actor=principal.global_user_id,
                    reason="cancellation_pending",
                    expected_revision=task.revision,
                    cancellation_token=token,
                )
            return self.state_machine.transition(
                task.task_id,
                TaskState.CANCELLED,
                actor=principal.global_user_id,
                reason="user_cancelled",
                expected_revision=task.revision,
                cancellation_token=token,
            )
        if command.action == ControlAction.PAUSE:
            return self.state_machine.transition(
                task.task_id,
                TaskState.PAUSED,
                actor=principal.global_user_id,
                reason="user_paused",
                expected_revision=task.revision,
            )
        if command.action == ControlAction.RESUME:
            return self.state_machine.transition(
                task.task_id,
                TaskState.RUNNING,
                actor=principal.global_user_id,
                reason="user_resumed",
                expected_revision=task.revision,
            )

        if task.state in (TaskState.COMPLETED, TaskState.CANCELLED, TaskState.EXPIRED):
            raise ValueError("terminal task cannot be modified")
        patch = dict(command.patch or {})
        unsupported = set(patch) - {"user_goal", "checkpoint_ref"}
        if unsupported:
            raise ValueError(f"unsupported modify fields: {', '.join(sorted(unsupported))}")
        return self.state_machine.revise(
            task.task_id,
            actor=principal.global_user_id,
            reason="user_modified",
            expected_revision=task.revision,
            **patch,
        )

    def _authorize(self, task: TaskInstance, principal: ControlPrincipal) -> None:
        if task.session_id != principal.session_id:
            raise ControlPermissionError("task does not belong to the resolved session")
        if self.session_owner_resolver is not None:
            owner = self.session_owner_resolver(task.session_id)
            if owner != principal.global_user_id:
                raise ControlPermissionError("task does not belong to the resolved identity")
