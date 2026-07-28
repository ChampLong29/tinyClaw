from pathlib import Path

import pytest

from tinyclaw.agent.tools import ToolDispatcher
from tinyclaw.contracts import TaskState
from tinyclaw.interaction.confirmation import ConfirmationService, ConfirmationTokenSigner
from tinyclaw.interaction.production import (
    ProductionInteractionService,
    parse_clarification_command,
    parse_confirmation_command,
)
from tinyclaw.interaction.request_store import (
    ConfirmationDecision,
    ConfirmationRequest,
    RequestState,
    RiskLevel,
    SQLiteRequestStore,
)
from tinyclaw.interaction.state_machine import TaskStateMachine
from tinyclaw.interaction.task_store import SQLiteTaskStore
from tinyclaw.interaction.tool_gate import ToolExecutionContext, ToolExecutionGate
from tinyclaw.observability.artifacts import ArtifactStore
from tinyclaw.pause_signals import (
    ClarificationRequiredSignal,
    ConfirmationRequiredSignal,
    ToolRuntimeFailure,
)
from tinyclaw.resilience.failover import AuthProfile, ProfileManager
from tinyclaw.resilience.runner import ResilienceRunner


@pytest.fixture
def confirmation_setup(tmp_path: Path):
    db_path = tmp_path / "interaction.db"
    task_store = SQLiteTaskStore(db_path)
    request_store = SQLiteRequestStore(db_path)
    machine = TaskStateMachine(task_store)
    confirmation = ConfirmationService(
        request_store,
        machine,
        ConfirmationTokenSigner("0123456789abcdef-confirmation-secret"),
    )
    try:
        yield task_store, request_store, machine, confirmation
    finally:
        request_store.close()
        task_store.close()


def test_high_risk_tool_requires_and_consumes_exact_approval_once(confirmation_setup):
    task_store, request_store, machine, confirmation = confirmation_setup
    task = machine.create_task(
        session_id="session-1",
        user_goal="write",
        actor="user-1",
        task_id="task-1",
    )
    machine.transition(task.task_id, TaskState.RUNNING, actor="runtime", reason="started")
    gate = ToolExecutionGate(confirmation)
    context = ToolExecutionContext(
        task_id=task.task_id,
        session_id=task.session_id,
        global_user_id="user-1",
    )
    tool_input = {"file_path": "a.txt", "content": "safe only after approval"}

    with pytest.raises(ConfirmationRequiredSignal) as raised:
        gate.authorize("write_file", tool_input, context)
    signal = raised.value
    request, token = confirmation.open(
        task_id=task.task_id,
        global_user_id="user-1",
        action_summary=signal.action_summary,
        action=signal.action,
        risk_level=signal.risk_level,
        scope=signal.scope,
        actor="runtime",
    )
    assert request.action == {
        "tool": "write_file",
        "arguments": tool_input,
    }

    confirmation.decide(
        request.request_id,
        session_id="session-1",
        global_user_id="user-1",
        action_token=token,
        decision=ConfirmationDecision.APPROVE_ONCE,
        actor="user-1",
    )
    gate.authorize("write_file", tool_input, context)
    assert request_store.get(request.request_id).state == RequestState.CONSUMED

    with pytest.raises(ConfirmationRequiredSignal):
        gate.authorize("write_file", tool_input, context)


def test_denied_confirmation_cancels_task(confirmation_setup):
    task_store, _request_store, machine, confirmation = confirmation_setup
    task = machine.create_task(
        session_id="session-1",
        user_goal="write",
        actor="user-1",
        task_id="task-1",
    )
    machine.transition(task.task_id, TaskState.RUNNING, actor="runtime", reason="started")
    action = {"tool": "write_file", "arguments": {"file_path": "a.txt", "content": "x"}}
    request, token = confirmation.open(
        task_id=task.task_id,
        global_user_id="user-1",
        action_summary="write",
        action=action,
        risk_level=RiskLevel.HIGH,
        scope={"task_id": task.task_id},
        actor="runtime",
    )
    confirmation.decide(
        request.request_id,
        session_id="session-1",
        global_user_id="user-1",
        action_token=token,
        decision=ConfirmationDecision.DENY,
        actor="user-1",
    )
    assert task_store.get_task(task.task_id).state == TaskState.CANCELLED


def test_runtime_adapter_turns_pause_signals_into_durable_wait_states(
    confirmation_setup, tmp_path: Path
):
    task_store, request_store, machine, confirmation = confirmation_setup
    service = ProductionInteractionService(
        state_machine=machine,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        confirmation_service=confirmation,
    )
    task = service.submit(
        session_id="session-1",
        global_user_id="user-1",
        user_goal="write",
        runtime_ref="main",
        task_id="task-1",
    )

    def pause(_context):
        raise ConfirmationRequiredSignal(
            action_summary="write",
            action={"tool": "write_file", "arguments": {"file_path": "a.txt"}},
            risk_level=RiskLevel.HIGH,
            scope={"task_id": task.task_id},
        )

    outcome = service.run(task.task_id, global_user_id="user-1", executor=pause)
    assert outcome.task.state == TaskState.WAITING_CONFIRMATION
    request = request_store.get(outcome.task.pending_request_ref)
    assert isinstance(request, ConfirmationRequest)


def test_explicit_request_command_parsers_do_not_treat_ordinary_chat_as_authority():
    assert parse_confirmation_command("好的，执行吧") is None
    assert parse_clarification_command("生产环境") is None
    confirm = parse_confirmation_command("/confirm approve req-1 signed-token")
    assert confirm.request_id == "req-1"
    assert confirm.decision == ConfirmationDecision.APPROVE_ONCE
    clarify = parse_clarification_command('/clarify req-2 {"environment":"production"}')
    assert clarify.answer == {"environment": "production"}
    clarify = parse_clarification_command('/clarify req-2 environment="staging blue"')
    assert clarify.answer == {"environment": "staging blue"}


def test_clarification_pause_signal_is_not_a_failure(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "interaction.db")
    request_store = SQLiteRequestStore(tmp_path / "interaction.db")
    machine = TaskStateMachine(store)
    from tinyclaw.interaction.clarification import ClarificationService

    service = ProductionInteractionService(
        state_machine=machine,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        clarification_service=ClarificationService(request_store, machine),
    )
    task = service.submit(
        session_id="session-1",
        global_user_id="user-1",
        user_goal="deploy",
        runtime_ref="main",
    )
    outcome = service.run(
        task.task_id,
        global_user_id="user-1",
        executor=lambda _context: (_ for _ in ()).throw(
            ClarificationRequiredSignal(
                question="Which environment?",
                required_fields=("environment",),
            )
        ),
    )
    assert outcome.task.state == TaskState.WAITING_USER
    request_store.close()
    store.close()


def test_pause_signal_bypasses_dispatcher_and_resilience_retry(monkeypatch):
    signal = ClarificationRequiredSignal(
        question="Which environment?",
        required_fields=("environment",),
    )
    dispatcher = ToolDispatcher()
    dispatcher.register(
        {"name": "pause", "input_schema": {"type": "object"}},
        lambda: (_ for _ in ()).throw(signal),
    )
    with pytest.raises(ClarificationRequiredSignal):
        dispatcher.dispatch("pause", {})

    profile = AuthProfile(name="primary", provider="anthropic", api_key="test-key")
    runner = ResilienceRunner(ProfileManager([profile]), "test-model")
    monkeypatch.setattr(runner, "_tool_loop", lambda *args, **kwargs: (_ for _ in ()).throw(signal))
    with pytest.raises(ClarificationRequiredSignal):
        runner.run(system="system", messages=[], tools=[], tool_handler=lambda *_: "")
    assert runner.total_failures == 0
    assert profile.cooldown_until == 0

    fatal = ToolRuntimeFailure("fatal tool failure", category="fatal")
    monkeypatch.setattr(runner, "_tool_loop", lambda *args, **kwargs: (_ for _ in ()).throw(fatal))
    with pytest.raises(ToolRuntimeFailure):
        runner.run(system="system", messages=[], tools=[], tool_handler=lambda *_: "")
    assert runner.total_failures == 0


def test_approved_action_survives_request_store_restart(tmp_path: Path):
    db_path = tmp_path / "interaction.db"
    task_store = SQLiteTaskStore(db_path)
    request_store = SQLiteRequestStore(db_path)
    machine = TaskStateMachine(task_store)
    signer = ConfirmationTokenSigner("0123456789abcdef-confirmation-secret")
    confirmation = ConfirmationService(request_store, machine, signer)
    task = machine.create_task(
        session_id="session-1",
        user_goal="write",
        actor="user-1",
        task_id="task-1",
    )
    machine.transition(task.task_id, TaskState.RUNNING, actor="runtime", reason="started")
    context = ToolExecutionContext(
        task_id=task.task_id,
        session_id=task.session_id,
        global_user_id="user-1",
    )
    action = {"tool": "write_file", "arguments": {"file_path": "a.txt", "content": "x"}}
    request, token = confirmation.open(
        task_id=task.task_id,
        global_user_id="user-1",
        action_summary="write",
        action=action,
        risk_level=RiskLevel.HIGH,
        scope={
            "task_id": task.task_id,
            "session_id": task.session_id,
            "global_user_id": "user-1",
        },
        actor="runtime",
    )
    confirmation.decide(
        request.request_id,
        session_id=task.session_id,
        global_user_id="user-1",
        action_token=token,
        decision=ConfirmationDecision.APPROVE_ONCE,
        actor="user-1",
    )
    request_store.close()

    reopened = SQLiteRequestStore(db_path)
    try:
        loaded = reopened.get(request.request_id)
        assert isinstance(loaded, ConfirmationRequest)
        assert loaded.action == action
        restarted_gate = ToolExecutionGate(ConfirmationService(reopened, machine, signer))
        restarted_gate.authorize("write_file", action["arguments"], context)
        assert reopened.get(request.request_id).state == RequestState.CONSUMED
    finally:
        reopened.close()
        task_store.close()
