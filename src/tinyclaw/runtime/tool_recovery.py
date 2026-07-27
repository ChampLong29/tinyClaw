"""Structured tool failure classification and safe recovery policy."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ToolErrorCategory(str, Enum):
    TRANSIENT_NETWORK = "transient_network"
    RATE_LIMITED = "rate_limited"
    AUTH_EXPIRED = "auth_expired"
    INVALID_ARGUMENTS = "invalid_arguments"
    PERMISSION_DENIED = "permission_denied"
    TOOL_UNAVAILABLE = "tool_unavailable"
    EXECUTION_TIMEOUT = "execution_timeout"
    PARTIAL_SIDE_EFFECT = "partial_side_effect"
    USER_ACTION_REQUIRED = "user_action_required"
    FATAL = "fatal"


class SideEffectLevel(str, Enum):
    NONE = "none"
    READ_ONLY = "read_only"
    IDEMPOTENT = "idempotent"
    REVERSIBLE = "reversible"
    DESTRUCTIVE = "destructive"
    UNKNOWN = "unknown"


class RecoveryAction(str, Enum):
    RETRY = "retry"
    ROTATE_AUTH = "rotate_auth"
    RETURN_TO_AGENT = "return_to_agent"
    REQUEST_CONFIRMATION = "request_confirmation"
    CHECK_STATUS = "check_status"
    COMPENSATE = "compensate"
    WAIT_USER = "wait_user"
    FAIL = "fail"


@dataclass(frozen=True, kw_only=True)
class ToolOperation:
    tool_name: str
    arguments: Mapping[str, Any]
    side_effect_level: SideEffectLevel
    operation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    idempotency_key: str | None = None
    can_check_status: bool = False
    can_compensate: bool = False
    attempt_number: int = 1

    def next_attempt(self) -> "ToolOperation":
        return ToolOperation(
            tool_name=self.tool_name,
            arguments=self.arguments,
            side_effect_level=self.side_effect_level,
            operation_id=self.operation_id,
            idempotency_key=self.idempotency_key,
            can_check_status=self.can_check_status,
            can_compensate=self.can_compensate,
            attempt_number=self.attempt_number + 1,
        )


@dataclass(frozen=True, kw_only=True)
class ToolFailure:
    category: ToolErrorCategory
    message: str
    retry_after_seconds: float | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class RecoveryDecision:
    action: RecoveryAction
    reason: str
    retry_after_seconds: float | None = None


class ToolExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: ToolErrorCategory,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retry_after_seconds = retry_after_seconds


class ToolErrorClassifier:
    def classify(self, error: Exception) -> ToolFailure:
        if isinstance(error, ToolExecutionError):
            return ToolFailure(
                category=error.category,
                message=str(error),
                retry_after_seconds=error.retry_after_seconds,
            )
        message = str(error)
        lowered = message.lower()
        status_code = getattr(error, "status_code", None)
        if status_code == 429 or "rate limit" in lowered or "429" in lowered:
            category = ToolErrorCategory.RATE_LIMITED
        elif status_code == 401 or "token expired" in lowered or "auth expired" in lowered:
            category = ToolErrorCategory.AUTH_EXPIRED
        elif status_code == 403 or "permission denied" in lowered or "forbidden" in lowered:
            category = ToolErrorCategory.PERMISSION_DENIED
        elif isinstance(error, (TimeoutError, ConnectionError)):
            category = (
                ToolErrorCategory.EXECUTION_TIMEOUT
                if isinstance(error, TimeoutError)
                else ToolErrorCategory.TRANSIENT_NETWORK
            )
        elif "invalid argument" in lowered or "validation" in lowered:
            category = ToolErrorCategory.INVALID_ARGUMENTS
        elif "unavailable" in lowered or "not found" in lowered:
            category = ToolErrorCategory.TOOL_UNAVAILABLE
        else:
            category = ToolErrorCategory.FATAL
        return ToolFailure(category=category, message=message)


class ToolRecoveryPolicy:
    """Never blindly retries an operation with uncertain side effects."""

    _SAFE_RETRY_LEVELS = frozenset(
        {SideEffectLevel.NONE, SideEffectLevel.READ_ONLY, SideEffectLevel.IDEMPOTENT}
    )

    def decide(
        self,
        operation: ToolOperation,
        failure: ToolFailure,
    ) -> RecoveryDecision:
        if failure.category == ToolErrorCategory.PARTIAL_SIDE_EFFECT:
            if operation.can_check_status:
                return RecoveryDecision(
                    action=RecoveryAction.CHECK_STATUS,
                    reason="partial side effect must be reconciled before retry",
                )
            if operation.can_compensate:
                return RecoveryDecision(
                    action=RecoveryAction.COMPENSATE,
                    reason="partial side effect requires compensation",
                )
            return RecoveryDecision(
                action=RecoveryAction.WAIT_USER,
                reason="partial side effect is uncertain and cannot be reconciled",
            )

        if failure.category == ToolErrorCategory.AUTH_EXPIRED:
            return RecoveryDecision(
                action=RecoveryAction.ROTATE_AUTH,
                reason="authentication profile expired",
            )
        if failure.category == ToolErrorCategory.INVALID_ARGUMENTS:
            return RecoveryDecision(
                action=RecoveryAction.RETURN_TO_AGENT,
                reason="agent must correct invalid arguments",
            )
        if failure.category == ToolErrorCategory.PERMISSION_DENIED:
            return RecoveryDecision(
                action=RecoveryAction.REQUEST_CONFIRMATION,
                reason="permission or risk confirmation is required",
            )
        if failure.category == ToolErrorCategory.USER_ACTION_REQUIRED:
            return RecoveryDecision(
                action=RecoveryAction.WAIT_USER,
                reason="tool requires user action",
            )

        retryable = failure.category in {
            ToolErrorCategory.TRANSIENT_NETWORK,
            ToolErrorCategory.RATE_LIMITED,
            ToolErrorCategory.EXECUTION_TIMEOUT,
            ToolErrorCategory.TOOL_UNAVAILABLE,
        }
        safe_to_retry = (
            operation.side_effect_level in self._SAFE_RETRY_LEVELS
            or operation.idempotency_key is not None
        )
        if retryable and safe_to_retry:
            return RecoveryDecision(
                action=RecoveryAction.RETRY,
                reason="transient failure on retry-safe operation",
                retry_after_seconds=failure.retry_after_seconds,
            )
        if retryable and operation.can_check_status:
            return RecoveryDecision(
                action=RecoveryAction.CHECK_STATUS,
                reason="side effects are uncertain; query status before retry",
            )
        if retryable:
            return RecoveryDecision(
                action=RecoveryAction.WAIT_USER,
                reason="retryable error but operation side effects are not retry-safe",
            )
        return RecoveryDecision(
            action=RecoveryAction.FAIL,
            reason="fatal tool failure",
        )
