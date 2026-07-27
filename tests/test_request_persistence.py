from pathlib import Path

import pytest

from tinyclaw.contracts import TaskState
from tinyclaw.interaction.clarification import ClarificationService
from tinyclaw.interaction.control import (
    ControlAction,
    ControlCommand,
    ControlCommandHandler,
    ControlPrincipal,
)
from tinyclaw.interaction.request_store import (
    ClarificationRequest,
    RequestState,
    SQLiteRequestStore,
)
from tinyclaw.interaction.state_machine import TaskStateMachine
from tinyclaw.interaction.task_store import SQLiteTaskStore


def test_open_request_survives_store_restart(tmp_path: Path):
    db_path = tmp_path / "interaction.db"
    task_store = SQLiteTaskStore(db_path)
    request_store = SQLiteRequestStore(db_path)
    machine = TaskStateMachine(task_store)
    task = machine.create_task(
        session_id="session-1",
        user_goal="persistent request",
        actor="user-1",
        task_id="task-1",
    )
    machine.transition(
        task.task_id,
        TaskState.RUNNING,
        actor="runtime",
        reason="started",
    )
    request = ClarificationService(request_store, machine).open(
        task_id=task.task_id,
        global_user_id="user-1",
        question="Which target?",
        required_fields=("target",),
        actor="runtime",
    )
    request_store.close()

    reopened = SQLiteRequestStore(db_path)
    try:
        loaded = reopened.get(request.request_id)
        assert isinstance(loaded, ClarificationRequest)
        assert loaded.state == RequestState.OPEN
        assert loaded.required_fields == ("target",)
    finally:
        reopened.close()
        task_store.close()


def test_terminal_task_cannot_be_modified(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "interaction.db")
    try:
        machine = TaskStateMachine(store)
        task = machine.create_task(
            session_id="session-1",
            user_goal="finish",
            actor="user-1",
            task_id="task-1",
        )
        running = machine.transition(
            task.task_id,
            TaskState.RUNNING,
            actor="runtime",
            reason="started",
        )
        machine.transition(
            running.task_id,
            TaskState.COMPLETED,
            actor="runtime",
            reason="completed",
            result_ref="artifact:result",
        )
        handler = ControlCommandHandler(
            machine,
            session_owner_resolver=lambda _session_id: "user-1",
        )
        with pytest.raises(ValueError, match="terminal"):
            handler.execute(
                ControlCommand(
                    action=ControlAction.MODIFY,
                    task_id=task.task_id,
                    patch={"user_goal": "change history"},
                ),
                ControlPrincipal(session_id="session-1", global_user_id="user-1"),
            )
    finally:
        store.close()
