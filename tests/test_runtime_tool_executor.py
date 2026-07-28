import pytest

from tinyclaw.agent.tools import ToolDispatcher
from tinyclaw.pause_signals import (
    ClarificationRequiredSignal,
    ConfirmationRequiredSignal,
    ToolRuntimeFailure,
)
from tinyclaw.runtime.tool_executor import ToolExecutionPolicy, ToolRecoveryExecutor
from tinyclaw.runtime.tool_recovery import ToolErrorCategory, ToolExecutionError


def _executor() -> ToolRecoveryExecutor:
    return ToolRecoveryExecutor(
        execution_policy=ToolExecutionPolicy(
            max_attempts=2,
            base_backoff_seconds=0,
            max_backoff_seconds=0,
        ),
        sleep=lambda _seconds: None,
    )


def test_read_only_transient_failure_retries_with_stable_operation_id():
    attempts = 0
    events = []

    def call(_name, _arguments):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("connection reset")
        return "ok"

    result = _executor().execute(
        "read_file",
        {"file_path": "a.txt"},
        call,
        scope={"task_id": "task-1"},
        event_sink=events.append,
    )

    assert result == "ok"
    assert attempts == 2
    started = [event for event in events if event["event_type"] == "tool_attempt_started"]
    assert [event["attempt_number"] for event in started] == [1, 2]
    assert len({event["operation_id"] for event in started}) == 1
    assert len({event["attempt_id"] for event in started}) == 2


def test_unknown_side_effect_transient_failure_waits_for_user_without_retry():
    attempts = 0

    def call(_name, _arguments):
        nonlocal attempts
        attempts += 1
        raise ConnectionError("connection reset after submit")

    with pytest.raises(ClarificationRequiredSignal):
        _executor().execute(
            "custom_external_action",
            {"value": 1},
            call,
            scope={"task_id": "task-1"},
        )
    assert attempts == 1


@pytest.mark.parametrize(
    "category",
    [
        ToolErrorCategory.PARTIAL_SIDE_EFFECT,
        ToolErrorCategory.USER_ACTION_REQUIRED,
    ],
)
def test_uncertain_or_user_action_failure_waits_for_user(category):
    with pytest.raises(ClarificationRequiredSignal):
        _executor().execute(
            "custom_external_action",
            {},
            lambda *_: (_ for _ in ()).throw(
                ToolExecutionError("manual resolution required", category=category)
            ),
            scope={"task_id": "task-1"},
        )


def test_invalid_arguments_return_to_agent_and_permission_requests_confirmation():
    invalid = _executor().execute(
        "read_file",
        {},
        lambda *_: (_ for _ in ()).throw(ValueError("invalid argument: file_path")),
        scope={"task_id": "task-1"},
    )
    assert "invalid_arguments" in invalid
    assert "return_to_agent" in invalid

    with pytest.raises(ConfirmationRequiredSignal) as raised:
        _executor().execute(
            "read_file",
            {"file_path": "private.txt"},
            lambda *_: (_ for _ in ()).throw(PermissionError("permission denied")),
            scope={"task_id": "task-1"},
        )
    assert raised.value.action["tool"] == "read_file"


def test_fatal_tool_failure_is_terminal():
    with pytest.raises(ToolRuntimeFailure) as raised:
        _executor().execute(
            "read_file",
            {"file_path": "a.txt"},
            lambda *_: (_ for _ in ()).throw(
                ToolExecutionError("corrupt runtime", category=ToolErrorCategory.FATAL)
            ),
            scope={"task_id": "task-1"},
        )
    assert raised.value.category == "fatal"


def test_expired_auth_rotates_only_for_retry_safe_operation():
    attempts = 0
    rotations = []
    executor = ToolRecoveryExecutor(
        execution_policy=ToolExecutionPolicy(
            max_attempts=2,
            base_backoff_seconds=0,
            max_backoff_seconds=0,
        ),
        auth_rotator=lambda tool_name: rotations.append(tool_name) or True,
        sleep=lambda _seconds: None,
    )

    def call(_name, _arguments):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ToolExecutionError(
                "auth expired",
                category=ToolErrorCategory.AUTH_EXPIRED,
            )
        return "ok"

    assert executor.execute("read_file", {}, call, scope={"task_id": "task-1"}) == "ok"
    assert rotations == ["read_file"]


def test_strict_dispatch_preserves_error_for_recovery_classifier():
    dispatcher = ToolDispatcher()
    dispatcher.register(
        {"name": "fails", "input_schema": {"type": "object"}},
        lambda: "Error: tool unavailable",
    )
    with pytest.raises(RuntimeError, match="tool unavailable"):
        dispatcher.dispatch_strict("fails", {})
