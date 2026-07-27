from datetime import timedelta
from pathlib import Path

from tinyclaw.contracts import TaskState
from tinyclaw.contracts._common import utc_now
from tinyclaw.interaction.clarification import ClarificationService
from tinyclaw.interaction.orchestrator import InteractionOrchestrator
from tinyclaw.interaction.progress import ProgressCoalescer
from tinyclaw.interaction.request_store import (
    ClarificationRequest,
    SQLiteRequestStore,
)
from tinyclaw.interaction.state_machine import TaskStateMachine
from tinyclaw.interaction.task_store import SQLiteTaskStore
from tinyclaw.runtime.port import RuntimeEvent, RuntimeEventType


class FakeRuntime:
    def __init__(self, events):
        self.events = events
        self.context = None

    def start(self, context):
        self.context = context
        return iter(self.events)


def test_orchestrator_persists_progress_and_terminal_result(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "interaction.db")
    emitted = []
    now = utc_now()
    runtime = FakeRuntime(
        [
            RuntimeEvent(
                type=RuntimeEventType.PROGRESS,
                payload={
                    "type": "phase_started",
                    "phase": "collect",
                    "message": "Collecting",
                    "occurred_at": now,
                },
            ),
            RuntimeEvent(
                type=RuntimeEventType.PROGRESS,
                payload={
                    "type": "step_progress",
                    "phase": "collect",
                    "message": "Noisy update",
                    "occurred_at": now + timedelta(seconds=1),
                },
            ),
            RuntimeEvent(
                type=RuntimeEventType.CHECKPOINT,
                payload={"checkpoint_ref": "checkpoint:1"},
            ),
            RuntimeEvent(
                type=RuntimeEventType.RESULT,
                payload={"result_ref": "artifact:result"},
            ),
        ]
    )
    orchestrator = InteractionOrchestrator(
        state_machine=TaskStateMachine(store),
        runtime=runtime,
        progress_coalescer=ProgressCoalescer(
            minimum_interval=timedelta(seconds=10)
        ),
        progress_sink=emitted.append,
    )
    try:
        task = orchestrator.start(
            session_id="session-1",
            global_user_id="user-1",
            user_goal="build report",
            runtime_ref="fake.v1",
            task_id="task-1",
            trace_id="trace-1",
        )

        assert task.state == TaskState.COMPLETED
        assert task.result_ref == "artifact:result"
        assert task.checkpoint_ref == "checkpoint:1"
        assert runtime.context.task_id == "task-1"
        assert [progress.message for progress in emitted] == ["Collecting"]
        event_types = [event.event_type.value for event in store.list_events("task-1")]
        assert event_types.count("progress") == 3
    finally:
        store.close()


def test_orchestrator_pauses_for_runtime_clarification(tmp_path: Path):
    db_path = tmp_path / "interaction.db"
    task_store = SQLiteTaskStore(db_path)
    request_store = SQLiteRequestStore(db_path)
    machine = TaskStateMachine(task_store)
    runtime = FakeRuntime(
        [
            RuntimeEvent(
                type=RuntimeEventType.CLARIFICATION,
                payload={
                    "question": "Which environment?",
                    "required_fields": ["environment"],
                },
            )
        ]
    )
    orchestrator = InteractionOrchestrator(
        state_machine=machine,
        runtime=runtime,
        clarification_service=ClarificationService(request_store, machine),
    )
    try:
        task = orchestrator.start(
            session_id="session-1",
            global_user_id="user-1",
            user_goal="deploy",
            runtime_ref="fake.v1",
            task_id="task-1",
        )

        assert task.state == TaskState.WAITING_USER
        assert task.pending_request_ref
        request = request_store.get(task.pending_request_ref)
        assert isinstance(request, ClarificationRequest)
    finally:
        request_store.close()
        task_store.close()


def test_orchestrator_converts_runtime_exception_to_failure(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "interaction.db")

    class BrokenRuntime:
        def start(self, _context):
            raise RuntimeError("model unavailable")

    try:
        task = InteractionOrchestrator(
            state_machine=TaskStateMachine(store),
            runtime=BrokenRuntime(),
        ).start(
            session_id="session-1",
            global_user_id="user-1",
            user_goal="do work",
            runtime_ref="broken.v1",
            task_id="task-1",
        )
        assert task.state == TaskState.FAILED
        assert task.failure is not None
        assert task.failure.message == "model unavailable"
    finally:
        store.close()
