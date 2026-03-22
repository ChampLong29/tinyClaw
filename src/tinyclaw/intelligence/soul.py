"""Soul system: loads and applies agent personality from SOUL.md."""

from __future__ import annotations

from pathlib import Path


class SoulSystem:
    """Loads the agent's personality / "soul" from SOUL.md.

    The soul is injected near the top of the system prompt because
    position matters: earlier content has stronger influence.
    """

    def __init__(self, workspace: Path) -> None:
        self.soul_path = workspace / "SOUL.md"

    def load(self) -> str:
        """Load SOUL.md content, or return a default."""
        if self.soul_path.exists():
            return self.soul_path.read_text(encoding="utf-8").strip()
        return "You are a helpful AI assistant."

    def build_system_prompt(self, extra: str = "") -> str:
        """Build the soul portion of the system prompt."""
        parts = [self.load()]
        if extra:
            parts.append(extra)
        return "\n\n".join(parts)
