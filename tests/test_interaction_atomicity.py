from dataclasses import replace
from pathlib import Path

import pytest

from tinyclaw.contracts import TaskState
from tinyclaw.contracts._common import utc_now
from tinyclaw.interaction import SQLiteTaskStore, TaskStateMachine


def test_event_failure_rolls_back_task_state(tmp_path: Path, monkeypatch):
    store = SQLiteTaskStore(tmp_path / "interaction.db")
    try:
        machine = TaskStateMachine(store)
        current = machine.create_task(
            session_id="session-1",
            user_goal="atomic update",
            actor="user-1",
            task_id="task-1",
        )
        updated = replace(
            current,
            state=TaskState.RUNNING,
            revision=2,
            updated_at=utc_now(),
        )

        def fail_event_write(_event):
            raise RuntimeError("injected event failure")

        monkeypatch.setattr(store, "_insert_event", fail_event_write)
        with pytest.raises(RuntimeError, match="injected"):
            store.compare_and_set(
                updated,
                expected_revision=1,
                previous_state=TaskState.QUEUED,
                actor="gateway",
                reason="runtime_started",
            )

        persisted = store.get_task("task-1")
        assert persisted.state == TaskState.QUEUED
        assert persisted.revision == 1
    finally:
        store.close()
