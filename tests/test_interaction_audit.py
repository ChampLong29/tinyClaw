from pathlib import Path

import pytest

from tinyclaw.contracts import TaskState
from tinyclaw.interaction import SQLiteTaskStore, TaskStateMachine


def test_state_transition_requires_an_audit_reason(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "interaction.db")
    try:
        machine = TaskStateMachine(store)
        task = machine.create_task(
            session_id="session-1",
            user_goal="audited work",
            actor="user-1",
            task_id="task-1",
        )
        with pytest.raises(ValueError, match="reason"):
            machine.transition(
                task.task_id,
                TaskState.RUNNING,
                actor="gateway",
                reason="",
            )
        assert store.get_task(task.task_id).revision == 1
    finally:
        store.close()
