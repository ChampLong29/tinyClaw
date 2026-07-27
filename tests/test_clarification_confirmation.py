from datetime import timedelta
from pathlib import Path

import pytest

from tinyclaw.contracts import TaskState
from tinyclaw.contracts._common import utc_now
from tinyclaw.interaction.clarification import (
    ClarificationService,
    RequestAuthorizationError,
    RequestExpiredError,
)
from tinyclaw.interaction.confirmation import (
    ActionDigestMismatchError,
    ConfirmationService,
    ConfirmationTokenSigner,
    InvalidConfirmationTokenError,
)
from tinyclaw.interaction.request_store import (
    ConfirmationDecision,
    RequestState,
    RiskLevel,
    SQLiteRequestStore,
)
from tinyclaw.interaction.state_machine import TaskStateMachine
from tinyclaw.interaction.task_store import SQLiteTaskStore


@pytest.fixture
def interaction_setup(tmp_path: Path):
    db_path = tmp_path / "interaction.db"
    task_store = SQLiteTaskStore(db_path)
    request_store = SQLiteRequestStore(db_path)
    machine = TaskStateMachine(task_store)
    task = machine.create_task(
        session_id="session-1",
        user_goal="deploy service",
        actor="global-user-1",
        task_id="task-1",
    )
    machine.transition(
        task.task_id,
        TaskState.RUNNING,
        actor="runtime",
        reason="started",
    )
    try:
        yield task_store, request_store, machine
    finally:
        request_store.close()
        task_store.close()


def test_clarification_requires_matching_identity_and_required_fields(interaction_setup):
    task_store, request_store, machine = interaction_setup
    service = ClarificationService(request_store, machine)
    request = service.open(
        task_id="task-1",
        global_user_id="global-user-1",
        question="Which environment?",
        required_fields=("environment",),
        actor="runtime",
    )
    assert task_store.get_task("task-1").state == TaskState.WAITING_USER

    with pytest.raises(RequestAuthorizationError):
        service.answer(
            request.request_id,
            session_id="session-1",
            global_user_id="global-user-other",
            answer={"environment": "staging"},
            actor="global-user-other",
        )
    with pytest.raises(ValueError, match="missing required"):
        service.answer(
            request.request_id,
            session_id="session-1",
            global_user_id="global-user-1",
            answer={},
            actor="global-user-1",
        )

    answered = service.answer(
        request.request_id,
        session_id="session-1",
        global_user_id="global-user-1",
        answer={"environment": "staging"},
        actor="global-user-1",
    )
    assert answered.state == RequestState.ANSWERED
    assert task_store.get_task("task-1").state == TaskState.RUNNING


def test_expired_clarification_expires_the_task(interaction_setup):
    task_store, request_store, machine = interaction_setup
    service = ClarificationService(request_store, machine)
    request = service.open(
        task_id="task-1",
        global_user_id="global-user-1",
        question="Which environment?",
        required_fields=("environment",),
        actor="runtime",
        expires_at=utc_now() - timedelta(seconds=1),
    )

    with pytest.raises(RequestExpiredError):
        service.answer(
            request.request_id,
            session_id="session-1",
            global_user_id="global-user-1",
            answer={"environment": "staging"},
            actor="global-user-1",
        )
    assert task_store.get_task("task-1").state == TaskState.EXPIRED


def test_confirmation_requires_token_and_unchanged_action(interaction_setup):
    task_store, request_store, machine = interaction_setup
    service = ConfirmationService(
        request_store,
        machine,
        ConfirmationTokenSigner("0123456789abcdef-confirmation-secret"),
    )
    action = {"tool": "deploy", "arguments": {"environment": "production"}}
    request, token = service.open(
        task_id="task-1",
        global_user_id="global-user-1",
        action_summary="Deploy to production",
        action=action,
        risk_level=RiskLevel.HIGH,
        scope={"environment": "production"},
        actor="runtime",
    )
    assert task_store.get_task("task-1").state == TaskState.WAITING_CONFIRMATION

    with pytest.raises(InvalidConfirmationTokenError):
        service.decide(
            request.request_id,
            session_id="session-1",
            global_user_id="global-user-1",
            action_token="ordinary-chat-has-no-valid-token",
            action=action,
            decision=ConfirmationDecision.APPROVE_ONCE,
            actor="global-user-1",
        )
    with pytest.raises(ActionDigestMismatchError):
        service.decide(
            request.request_id,
            session_id="session-1",
            global_user_id="global-user-1",
            action_token=token,
            action={"tool": "deploy", "arguments": {"environment": "other"}},
            decision=ConfirmationDecision.APPROVE_ONCE,
            actor="global-user-1",
        )

    approved = service.decide(
        request.request_id,
        session_id="session-1",
        global_user_id="global-user-1",
        action_token=token,
        action=action,
        decision=ConfirmationDecision.APPROVE_ONCE,
        actor="global-user-1",
    )
    assert approved.state == RequestState.APPROVED
    assert task_store.get_task("task-1").state == TaskState.RUNNING
