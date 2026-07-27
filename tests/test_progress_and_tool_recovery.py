from datetime import timedelta

from tinyclaw.contracts._common import utc_now
from tinyclaw.interaction.progress import (
    ProgressCoalescer,
    ProgressEvent,
    ProgressEventType,
)
from tinyclaw.runtime.tool_recovery import (
    RecoveryAction,
    SideEffectLevel,
    ToolErrorCategory,
    ToolFailure,
    ToolOperation,
    ToolRecoveryPolicy,
)


def test_progress_coalesces_same_phase_but_emits_phase_and_terminal_changes():
    now = utc_now()
    coalescer = ProgressCoalescer(
        minimum_interval=timedelta(seconds=10),
        minimum_fraction_delta=0.25,
    )
    first = ProgressEvent(
        task_id="task-1",
        type=ProgressEventType.PHASE_STARTED,
        phase="collect",
        message="Collecting input",
        occurred_at=now,
    )
    noisy = ProgressEvent(
        task_id="task-1",
        type=ProgressEventType.STEP_PROGRESS,
        phase="collect",
        message="Still collecting",
        occurred_at=now + timedelta(seconds=1),
        completed_units=1,
        total_units=10,
    )
    next_phase = ProgressEvent(
        task_id="task-1",
        type=ProgressEventType.PHASE_STARTED,
        phase="render",
        message="Rendering",
        occurred_at=now + timedelta(seconds=2),
    )
    completed = ProgressEvent(
        task_id="task-1",
        type=ProgressEventType.COMPLETED,
        phase="render",
        message="Done",
        occurred_at=now + timedelta(seconds=3),
    )

    assert coalescer.should_emit(first) is True
    assert coalescer.should_emit(noisy) is False
    assert coalescer.should_emit(next_phase) is True
    assert coalescer.should_emit(completed) is True


def test_partial_side_effect_is_never_blindly_retried():
    policy = ToolRecoveryPolicy()
    operation = ToolOperation(
        tool_name="charge_card",
        arguments={"amount": 100},
        side_effect_level=SideEffectLevel.DESTRUCTIVE,
        can_check_status=True,
    )
    decision = policy.decide(
        operation,
        ToolFailure(
            category=ToolErrorCategory.PARTIAL_SIDE_EFFECT,
            message="connection dropped after submit",
        ),
    )

    assert decision.action == RecoveryAction.CHECK_STATUS


def test_transient_error_retries_only_when_operation_is_safe():
    policy = ToolRecoveryPolicy()
    failure = ToolFailure(
        category=ToolErrorCategory.TRANSIENT_NETWORK,
        message="connection reset",
    )
    safe = ToolOperation(
        tool_name="lookup",
        arguments={},
        side_effect_level=SideEffectLevel.READ_ONLY,
    )
    unsafe = ToolOperation(
        tool_name="send_payment",
        arguments={},
        side_effect_level=SideEffectLevel.UNKNOWN,
    )

    assert policy.decide(safe, failure).action == RecoveryAction.RETRY
    assert policy.decide(unsafe, failure).action == RecoveryAction.WAIT_USER


def test_attempt_ids_change_but_operation_id_is_stable():
    operation = ToolOperation(
        tool_name="lookup",
        arguments={},
        side_effect_level=SideEffectLevel.READ_ONLY,
    )
    retry = operation.next_attempt()

    assert retry.operation_id == operation.operation_id
    assert retry.attempt_id != operation.attempt_id
    assert retry.attempt_number == 2
