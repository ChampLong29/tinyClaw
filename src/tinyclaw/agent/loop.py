"""Core agent loop for tinyClaw.

The agent loop is: while True + stop_reason
- Collect user input, append to messages
- Call LLM API
- Check stop_reason: "end_turn" or "tool_use"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from anthropic import Anthropic

from tinyclaw.agent.tools import ToolDispatcher


@dataclass
class AgentTurnResult:
    """Result of a complete agent turn (until end_turn)."""
    text: str
    messages: list[dict]
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class AgentLoop:
    """Core agent loop with tool-use support.

    This is the foundation of the tinyClaw agent. The loop:
      1. Collects user input, appends to messages
      2. Calls the LLM API
      3. On stop_reason == "end_turn": returns the response
      4. On stop_reason == "tool_use": dispatches tools and continues

    Attributes:
        client: Anthropic API client
        model: Model ID string
        tools: List of tool schemas for the LLM
        dispatcher: Tool name -> handler function dispatcher
        max_iterations: Max tool call iterations per turn (default 15)
    """

    def __init__(
        self,
        client: Anthropic,
        model: str,
        system_prompt: str,
        dispatcher: ToolDispatcher | None = None,
        max_iterations: int = 15,
        max_tokens: int = 8096,
    ) -> None:
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.dispatcher = dispatcher or ToolDispatcher()
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens

    def run_turn(
        self,
        messages: list[dict],
        user_input: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> tuple[str, list[dict]]:
        """Run a complete agent turn.

        Args:
            messages: Conversation history (modified in-place)
            user_input: New user message
            tools: Override tools list (uses dispatcher.tools if None)

        Returns:
            (assistant_text, updated_messages)
        """
        tools = tools or self.dispatcher.tools
        messages.append({"role": "user", "content": user_input})

        for _ in range(self.max_iterations):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=self.system_prompt,
                    tools=tools,
                    messages=messages,
                )
            except Exception as exc:
                # Roll back to last user message on error
                while messages and messages[-1]["role"] != "user":
                    messages.pop()
                if messages:
                    messages.pop()
                raise

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                text = "".join(
                    b.text for b in response.content if hasattr(b, "text")
                )
                return text, messages

            elif response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    result = self.dispatcher.dispatch(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
                messages.append({"role": "user", "content": tool_results})
                continue

            else:
                # max_tokens or other
                text = "".join(
                    b.text for b in response.content if hasattr(b, "text")
                )
                return text, messages

        return "[max iterations reached]", messages


def create_agent_loop(
    client: Anthropic,
    model: str,
    system_prompt: str,
    tools: list[dict[str, Any]] | None = None,
    dispatcher: ToolDispatcher | None = None,
) -> AgentLoop:
    """Factory function to create a configured AgentLoop."""
    loop = AgentLoop(
        client=client,
        model=model,
        system_prompt=system_prompt,
        dispatcher=dispatcher,
    )
    return loop
