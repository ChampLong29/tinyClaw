"""Compatibility adapter from the current AgentLoop to RuntimePort events."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from typing import Any, Mapping

from tinyclaw.agent.loop import AgentLoop
from tinyclaw.runtime.port import RuntimeEvent, RuntimeEventType, TaskContext

HistoryLoader = Callable[[str], list[dict[str, Any]]]
ResultWriter = Callable[[str, str], str]


class LocalAgentRuntimeAdapter:
    """Runs the existing synchronous loop without exposing Channel concerns."""

    def __init__(
        self,
        agent_loop: AgentLoop,
        *,
        history_loader: HistoryLoader,
        result_writer: ResultWriter,
    ) -> None:
        self.agent_loop = agent_loop
        self.history_loader = history_loader
        self.result_writer = result_writer
        self._lock = threading.RLock()
        self._status: dict[str, dict[str, Any]] = {}
        self._cancel_requested: set[str] = set()
        self._user_inputs: dict[str, Mapping[str, Any]] = {}

    def start(self, context: TaskContext) -> Iterable[RuntimeEvent]:
        with self._lock:
            self._status[context.task_id] = {"state": "running", "phase": "model"}
        yield RuntimeEvent(
            type=RuntimeEventType.PROGRESS,
            payload={
                "type": "phase_started",
                "phase": "model",
                "message": "Agent runtime started",
            },
        )
        history = self.history_loader(context.session_id)
        try:
            text, _updated_messages = self.agent_loop.run_turn(
                history,
                context.user_goal,
            )
        except Exception as exc:
            with self._lock:
                self._status[context.task_id] = {
                    "state": "failed",
                    "error": str(exc),
                }
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
            cancellation_pending = context.task_id in self._cancel_requested
        if cancellation_pending:
            with self._lock:
                self._status[context.task_id] = {"state": "cancellation_pending"}
            yield RuntimeEvent(
                type=RuntimeEventType.CANCELLED,
                payload={
                    "reason": "cancellation completed after the in-flight model call",
                },
            )
            return

        result_ref = self.result_writer(context.task_id, text)
        with self._lock:
            self._status[context.task_id] = {
                "state": "completed",
                "result_ref": result_ref,
            }
        yield RuntimeEvent(
            type=RuntimeEventType.PROGRESS,
            payload={
                "type": "completed",
                "phase": "model",
                "message": "Agent runtime completed",
            },
        )
        yield RuntimeEvent(
            type=RuntimeEventType.RESULT,
            payload={"result_ref": result_ref},
        )

    def resume(self, context: TaskContext, checkpoint_ref: str) -> Iterable[RuntimeEvent]:
        del checkpoint_ref
        return self.start(context)

    def apply_user_input(self, task_id: str, user_input: Mapping[str, Any]) -> None:
        with self._lock:
            self._user_inputs[task_id] = dict(user_input)

    def request_cancel(self, task_id: str) -> bool:
        with self._lock:
            self._cancel_requested.add(task_id)
            running = self._status.get(task_id, {}).get("state") == "running"
        return running

    def snapshot(self, task_id: str) -> str | None:
        with self._lock:
            value = self._status.get(task_id, {}).get("result_ref")
        return str(value) if value else None

    def get_status(self, task_id: str) -> Mapping[str, Any]:
        with self._lock:
            return dict(self._status.get(task_id, {"state": "unknown"}))
