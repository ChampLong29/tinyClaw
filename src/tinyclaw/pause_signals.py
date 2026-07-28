"""Neutral control-flow signals used to pause a runtime for user interaction."""

from __future__ import annotations

from typing import Any, Mapping


class InteractionPauseSignal(Exception):
    """Base class for expected runtime pauses that must bypass retry logic."""


class ToolRuntimeFailure(RuntimeError):
    """Fatal classified tool failure that must not trigger model-profile failover."""

    def __init__(self, message: str, *, category: str) -> None:
        super().__init__(message)
        self.category = category


class ConfirmationRequiredSignal(InteractionPauseSignal):
    def __init__(
        self,
        *,
        action_summary: str,
        action: Mapping[str, Any],
        risk_level: Any,
        scope: Mapping[str, Any],
    ) -> None:
        super().__init__(action_summary)
        self.action_summary = action_summary
        self.action = dict(action)
        self.risk_level = risk_level
        self.scope = dict(scope)


class ClarificationRequiredSignal(InteractionPauseSignal):
    def __init__(
        self,
        *,
        question: str,
        required_fields: tuple[str, ...],
        default_action: str = "cancel",
    ) -> None:
        super().__init__(question)
        self.question = question
        self.required_fields = required_fields
        self.default_action = default_action
