"""Production bridge from the current synchronous agent turn to Interaction State."""

from __future__ import annotations

import hashlib
import json
import shlex
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Mapping

from tinyclaw.contracts.interaction import TaskInstance, TaskState
from tinyclaw.interaction.clarification import ClarificationService
from tinyclaw.interaction.confirmation import ConfirmationService
from tinyclaw.interaction.control import (
    ControlAction,
    ControlCommand,
    ControlCommandHandler,
    ControlPrincipal,
)
from tinyclaw.interaction.orchestrator import InteractionOrchestrator
from tinyclaw.interaction.progress import ProgressEvent
from tinyclaw.interaction.request_store import ConfirmationDecision
from tinyclaw.interaction.state_machine import TaskStateMachine
from tinyclaw.observability.artifacts import ArtifactStore
from tinyclaw.pause_signals import (
    ClarificationRequiredSignal,
    ConfirmationRequiredSignal,
)
from tinyclaw.runtime.port import RuntimeEvent, RuntimeEventType, TaskContext

TaskExecutor = Callable[[TaskContext], str]
ProgressSink = Callable[[ProgressEvent], None]
ClarificationSink = Callable[[str], None]
ConfirmationSink = Callable[[str, str], None]


@dataclass(frozen=True, kw_only=True)
class TaskExecutionOutcome:
    task: TaskInstance
    text: str | None = None


@dataclass(frozen=True, kw_only=True)
class ParsedTaskCommand:
    action: ControlAction
    task_id: str | None = None
    patch: Mapping[str, Any] | None = None


@dataclass(frozen=True, kw_only=True)
class ParsedConfirmationCommand:
    decision: ConfirmationDecision
    request_id: str
    token: str


@dataclass(frozen=True, kw_only=True)
class ParsedClarificationCommand:
    request_id: str
    answer: Mapping[str, Any]


def parse_task_command(text: str) -> ParsedTaskCommand | None:
    """Parse only the explicit ``/task`` command namespace."""
    stripped = text.strip()
    if not stripped.lower().startswith("/task"):
        return None
    parts = stripped.split(maxsplit=3)
    if parts[0].lower() != "/task":
        return None
    if len(parts) < 2:
        raise ValueError("用法: /task <status|cancel|pause|resume|modify> [task_id] [新目标]")
    try:
        action = ControlAction(parts[1].lower())
    except ValueError as exc:
        raise ValueError("任务操作仅支持 status、cancel、pause、resume、modify") from exc

    if action == ControlAction.MODIFY:
        if len(parts) < 4:
            raise ValueError("用法: /task modify <task_id> <新目标>")
        return ParsedTaskCommand(
            action=action,
            task_id=parts[2],
            patch={"user_goal": parts[3]},
        )
    return ParsedTaskCommand(
        action=action,
        task_id=parts[2] if len(parts) >= 3 else None,
    )


def parse_confirmation_command(text: str) -> ParsedConfirmationCommand | None:
    stripped = text.strip()
    if not stripped.lower().startswith("/confirm"):
        return None
    parts = stripped.split()
    if parts[0].lower() != "/confirm":
        return None
    if len(parts) != 4 or parts[1].lower() not in {"approve", "deny"}:
        raise ValueError("用法: /confirm <approve|deny> <request_id> <token>")
    decision = (
        ConfirmationDecision.APPROVE_ONCE
        if parts[1].lower() == "approve"
        else ConfirmationDecision.DENY
    )
    return ParsedConfirmationCommand(
        decision=decision,
        request_id=parts[2],
        token=parts[3],
    )


def parse_clarification_command(text: str) -> ParsedClarificationCommand | None:
    stripped = text.strip()
    if not stripped.lower().startswith("/clarify"):
        return None
    parts = stripped.split(maxsplit=2)
    if parts[0].lower() != "/clarify":
        return None
    if len(parts) != 3:
        raise ValueError("用法: /clarify <request_id> <JSON 或 field=value...>")
    payload = parts[2].strip()
    if payload.startswith("{"):
        answer = json.loads(payload)
        if not isinstance(answer, dict):
            raise ValueError("澄清答复 JSON 必须是对象")
    else:
        answer = {}
        for item in shlex.split(payload):
            if "=" not in item:
                raise ValueError("澄清字段必须使用 field=value 格式")
            key, value = item.split("=", 1)
            if not key:
                raise ValueError("澄清字段名不能为空")
            answer[key] = value
    if not answer:
        raise ValueError("澄清答复不能为空")
    return ParsedClarificationCommand(request_id=parts[1], answer=answer)


def session_lane_name(session_id: str) -> str:
    """Build a bounded lane name without exposing the raw session identifier."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return f"session:{digest}"


class CallableRuntimeAdapter:
    """Expose a synchronous production turn callable through ``RuntimePort``."""

    def __init__(self, executor: TaskExecutor, artifact_store: ArtifactStore) -> None:
        self.executor = executor
        self.artifact_store = artifact_store
        self._lock = threading.RLock()
        self._status: dict[str, dict[str, Any]] = {}
        self._cancel_requested: set[str] = set()
        self._pause_requested: set[str] = set()
        self._results: dict[str, str] = {}

    def start(self, context: TaskContext) -> Iterable[RuntimeEvent]:
        return self._run(context)

    def resume(self, context: TaskContext, checkpoint_ref: str) -> Iterable[RuntimeEvent]:
        del checkpoint_ref
        return self._run(context)

    def _run(self, context: TaskContext) -> Iterable[RuntimeEvent]:
        with self._lock:
            self._status[context.task_id] = {"state": "running", "phase": "model"}
        yield RuntimeEvent(
            type=RuntimeEventType.PROGRESS,
            payload={
                "type": "phase_started",
                "phase": "model",
                "message": "任务开始执行",
            },
        )
        try:
            text = self.executor(context)
        except ClarificationRequiredSignal as signal:
            with self._lock:
                self._status[context.task_id] = {"state": "waiting_user"}
            yield RuntimeEvent(
                type=RuntimeEventType.CLARIFICATION,
                payload={
                    "question": signal.question,
                    "required_fields": signal.required_fields,
                    "default_action": signal.default_action,
                },
            )
            return
        except ConfirmationRequiredSignal as signal:
            with self._lock:
                self._status[context.task_id] = {"state": "waiting_confirmation"}
            risk_level = getattr(signal.risk_level, "value", str(signal.risk_level))
            yield RuntimeEvent(
                type=RuntimeEventType.CONFIRMATION,
                payload={
                    "action_summary": signal.action_summary,
                    "action": signal.action,
                    "risk_level": risk_level,
                    "scope": signal.scope,
                },
            )
            return
        except Exception as exc:
            with self._lock:
                self._status[context.task_id] = {"state": "failed", "error": str(exc)}
            yield RuntimeEvent(
                type=RuntimeEventType.ERROR,
                payload={
                    "code": "fatal",
                    "message": str(exc),
                    "retryable": False,
                },
            )
            return

        with self._lock:
            cancelled = context.task_id in self._cancel_requested
            paused = context.task_id in self._pause_requested
        if cancelled:
            with self._lock:
                self._status[context.task_id] = {"state": "cancelled"}
            yield RuntimeEvent(
                type=RuntimeEventType.CANCELLED,
                payload={"reason": "cancelled after the in-flight model call"},
            )
            return
        if paused:
            with self._lock:
                self._status[context.task_id] = {"state": "paused"}
            return

        result_ref = self.artifact_store.put_text(text, redact=False).artifact_ref
        with self._lock:
            self._results[context.task_id] = text
            self._status[context.task_id] = {
                "state": "completed",
                "result_ref": result_ref,
            }
        yield RuntimeEvent(
            type=RuntimeEventType.PROGRESS,
            payload={
                "type": "completed",
                "phase": "model",
                "message": "任务执行完成",
            },
        )
        yield RuntimeEvent(
            type=RuntimeEventType.RESULT,
            payload={"result_ref": result_ref},
        )

    def apply_user_input(self, task_id: str, user_input: Mapping[str, Any]) -> None:
        del task_id, user_input

    def request_cancel(self, task_id: str) -> bool:
        with self._lock:
            self._cancel_requested.add(task_id)
            return self._status.get(task_id, {}).get("state") == "running"

    def request_pause(self, task_id: str) -> bool:
        with self._lock:
            self._pause_requested.add(task_id)
            return self._status.get(task_id, {}).get("state") == "running"

    def snapshot(self, task_id: str) -> str | None:
        with self._lock:
            value = self._status.get(task_id, {}).get("result_ref")
        return str(value) if value else None

    def get_status(self, task_id: str) -> Mapping[str, Any]:
        with self._lock:
            return dict(self._status.get(task_id, {"state": "queued"}))

    def result(self, task_id: str) -> str | None:
        with self._lock:
            return self._results.get(task_id)


class ProductionInteractionService:
    """Persist and control task lifecycle around the current production turn."""

    def __init__(
        self,
        *,
        state_machine: TaskStateMachine,
        artifact_store: ArtifactStore,
        session_owner_resolver: Callable[[str], str | None] | None = None,
        progress_sink: ProgressSink | None = None,
        clarification_service: ClarificationService | None = None,
        confirmation_service: ConfirmationService | None = None,
        clarification_sink: ClarificationSink | None = None,
        confirmation_sink: ConfirmationSink | None = None,
    ) -> None:
        self.state_machine = state_machine
        self.artifact_store = artifact_store
        self.control_handler = ControlCommandHandler(
            state_machine,
            session_owner_resolver=session_owner_resolver,
        )
        self.progress_sink = progress_sink
        self.clarification_service = clarification_service
        self.confirmation_service = confirmation_service
        self.clarification_sink = clarification_sink
        self.confirmation_sink = confirmation_sink
        self._lock = threading.RLock()
        self._runtimes: dict[str, CallableRuntimeAdapter] = {}

    def submit(
        self,
        *,
        session_id: str,
        global_user_id: str,
        user_goal: str,
        runtime_ref: str,
        task_id: str | None = None,
        trace_id: str | None = None,
    ) -> TaskInstance:
        resolved_task_id = task_id or uuid.uuid4().hex
        return self.state_machine.create_task(
            session_id=session_id,
            user_goal=user_goal,
            actor=global_user_id,
            runtime_ref=runtime_ref,
            task_id=resolved_task_id,
            trace_id=trace_id or resolved_task_id,
        )

    def run(
        self,
        task_id: str,
        *,
        global_user_id: str,
        executor: TaskExecutor,
        trace_id: str | None = None,
        resume: bool = False,
    ) -> TaskExecutionOutcome:
        task = self.state_machine.store.get_task(task_id)
        if task.state in (TaskState.CANCELLED, TaskState.COMPLETED, TaskState.EXPIRED):
            return TaskExecutionOutcome(task=task)

        runtime = CallableRuntimeAdapter(executor, self.artifact_store)
        if trace_id is None:
            events = self.state_machine.store.list_events(task_id)
            trace_id = (events[0].trace_id if events else None) or task_id
        with self._lock:
            self._runtimes[task_id] = runtime
        orchestrator = InteractionOrchestrator(
            state_machine=self.state_machine,
            runtime=runtime,
            progress_sink=self.progress_sink,
            clarification_service=self.clarification_service,
            confirmation_service=self.confirmation_service,
            clarification_sink=self.clarification_sink,
            confirmation_sink=self.confirmation_sink,
        )
        try:
            if resume:
                task = orchestrator.resume(
                    task_id,
                    global_user_id=global_user_id,
                    trace_id=trace_id,
                )
            else:
                task = orchestrator.run_existing(
                    task_id,
                    global_user_id=global_user_id,
                    trace_id=trace_id,
                )
            return TaskExecutionOutcome(task=task, text=runtime.result(task_id))
        finally:
            with self._lock:
                self._runtimes.pop(task_id, None)

    def control(
        self,
        parsed: ParsedTaskCommand,
        principal: ControlPrincipal,
    ) -> TaskInstance:
        task = self.resolve_task(parsed, principal)
        task_id = task.task_id
        runtime = self._runtime(task_id)

        if parsed.action == ControlAction.MODIFY and task.state == TaskState.RUNNING:
            if runtime:
                runtime.request_pause(task_id)
            task = self.control_handler.execute(
                ControlCommand(action=ControlAction.PAUSE, task_id=task_id),
                principal,
            )
        elif parsed.action == ControlAction.PAUSE and runtime:
            runtime.request_pause(task_id)

        if parsed.action == ControlAction.CANCEL:
            cancellation_pending = runtime.request_cancel(task_id) if runtime else False
            return self.control_handler.execute(
                ControlCommand(action=parsed.action, task_id=task_id),
                principal,
                cancellation_pending=cancellation_pending,
            )

        return self.control_handler.execute(
            ControlCommand(
                action=parsed.action,
                task_id=task_id,
                patch=parsed.patch,
            ),
            principal,
        )

    def resolve_task(
        self,
        parsed: ParsedTaskCommand,
        principal: ControlPrincipal,
    ) -> TaskInstance:
        task_id = parsed.task_id or self._latest_task_id(principal.session_id)
        task = self.state_machine.store.get_task(task_id)
        if task.session_id != principal.session_id:
            raise PermissionError("task does not belong to the resolved session")
        return task

    def latest_task(self, session_id: str) -> TaskInstance | None:
        tasks = self.state_machine.store.list_tasks(session_id=session_id)
        return tasks[-1] if tasks else None

    def recover_interrupted(self) -> list[TaskInstance]:
        return self.state_machine.recover_interrupted()

    def _latest_task_id(self, session_id: str) -> str:
        task = self.latest_task(session_id)
        if task is None:
            raise ValueError("当前会话还没有任务")
        return task.task_id

    def _runtime(self, task_id: str) -> CallableRuntimeAdapter | None:
        with self._lock:
            return self._runtimes.get(task_id)
