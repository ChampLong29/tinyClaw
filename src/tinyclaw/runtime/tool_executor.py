"""Production execution wrapper for classified, side-effect-aware tool recovery."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Mapping

from tinyclaw.pause_signals import (
    ClarificationRequiredSignal,
    ConfirmationRequiredSignal,
    InteractionPauseSignal,
    ToolRuntimeFailure,
)
from tinyclaw.runtime.tool_recovery import (
    RecoveryAction,
    SideEffectLevel,
    ToolErrorClassifier,
    ToolOperation,
    ToolRecoveryPolicy,
)

ToolCall = Callable[[str, dict[str, Any]], str]
RecoveryEventSink = Callable[[Mapping[str, Any]], None]
AuthRotator = Callable[[str], bool]


@dataclass(frozen=True, kw_only=True)
class ToolExecutionPolicy:
    max_attempts: int = 2
    base_backoff_seconds: float = 0.1
    max_backoff_seconds: float = 1.0


class ToolRecoveryExecutor:
    """Execute a tool and apply the domain recovery decision without blind retries."""

    _READ_ONLY = frozenset({"get_current_time", "memory_search", "read_file", "reminder_list"})
    _IDEMPOTENT = frozenset()
    _REVERSIBLE = frozenset({"edit_file", "memory_write", "reminder_write", "write_file"})
    _DESTRUCTIVE = frozenset({"bash", "wecom_cli_send_message"})

    def __init__(
        self,
        *,
        classifier: ToolErrorClassifier | None = None,
        recovery_policy: ToolRecoveryPolicy | None = None,
        execution_policy: ToolExecutionPolicy | None = None,
        auth_rotator: AuthRotator | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.classifier = classifier or ToolErrorClassifier()
        self.recovery_policy = recovery_policy or ToolRecoveryPolicy()
        self.execution_policy = execution_policy or ToolExecutionPolicy()
        self.auth_rotator = auth_rotator
        self.sleep = sleep

    def execute(
        self,
        tool_name: str,
        tool_input: Mapping[str, Any],
        call: ToolCall,
        *,
        scope: Mapping[str, Any],
        event_sink: RecoveryEventSink | None = None,
    ) -> str:
        operation = ToolOperation(
            tool_name=tool_name,
            arguments=dict(tool_input),
            side_effect_level=self._side_effect_level(tool_name),
        )
        while True:
            self._emit(event_sink, "tool_attempt_started", operation)
            try:
                result = call(tool_name, dict(tool_input))
            except InteractionPauseSignal:
                raise
            except Exception as error:
                failure = self.classifier.classify(error)
                decision = self.recovery_policy.decide(operation, failure)
                self._emit(
                    event_sink,
                    "tool_recovery_decided",
                    operation,
                    category=failure.category.value,
                    recovery_action=decision.action.value,
                    reason=decision.reason,
                    message=failure.message,
                )
                if (
                    decision.action == RecoveryAction.RETRY
                    and operation.attempt_number < self.execution_policy.max_attempts
                ):
                    delay = decision.retry_after_seconds
                    if delay is None:
                        delay = self.execution_policy.base_backoff_seconds * (
                            2 ** (operation.attempt_number - 1)
                        )
                    self.sleep(min(delay, self.execution_policy.max_backoff_seconds))
                    operation = operation.next_attempt()
                    continue
                if decision.action == RecoveryAction.RETURN_TO_AGENT:
                    return self._agent_error(failure.category.value, decision.action.value, error)
                if decision.action == RecoveryAction.ROTATE_AUTH:
                    retry_safe = operation.side_effect_level in {
                        SideEffectLevel.NONE,
                        SideEffectLevel.READ_ONLY,
                        SideEffectLevel.IDEMPOTENT,
                    }
                    if (
                        self.auth_rotator is not None
                        and retry_safe
                        and operation.attempt_number < self.execution_policy.max_attempts
                        and self.auth_rotator(tool_name)
                    ):
                        operation = operation.next_attempt()
                        continue
                    raise ClarificationRequiredSignal(
                        question=(
                            f"工具 {tool_name} 的认证已失效，且当前没有可安全轮换的凭据。"
                            "请更新凭据后说明处理方式。"
                        ),
                        required_fields=("resolution",),
                    ) from error
                if decision.action == RecoveryAction.REQUEST_CONFIRMATION:
                    action = {"tool": tool_name, "arguments": dict(tool_input)}
                    raise ConfirmationRequiredSignal(
                        action_summary=f"工具 {tool_name} 遇到权限限制，需要用户确认后继续",
                        action=action,
                        risk_level="high",
                        scope=dict(scope),
                    ) from error
                if decision.action in {
                    RecoveryAction.WAIT_USER,
                    RecoveryAction.CHECK_STATUS,
                    RecoveryAction.COMPENSATE,
                }:
                    raise ClarificationRequiredSignal(
                        question=(
                            f"工具 {tool_name} 执行结果不确定（{failure.category.value}）："
                            f"{failure.message}。请说明如何处理。"
                        ),
                        required_fields=("resolution",),
                    ) from error
                raise ToolRuntimeFailure(
                    f"{tool_name} failed [{failure.category.value}]: {failure.message}",
                    category=failure.category.value,
                ) from error
            self._emit(event_sink, "tool_attempt_completed", operation)
            return result

    @classmethod
    def _side_effect_level(cls, tool_name: str) -> SideEffectLevel:
        if tool_name in cls._READ_ONLY:
            return SideEffectLevel.READ_ONLY
        if tool_name in cls._IDEMPOTENT:
            return SideEffectLevel.IDEMPOTENT
        if tool_name in cls._REVERSIBLE:
            return SideEffectLevel.REVERSIBLE
        if tool_name in cls._DESTRUCTIVE:
            return SideEffectLevel.DESTRUCTIVE
        return SideEffectLevel.UNKNOWN

    @staticmethod
    def _agent_error(category: str, recovery_action: str, error: Exception) -> str:
        return f"Error [{category}; recovery={recovery_action}]: {error}"

    @staticmethod
    def _emit(
        sink: RecoveryEventSink | None,
        event_type: str,
        operation: ToolOperation,
        **payload: Any,
    ) -> None:
        if sink is None:
            return
        sink(
            {
                "event_type": event_type,
                "tool_name": operation.tool_name,
                "operation_id": operation.operation_id,
                "attempt_id": operation.attempt_id,
                "attempt_number": operation.attempt_number,
                "side_effect_level": operation.side_effect_level.value,
                **payload,
            }
        )
