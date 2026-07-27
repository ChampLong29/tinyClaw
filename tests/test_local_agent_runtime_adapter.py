from tinyclaw.runtime.local_agent_adapter import LocalAgentRuntimeAdapter
from tinyclaw.runtime.port import RuntimeEventType, TaskContext


class FakeAgentLoop:
    def __init__(self):
        self.calls = []

    def run_turn(self, messages, user_input):
        self.calls.append((messages, user_input))
        messages.append({"role": "assistant", "content": "done"})
        return "done", messages


def test_existing_agent_loop_is_adapted_to_runtime_events():
    loop = FakeAgentLoop()
    written = {}
    adapter = LocalAgentRuntimeAdapter(
        loop,
        history_loader=lambda session_id: [{"role": "system", "content": session_id}],
        result_writer=lambda task_id, text: written.setdefault(task_id, f"artifact:{text}"),
    )
    context = TaskContext(
        task_id="task-1",
        session_id="session-1",
        user_goal="do work",
        task_revision=2,
    )

    events = list(adapter.start(context))

    assert [event.type for event in events] == [
        RuntimeEventType.PROGRESS,
        RuntimeEventType.PROGRESS,
        RuntimeEventType.RESULT,
    ]
    assert events[-1].payload["result_ref"] == "artifact:done"
    assert loop.calls[0][1] == "do work"
    assert adapter.get_status("task-1")["state"] == "completed"
