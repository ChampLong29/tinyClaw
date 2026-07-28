from __future__ import annotations

import threading
from pathlib import Path

from tinyclaw.concurrency import CommandQueue
from tinyclaw.contracts.interaction import TaskState
from tinyclaw.interaction.control import ControlAction, ControlPrincipal
from tinyclaw.interaction.production import (
    ParsedTaskCommand,
    ProductionInteractionService,
    parse_task_command,
    session_lane_name,
)
from tinyclaw.interaction.state_machine import TaskStateMachine
from tinyclaw.interaction.task_store import SQLiteTaskStore
from tinyclaw.observability.artifacts import ArtifactStore


def _service(tmp_path: Path) -> ProductionInteractionService:
    state_machine = TaskStateMachine(SQLiteTaskStore(tmp_path / "interaction.db"))
    return ProductionInteractionService(
        state_machine=state_machine,
        artifact_store=ArtifactStore(tmp_path / "results"),
    )


def test_explicit_task_command_parser_and_lane_name():
    assert parse_task_command("普通聊天") is None
    assert parse_task_command("/task status") == ParsedTaskCommand(action=ControlAction.STATUS)
    assert parse_task_command("/task cancel task-1") == ParsedTaskCommand(
        action=ControlAction.CANCEL,
        task_id="task-1",
    )
    assert parse_task_command("/task modify task-1 更新后的目标") == ParsedTaskCommand(
        action=ControlAction.MODIFY,
        task_id="task-1",
        patch={"user_goal": "更新后的目标"},
    )
    assert session_lane_name("session-1") == session_lane_name("session-1")
    assert session_lane_name("session-1") != session_lane_name("session-2")
    assert "session-1" not in session_lane_name("session-1")


def test_submitted_task_runs_through_orchestrator_and_persists_result(tmp_path: Path):
    service = _service(tmp_path)
    task = service.submit(
        session_id="session-1",
        global_user_id="user-1",
        user_goal="say hello",
        runtime_ref="main",
        trace_id="trace-1",
    )

    outcome = service.run(
        task.task_id,
        global_user_id="user-1",
        executor=lambda _context: "hello",
        trace_id="trace-1",
    )

    assert outcome.text == "hello"
    assert outcome.task.state == TaskState.COMPLETED
    assert outcome.task.result_ref
    assert service.artifact_store.get_bytes(outcome.task.result_ref).decode("utf-8") == "hello"
    assert [
        event.payload.get("to_state")
        for event in service.state_machine.store.list_events(task.task_id)
        if event.payload.get("to_state")
    ] == ["running", "completed"]


def test_running_task_accepts_cooperative_cancel(tmp_path: Path):
    service = _service(tmp_path)
    started = threading.Event()
    release = threading.Event()
    task = service.submit(
        session_id="session-1",
        global_user_id="user-1",
        user_goal="wait",
        runtime_ref="main",
    )
    outcome_holder = []

    def execute(_context):
        started.set()
        assert release.wait(timeout=2)
        return "late result"

    thread = threading.Thread(
        target=lambda: outcome_holder.append(
            service.run(
                task.task_id,
                global_user_id="user-1",
                executor=execute,
            )
        )
    )
    thread.start()
    assert started.wait(timeout=2)

    pending = service.control(
        ParsedTaskCommand(action=ControlAction.CANCEL, task_id=task.task_id),
        ControlPrincipal(session_id="session-1", global_user_id="user-1"),
    )
    assert pending.state == TaskState.RUNNING
    assert pending.cancellation_token.pending is True

    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert outcome_holder[0].task.state == TaskState.CANCELLED
    assert outcome_holder[0].text is None


def test_pause_modify_and_resume_uses_revised_goal(tmp_path: Path):
    service = _service(tmp_path)
    started = threading.Event()
    release = threading.Event()
    task = service.submit(
        session_id="session-1",
        global_user_id="user-1",
        user_goal="old goal",
        runtime_ref="main",
    )
    first_outcome = []

    def execute(_context):
        started.set()
        assert release.wait(timeout=2)
        return "discarded"

    thread = threading.Thread(
        target=lambda: first_outcome.append(
            service.run(
                task.task_id,
                global_user_id="user-1",
                executor=execute,
            )
        )
    )
    thread.start()
    assert started.wait(timeout=2)

    modified = service.control(
        ParsedTaskCommand(
            action=ControlAction.MODIFY,
            task_id=task.task_id,
            patch={"user_goal": "new goal"},
        ),
        ControlPrincipal(session_id="session-1", global_user_id="user-1"),
    )
    assert modified.state == TaskState.PAUSED
    assert modified.user_goal == "new goal"
    release.set()
    thread.join(timeout=2)
    assert first_outcome[0].task.state == TaskState.PAUSED

    resumed = service.control(
        ParsedTaskCommand(action=ControlAction.RESUME, task_id=task.task_id),
        ControlPrincipal(session_id="session-1", global_user_id="user-1"),
    )
    assert resumed.state == TaskState.RUNNING
    second = service.run(
        task.task_id,
        global_user_id="user-1",
        executor=lambda context: f"completed: {context.user_goal}",
        resume=True,
    )
    assert second.task.state == TaskState.COMPLETED
    assert second.text == "completed: new goal"


def test_resume_transition_waits_for_prior_session_lane_execution(tmp_path: Path):
    service = _service(tmp_path)
    queue = CommandQueue()
    lane = session_lane_name("session-1")
    principal = ControlPrincipal(session_id="session-1", global_user_id="user-1")
    started = threading.Event()
    release = threading.Event()
    task = service.submit(
        session_id="session-1",
        global_user_id="user-1",
        user_goal="old goal",
        runtime_ref="main",
    )

    first = queue.enqueue(
        lane,
        lambda: service.run(
            task.task_id,
            global_user_id="user-1",
            executor=lambda _context: (
                started.set(),
                release.wait(timeout=2),
                "discarded",
            )[-1],
        ),
    )
    assert started.wait(timeout=2)
    service.control(
        ParsedTaskCommand(
            action=ControlAction.MODIFY,
            task_id=task.task_id,
            patch={"user_goal": "new goal"},
        ),
        principal,
    )

    def resume_in_lane():
        service.control(
            ParsedTaskCommand(action=ControlAction.RESUME, task_id=task.task_id),
            principal,
        )
        return service.run(
            task.task_id,
            global_user_id="user-1",
            executor=lambda context: f"completed: {context.user_goal}",
            resume=True,
        )

    second = queue.enqueue(lane, resume_in_lane)
    release.set()
    assert first.result(timeout=2).task.state == TaskState.PAUSED
    resumed = second.result(timeout=2)
    assert resumed.task.state == TaskState.COMPLETED
    assert resumed.text == "completed: new goal"
