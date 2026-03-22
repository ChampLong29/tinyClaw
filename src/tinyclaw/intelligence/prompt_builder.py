"""System prompt builder: 8-layer assembly for tinyClaw agents.

The system prompt is assembled from these layers (in order):
  1. Identity (IDENTITY.md)
  2. Soul (SOUL.md) -- personality, highest influence position
  3. Tool Guidelines (TOOLS.md)
  4. Skills (discovered from workspace/.skills)
  5. Memory (MEMORY.md + auto-recall from hybrid search)
  6. Bootstrap (HEARTBEAT.md, BOOTSTRAP.md, AGENTS.md, USER.md)
  7. Runtime Context (agent_id, model, channel, time)
  8. Channel Hints (channel-specific formatting guidance)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def build_system_prompt(
    mode: str = "full",
    bootstrap: dict[str, str] | None = None,
    skills_block: str = "",
    memory_context: str = "",
    agent_id: str = "main",
    channel: str = "terminal",
    model: str = "",
) -> str:
    """Build the complete system prompt from layers.

    Args:
        mode: "full" (main agent) | "minimal" (sub-agent/cron) | "none"
        bootstrap: {filename: content} from BootstrapLoader
        skills_block: Formatted skills block from SkillsManager
        memory_context: Auto-recalled memories from hybrid search
        agent_id: Current agent identifier
        channel: Current channel name (terminal, telegram, discord, etc.)
        model: Model identifier for runtime context
    """
    if bootstrap is None:
        bootstrap = {}
    sections: list[str] = []

    # Layer 1: Identity
    identity = bootstrap.get("IDENTITY.md", "").strip()
    sections.append(identity if identity else "You are a helpful personal AI assistant.")

    # Layer 2: Soul (personality)
    if mode == "full":
        soul = bootstrap.get("SOUL.md", "").strip()
        if soul:
            sections.append(f"## Personality\n\n{soul}")

    # Layer 3: Tool guidelines
    tools_md = bootstrap.get("TOOLS.md", "").strip()
    if tools_md:
        sections.append(f"## Tool Usage Guidelines\n\n{tools_md}")

    # Layer 4: Skills
    if mode == "full" and skills_block:
        sections.append(skills_block)

    # Layer 5: Memory
    if mode == "full":
        mem_md = bootstrap.get("MEMORY.md", "").strip()
        parts: list[str] = []
        if mem_md:
            parts.append(f"### Evergreen Memory\n\n{mem_md}")
        if memory_context:
            parts.append(f"### Recalled Memories (auto-searched)\n\n{memory_context}")
        if parts:
            sections.append("## Memory\n\n" + "\n\n".join(parts))
        sections.append(
            "## Memory Instructions\n\n"
            "- Use memory_write to save important user facts and preferences.\n"
            "- Reference remembered facts naturally in conversation.\n"
            "- Use memory_search to recall specific past information."
        )

    # Layer 6: Bootstrap files
    if mode in ("full", "minimal"):
        for name in ["HEARTBEAT.md", "BOOTSTRAP.md", "AGENTS.md", "USER.md"]:
            content = bootstrap.get(name, "").strip()
            if content:
                sections.append(f"## {name.replace('.md', '')}\n\n{content}")

    # Layer 7: Runtime context
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ctx_parts = [f"- Agent ID: {agent_id}"]
    if model:
        ctx_parts.append(f"- Model: {model}")
    ctx_parts.extend([f"- Channel: {channel}", f"- Current time: {now}", f"- Prompt mode: {mode}"])
    sections.append("## Runtime Context\n\n" + "\n".join(ctx_parts))

    # Layer 8: Channel hints
    hints: dict[str, str] = {
        "terminal": "You are responding via a terminal REPL. Markdown is supported.",
        "telegram": "You are responding via Telegram. Keep messages concise.",
        "discord": "You are responding via Discord. Keep messages under 2000 characters.",
        "slack": "You are responding via Slack. Use Slack mrkdwn formatting.",
        "feishu": "You are responding via Feishu/Lark. Keep messages clear and well-formatted.",
    }
    sections.append(f"## Channel\n\n{hints.get(channel, f'You are responding via {channel}.')}")

    return "\n\n".join(sections)
