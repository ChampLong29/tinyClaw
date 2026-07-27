from pathlib import Path

import pytest

from tinyclaw.contracts import TaskState
from tinyclaw.interaction.control import (
    ControlAction,
    ControlCommand,
    ControlCommandHandler,
    ControlPermissionError,
    ControlPrincipal,
)
from tinyclaw.interaction.state_machine import TaskStateMachine
from tinyclaw.interaction.task_store import SQLiteTaskStore


@pytest.fixture
def control_setup(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "interaction.db")
    machine = TaskStateMachine(store)
    task = machine.create_task(
        session_id="session-1",
        user_goal="original goal",
        actor="user-1",
        task_id="task-1",
    )
    running = machine.transition(
        task.task_id,
        TaskState.RUNNING,
        actor="runtime",
        reason="started",
    )
    handler = ControlCommandHandler(
        machine,
        session_owner_resolver=lambda session_id: {
            "session-1": "global-user-1"
        }.get(session_id),
    )
    try:
        yield store, handler, running
    finally:
        store.close()


def test_control_parser_rejects_ordinary_chat():
    with pytest.raises(ValueError, match="explicit control"):
        ControlCommand.from_payload({"text": "yes, cancel it"})


def test_control_command_cannot_target_another_session(control_setup):
    _store, handler, _task = control_setup
    with pytest.raises(ControlPermissionError):
        handler.execute(
            ControlCommand(action=ControlAction.CANCEL, task_id="task-1"),
            ControlPrincipal(
                session_id="session-other",
                global_user_id="global-user-1",
            ),
        )


def test_control_command_cannot_target_another_identity(control_setup):
    _store, handler, _task = control_setup
    with pytest.raises(ControlPermissionError):
        handler.execute(
            ControlCommand(action=ControlAction.STATUS, task_id="task-1"),
            ControlPrincipal(
                session_id="session-1",
                global_user_id="global-user-other",
            ),
        )


def test_modify_creates_revision_without_changing_state(control_setup):
    store, handler, task = control_setup
    modified = handler.execute(
        ControlCommand(
            action=ControlAction.MODIFY,
            task_id=task.task_id,
            expected_revision=task.revision,
            patch={"user_goal": "revised goal"},
        ),
        ControlPrincipal(
            session_id="session-1",
            global_user_id="global-user-1",
        ),
    )

    assert modified.state == TaskState.RUNNING
    assert modified.revision == task.revision + 1
    assert modified.user_goal == "revised goal"
    assert store.list_events(task.task_id)[-1].event_type.value == "control_applied"


def test_non_interruptible_cancel_records_pending_token(control_setup):
    _store, handler, task = control_setup
    pending = handler.execute(
        ControlCommand(action=ControlAction.CANCEL, task_id=task.task_id),
        ControlPrincipal(
            session_id="session-1",
            global_user_id="global-user-1",
        ),
        cancellation_pending=True,
    )

    assert pending.state == TaskState.RUNNING
    assert pending.cancellation_token.pending is True
    assert pending.cancellation_token.requested_by == "global-user-1"
