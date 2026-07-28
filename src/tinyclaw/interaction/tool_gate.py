"""Production gate for high-risk tool calls."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from tinyclaw.interaction.confirmation import ConfirmationService, compute_action_digest
from tinyclaw.interaction.request_store import (
    ConfirmationRequest,
    RequestState,
    RiskLevel,
)
from tinyclaw.pause_signals import ConfirmationRequiredSignal


@dataclass(frozen=True, kw_only=True)
class ToolExecutionContext:
    task_id: str
    session_id: str
    global_user_id: str


class ToolRiskPolicy:
    """Conservative default policy for tools with external side effects."""

    HIGH_RISK_TOOLS = frozenset(
        {
            "bash",
            "edit_file",
            "wecom_cli_send_message",
            "write_file",
        }
    )

    def classify(self, tool_name: str, tool_input: Mapping[str, Any]) -> RiskLevel:
        del tool_input
        if tool_name in self.HIGH_RISK_TOOLS:
            return RiskLevel.HIGH
        return RiskLevel.LOW

    @staticmethod
    def summarize(tool_name: str, tool_input: Mapping[str, Any]) -> str:
        preview = json.dumps(
            dict(tool_input),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(preview) > 240:
            preview = preview[:237] + "..."
        return f"执行高风险工具 {tool_name}: {preview}"


class ToolExecutionGate:
    """Require and consume one durable approval for each exact high-risk action."""

    def __init__(
        self,
        confirmation_service: ConfirmationService,
        *,
        policy: ToolRiskPolicy | None = None,
    ) -> None:
        self.confirmation_service = confirmation_service
        self.policy = policy or ToolRiskPolicy()

    def authorize(
        self,
        tool_name: str,
        tool_input: Mapping[str, Any],
        context: ToolExecutionContext,
    ) -> None:
        risk_level = self.policy.classify(tool_name, tool_input)
        if risk_level not in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            return

        action = {"tool": tool_name, "arguments": dict(tool_input)}
        scope = {
            "task_id": context.task_id,
            "session_id": context.session_id,
            "global_user_id": context.global_user_id,
        }
        digest = compute_action_digest({"action": action, "scope": scope})
        for request in reversed(
            self.confirmation_service.request_store.list_for_task(context.task_id)
        ):
            if not isinstance(request, ConfirmationRequest):
                continue
            if request.action_digest != digest or request.state != RequestState.APPROVED:
                continue
            self.confirmation_service.consume_approval(
                request.request_id,
                session_id=context.session_id,
                global_user_id=context.global_user_id,
                action=action,
            )
            return

        raise ConfirmationRequiredSignal(
            action_summary=self.policy.summarize(tool_name, tool_input),
            action=action,
            risk_level=risk_level,
            scope=scope,
        )
