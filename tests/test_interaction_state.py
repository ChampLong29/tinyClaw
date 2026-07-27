from pathlib import Path

import pytest

from tinyclaw.contracts import FailureInfo, TaskState
from tinyclaw.interaction import (
    ActiveTaskExistsError,
    InvalidTaskTransitionError,
    SQLiteTaskStore,
    TaskRevisionConflictError,
    TaskStateMachine,
)


@pytest.fixture
def store(tmp_path: Path):
    task_store = SQLiteTaskStore(tmp_path / "interaction.db")
    try:
        yield task_store
    finally:
        task_store.close()


def test_transition_and_event_are_persisted_atomically(store: SQLiteTaskStore):
    machine = TaskStateMachine(store)
    created = machine.create_task(
        session_id="session-1",
        user_goal="generate report",
        actor="user-1",
        task_id="task-1",
        trace_id="trace-1",
    )

    running = machine.transition(
        created.task_id,
        TaskState.RUNNING,
        actor="gateway",
        reason="runtime_started",
        expected_revision=1,
        checkpoint_ref="checkpoint:1",
    )

    assert running.revision == 2
    assert store.get_task("task-1") == running
    events = store.list_events("task-1")
    assert [event.sequence for event in events] == [0, 1]
    assert events[1].payload == {
        "from_state": "queued",
        "to_state": "running",
        "reason": "runtime_started",
        "revision": 2,
    }


def test_invalid_transition_does_not_change_task_or_append_event(store: SQLiteTaskStore):
    machine = TaskStateMachine(store)
    task = machine.create_task(
        session_id="session-1",
        user_goal="generate report",
        actor="user-1",
        task_id="task-1",
    )

    with pytest.raises(InvalidTaskTransitionError):
        machine.transition(
            task.task_id,
            TaskState.COMPLETED,
            actor="runtime",
            reason="skipped_running",
            result_ref="artifact:result",
        )

    assert store.get_task(task.task_id).revision == 1
    assert len(store.list_events(task.task_id)) == 1


def test_stale_revision_is_rejected(store: SQLiteTaskStore):
    machine = TaskStateMachine(store)
    task = machine.create_task(
        session_id="session-1",
        user_goal="generate report",
        actor="user-1",
        task_id="task-1",
    )
    machine.transition(
        task.task_id,
        TaskState.RUNNING,
        actor="runtime",
        reason="started",
        expected_revision=1,
    )

    with pytest.raises(TaskRevisionConflictError):
        machine.transition(
            task.task_id,
            TaskState.PAUSED,
            actor="user-1",
            reason="pause",
            expected_revision=1,
        )

    assert store.get_task(task.task_id).state == TaskState.RUNNING
    assert len(store.list_events(task.task_id)) == 2


def test_only_one_active_task_is_allowed_per_session(store: SQLiteTaskStore):
    machine = TaskStateMachine(store)
    machine.create_task(
        session_id="session-1",
        user_goal="first",
        actor="user-1",
        task_id="task-1",
    )

    with pytest.raises(ActiveTaskExistsError):
        machine.create_task(
            session_id="session-1",
            user_goal="second",
            actor="user-1",
            task_id="task-2",
        )

    assert store.list_tasks(session_id="session-1")[0].task_id == "task-1"


def test_store_reopens_and_recovers_interrupted_tasks(tmp_path: Path):
    db_path = tmp_path / "interaction.db"
    first_store = SQLiteTaskStore(db_path)
    first_machine = TaskStateMachine(first_store)
    task = first_machine.create_task(
        session_id="session-1",
        user_goal="long task",
        actor="user-1",
        task_id="task-1",
    )
    first_machine.transition(
        task.task_id,
        TaskState.RUNNING,
        actor="runtime",
        reason="started",
    )
    first_store.close()

    second_store = SQLiteTaskStore(db_path)
    try:
        recovered = TaskStateMachine(second_store).recover_interrupted()
        assert [task.state for task in recovered] == [TaskState.RECOVERY_REQUIRED]
        persisted = second_store.get_task("task-1")
        assert persisted.state == TaskState.RECOVERY_REQUIRED
        assert persisted.revision == 3
        assert second_store.list_events("task-1")[-1].payload["reason"] == "process_restart"
    finally:
        second_store.close()


def test_failed_task_requires_structured_failure(store: SQLiteTaskStore):
    machine = TaskStateMachine(store)
    task = machine.create_task(
        session_id="session-1",
        user_goal="long task",
        actor="user-1",
        task_id="task-1",
    )
    running = machine.transition(
        task.task_id,
        TaskState.RUNNING,
        actor="runtime",
        reason="started",
    )

    with pytest.raises(ValueError, match="failure"):
        machine.transition(
            running.task_id,
            TaskState.FAILED,
            actor="runtime",
            reason="tool_failed",
        )

    failed = machine.transition(
        running.task_id,
        TaskState.FAILED,
        actor="runtime",
        reason="tool_failed",
        failure=FailureInfo(
            code="tool_unavailable",
            message="renderer is offline",
            retryable=True,
        ),
    )
    assert failed.failure is not None
    assert failed.failure.code == "tool_unavailable"
