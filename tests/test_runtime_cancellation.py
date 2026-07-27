from pathlib import Path

from tinyclaw.contracts import TaskState
from tinyclaw.interaction.orchestrator import InteractionOrchestrator
from tinyclaw.interaction.state_machine import TaskStateMachine
from tinyclaw.interaction.task_store import SQLiteTaskStore
from tinyclaw.runtime.local_agent_adapter import LocalAgentRuntimeAdapter
from tinyclaw.runtime.port import RuntimeEvent, RuntimeEventType, TaskContext


class ImmediateLoop:
    def run_turn(self, messages, user_input):
        return "late result", messages


def test_local_adapter_emits_cancelled_after_in_flight_work_finishes():
    adapter = LocalAgentRuntimeAdapter(
        ImmediateLoop(),
        history_loader=lambda _session_id: [],
        result_writer=lambda _task_id, _text: "artifact:late",
    )
    adapter.request_cancel("task-1")

    events = list(
        adapter.start(
            TaskContext(
                task_id="task-1",
                session_id="session-1",
                user_goal="work",
                task_revision=2,
            )
        )
    )

    assert events[-1].type == RuntimeEventType.CANCELLED
    assert all(event.type != RuntimeEventType.RESULT for event in events)


def test_orchestrator_maps_runtime_cancelled_to_task_cancelled(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "interaction.db")

    class CancelledRuntime:
        def start(self, _context):
            return iter(
                [
                    RuntimeEvent(
                        type=RuntimeEventType.CANCELLED,
                        payload={"reason": "cancelled at safe point"},
                    )
                ]
            )

    try:
        task = InteractionOrchestrator(
            state_machine=TaskStateMachine(store),
            runtime=CancelledRuntime(),
        ).start(
            session_id="session-1",
            global_user_id="user-1",
            user_goal="work",
            runtime_ref="cancelled.v1",
            task_id="task-1",
        )
        assert task.state == TaskState.CANCELLED
    finally:
        store.close()
