from pathlib import Path

from tinyclaw.contracts import TaskState
from tinyclaw.interaction.state_machine import TaskStateMachine
from tinyclaw.interaction.task_store import SQLiteTaskStore
from tinyclaw.observability import TraceRecorder


def test_task_state_machine_records_append_only_trace(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "tasks.db")
    trace = TraceRecorder(tmp_path / "observability")
    machine = TaskStateMachine(store, trace_recorder=trace)
    try:
        task = machine.create_task(
            task_id="task-1",
            session_id="session-1",
            user_goal="prepare report",
            actor="user-1",
            trace_id="trace-1",
        )
        running = machine.transition(
            task.task_id,
            TaskState.RUNNING,
            actor="orchestrator",
            reason="runtime_started",
            expected_revision=task.revision,
            trace_id="trace-1",
        )
        machine.transition(
            task.task_id,
            TaskState.COMPLETED,
            actor="runtime",
            reason="runtime_completed",
            expected_revision=running.revision,
            trace_id="trace-1",
            result_ref="artifact://result",
        )

        events = trace.read_events(
            session_id="session-1",
            task_id="task-1",
        )
        assert [event.event_type for event in events] == [
            "task_created",
            "task_state_changed",
            "task_state_changed",
        ]
        assert [event.sequence for event in events] == [0, 1, 2]
        assert {event.trace_context.trace_id for event in events} == {"trace-1"}
        assert events[-1].payload["to_state"] == "completed"
    finally:
        store.close()
