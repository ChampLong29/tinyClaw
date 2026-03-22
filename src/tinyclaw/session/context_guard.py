"""Context guard: protects against context window overflow.

Three-stage overflow recovery:
  1. Truncate oversized tool results (keep head at newline boundary)
  2. Compact old messages into LLM-generated summary (50% ratio)
  3. Still overflowing -> raise
"""

from __future__ import annotations

import json
from typing import Any

from anthropic import Anthropic


CONTEXT_SAFE_LIMIT = 180000
MAX_TOOL_OUTPUT = 50000


class ContextGuard:
    """Context window overflow protection with three-stage recovery."""

    def __init__(self, max_tokens: int = CONTEXT_SAFE_LIMIT) -> None:
        self.max_tokens = max_tokens

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Rough estimate: ~4 chars per token."""
        return len(text) // 4

    def estimate_messages_tokens(self, messages: list[dict]) -> int:
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += self.estimate_tokens(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if "text" in block:
                            total += self.estimate_tokens(block["text"])
                        elif block.get("type") == "tool_result":
                            rc = block.get("content", "")
                            if isinstance(rc, str):
                                total += self.estimate_tokens(rc)
                        elif block.get("type") == "tool_use":
                            total += self.estimate_tokens(json.dumps(block.get("input", {})))
                    elif hasattr(block, "text"):
                        total += self.estimate_tokens(block.text)
                    elif hasattr(block, "input"):
                        total += self.estimate_tokens(json.dumps(block.input))
        return total

    def truncate_tool_result(self, result: str, max_fraction: float = 0.3) -> str:
        """Truncate at newline boundary, keeping head of result."""
        max_chars = int(self.max_tokens * 4 * max_fraction)
        if len(result) <= max_chars:
            return result
        cut = result.rfind("\n", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        return result[:cut] + f"\n\n[... truncated ({len(result)} chars total) ...]"

    def compact_history(
        self,
        messages: list[dict],
        api_client: Anthropic,
        model: str,
    ) -> list[dict]:
        """Compress first 50% of messages into an LLM-generated summary."""
        total = len(messages)
        if total <= 4:
            return messages

        keep_count = max(4, int(total * 0.2))
        compress_count = max(2, int(total * 0.5))
        compress_count = min(compress_count, total - keep_count)
        if compress_count < 2:
            return messages

        old_messages = messages[:compress_count]
        recent_messages = messages[compress_count:]
        old_text = _serialize_messages_for_summary(old_messages)

        summary_prompt = (
            "Summarize the following conversation concisely, "
            "preserving key facts and decisions. "
            "Output only the summary, no preamble.\n\n" + old_text
        )

        try:
            summary_resp = api_client.messages.create(
                model=model,
                max_tokens=2048,
                system="You are a conversation summarizer. Be concise and factual.",
                messages=[{"role": "user", "content": summary_prompt}],
            )
            summary_text = "".join(
                b.text for b in summary_resp.content if hasattr(b, "text")
            )
        except Exception:
            return recent_messages

        compacted = [
            {"role": "user", "content": "[Previous conversation summary]\n" + summary_text},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Understood, I have the context from our previous conversation."}],
            },
        ]
        compacted.extend(recent_messages)
        return compacted

    def _truncate_large_tool_results(self, messages: list[dict]) -> list[dict]:
        """Truncate oversized tool_result blocks in messages."""
        result = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                new_blocks = []
                for block in content:
                    if (isinstance(block, dict)
                            and block.get("type") == "tool_result"
                            and isinstance(block.get("content"), str)):
                        block = dict(block)
                        block["content"] = self.truncate_tool_result(block["content"])
                    new_blocks.append(block)
                result.append({"role": msg["role"], "content": new_blocks})
            else:
                result.append(msg)
        return result

    def guard_api_call(
        self,
        api_client: Anthropic,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_retries: int = 2,
    ) -> Any:
        """Three-stage overflow recovery wrapper around messages.create().

        Stage 0: normal call
        Stage 1: truncate large tool results
        Stage 2: LLM summarization of history
        """
        current_messages = messages

        for attempt in range(max_retries + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "max_tokens": 8096,
                    "system": system,
                    "messages": current_messages,
                }
                if tools:
                    kwargs["tools"] = tools
                result = api_client.messages.create(**kwargs)
                if current_messages is not messages:
                    messages.clear()
                    messages.extend(current_messages)
                return result

            except Exception as exc:
                error_str = str(exc).lower()
                is_overflow = ("context" in error_str or "token" in error_str)

                if not is_overflow or attempt >= max_retries:
                    raise

                if attempt == 0:
                    current_messages = self._truncate_large_tool_results(current_messages)
                elif attempt == 1:
                    current_messages = self.compact_history(
                        current_messages, api_client, model
                    )

        raise RuntimeError("guard_api_call: exhausted retries")


def _serialize_messages_for_summary(messages: list[dict]) -> str:
    """Flatten messages to plain text for LLM summarization."""
    parts: list[str] = []
    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(f"[{role}]: {content}")
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    btype = block.get("type", "")
                    if btype == "text":
                        parts.append(f"[{role}]: {block['text']}")
                    elif btype == "tool_use":
                        parts.append(
                            f"[{role} called {block.get('name', '?')}]: "
                            f"{json.dumps(block.get('input', {}), ensure_ascii=False)}"
                        )
                    elif btype == "tool_result":
                        rc = block.get("content", "")
                        preview = rc[:500] if isinstance(rc, str) else str(rc)[:500]
                        parts.append(f"[tool_result]: {preview}")
                elif hasattr(block, "text"):
                    parts.append(f"[{role}]: {block.text}")
    return "\n".join(parts)
