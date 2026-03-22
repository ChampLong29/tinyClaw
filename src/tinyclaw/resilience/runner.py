"""3-layer resilience runner: auth rotation -> overflow recovery -> tool loop."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from anthropic import Anthropic

from tinyclaw.resilience.failover import (
    FailoverReason, ProfileManager, classify_failure,
)
from tinyclaw.session.context_guard import ContextGuard


MAX_OVERFLOW_COMPACTION = 3
BASE_RETRY = 24


@dataclass
class SimulatedFailure:
    """Arm a simulated failure for testing retry behavior."""
    TEMPLATES: dict[str, str] = field(default_factory=lambda: {
        "rate_limit": "Error code: 429 -- rate limit exceeded",
        "auth": "Error code: 401 -- authentication failed",
        "timeout": "Request timed out after 30s",
        "billing": "Error code: 402 -- billing quota exceeded",
        "overflow": "Error: context window token overflow",
        "unknown": "Error: unexpected internal server error",
    })

    _pending: str | None = None

    def arm(self, reason: str) -> str:
        if reason not in self.TEMPLATES:
            return f"Unknown reason '{reason}'. Valid: {', '.join(self.TEMPLATES.keys())}"
        self._pending = reason
        return f"Armed: next API call will fail with '{reason}'"

    def check_and_fire(self) -> None:
        if self._pending is not None:
            reason = self._pending
            self._pending = None
            raise RuntimeError(self.TEMPLATES[reason])

    @property
    def is_armed(self) -> bool:
        return self._pending is not None


class ResilienceRunner:
    """3-layer retry wrapper for agent execution.

    Layer 1 (outer): Auth profile rotation
    Layer 2 (middle): Context overflow compaction (up to 3 attempts)
    Layer 3 (inner): Tool-use loop (while True + stop_reason)

    On overflow: compact -> retry Layer 3 with same profile
    On auth/rate/timeout: cooldown profile -> retry Layer 1 with next profile
    On all profiles exhausted: try fallback models
    """

    def __init__(
        self,
        profile_manager: ProfileManager,
        model_id: str,
        fallback_models: list[str] | None = None,
        context_guard: ContextGuard | None = None,
        simulated_failure: SimulatedFailure | None = None,
        max_tool_iterations: int = 15,
    ) -> None:
        self.profile_manager = profile_manager
        self.model_id = model_id
        self.fallback_models = fallback_models or []
        self.guard = context_guard or ContextGuard()
        self.simulated_failure = simulated_failure
        self.max_tool_iterations = max_tool_iterations
        self.total_attempts = 0
        self.total_successes = 0
        self.total_failures = 0
        self.total_compactions = 0
        self.total_rotations = 0

    def run(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        tool_handler: Any,  # (name, input_dict) -> str
    ) -> tuple[Any, list[dict]]:
        """Execute a complete agent turn with 3-layer retry."""
        current_messages = list(messages)
        profiles_tried: set[str] = set()

        for _ in range(len(self.profile_manager.profiles)):
            profile = self.profile_manager.select_profile()
            if profile is None:
                break
            if profile.name in profiles_tried:
                break
            profiles_tried.add(profile.name)

            if len(profiles_tried) > 1:
                self.total_rotations += 1
            api_client = Anthropic(api_key=profile.api_key)

            for compact_attempt in range(MAX_OVERFLOW_COMPACTION):
                try:
                    self.total_attempts += 1
                    if self.simulated_failure:
                        self.simulated_failure.check_and_fire()

                    result, updated = self._tool_loop(
                        api_client, self.model_id, system,
                        current_messages, tools, tool_handler,
                    )
                    self.profile_manager.mark_success(profile)
                    self.total_successes += 1
                    return result, updated

                except Exception as exc:
                    reason = classify_failure(exc)
                    self.total_failures += 1

                    if reason == FailoverReason.overflow:
                        if compact_attempt < MAX_OVERFLOW_COMPACTION - 1:
                            self.total_compactions += 1
                            current_messages = self.guard._truncate_large_tool_results(
                                current_messages
                            )
                            current_messages = self.guard.compact_history(
                                current_messages, api_client, self.model_id
                            )
                            continue
                        else:
                            self.profile_manager.mark_failure(profile, reason, 600)
                            break

                    cooldown_map = {
                        FailoverReason.auth: 300,
                        FailoverReason.billing: 300,
                        FailoverReason.rate_limit: 120,
                        FailoverReason.timeout: 60,
                        FailoverReason.unknown: 120,
                    }
                    self.profile_manager.mark_failure(
                        profile, reason, cooldown_map.get(reason, 120)
                    )
                    break

        # Fallback models
        if self.fallback_models:
            for fallback_model in self.fallback_models:
                profile = self.profile_manager.select_profile()
                if profile is None:
                    for p in self.profile_manager.profiles:
                        if p.failure_reason in ("rate_limit", "timeout"):
                            p.cooldown_until = 0.0
                    profile = self.profile_manager.select_profile()
                if profile is None:
                    continue
                api_client = Anthropic(api_key=profile.api_key)
                try:
                    self.total_attempts += 1
                    if self.simulated_failure:
                        self.simulated_failure.check_and_fire()
                    result, updated = self._tool_loop(
                        api_client, fallback_model, system,
                        current_messages, tools, tool_handler,
                    )
                    self.profile_manager.mark_success(profile)
                    self.total_successes += 1
                    return result, updated
                except Exception:
                    self.total_failures += 1
                    continue

        raise RuntimeError(
            f"All {len(profiles_tried)} profiles and {len(self.fallback_models)} "
            "fallback models exhausted."
        )

    def _tool_loop(
        self,
        api_client: Anthropic,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        tool_handler: Any,
    ) -> tuple[Any, list[dict]]:
        """Layer 3: standard tool-use loop."""
        current_messages = list(messages)

        for _ in range(self.max_tool_iterations):
            response = api_client.messages.create(
                model=model,
                max_tokens=8096,
                system=system,
                tools=tools,
                messages=current_messages,
            )
            current_messages.append({
                "role": "assistant",
                "content": response.content,
            })

            if response.stop_reason == "end_turn":
                return response, current_messages

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    result = tool_handler(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
                current_messages.append({"role": "user", "content": tool_results})
                continue

            return response, current_messages

        raise RuntimeError(f"Tool loop exceeded {self.max_tool_iterations} iterations")

    def get_stats(self) -> dict[str, Any]:
        return {
            "total_attempts": self.total_attempts,
            "total_successes": self.total_successes,
            "total_failures": self.total_failures,
            "total_compactions": self.total_compactions,
            "total_rotations": self.total_rotations,
        }
