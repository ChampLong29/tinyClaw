"""Bootstrap loader: loads workspace bootstrap files at startup."""

from __future__ import annotations

from pathlib import Path

MAX_FILE_CHARS = 20000
MAX_TOTAL_CHARS = 150000

BOOTSTRAP_FILES = [
    "SOUL.md", "IDENTITY.md", "TOOLS.md", "USER.md",
    "HEARTBEAT.md", "BOOTSTRAP.md", "AGENTS.md", "MEMORY.md",
]


class BootstrapLoader:
    """Loads bootstrap files from the agent workspace.

    Modes:
      - full: Load all 8 BOOTSTRAP_FILES (main agent)
      - minimal: Load AGENTS.md + TOOLS.md (sub-agent / cron)
      - none: Return empty dict (minimized)
    """

    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir

    def load_file(self, name: str) -> str:
        path = self.workspace_dir / name
        if not path.is_file():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""

    def truncate_file(self, content: str, max_chars: int = MAX_FILE_CHARS) -> str:
        """Truncate at nearest newline boundary."""
        if len(content) <= max_chars:
            return content
        cut = content.rfind("\n", 0, max_chars)
        if cut <= 0:
            cut = max_chars
        return content[:cut] + f"\n\n[... truncated ({len(content)} chars total) ...]"

    def load_all(self, mode: str = "full") -> dict[str, str]:
        """Load all bootstrap files into {filename: content} dict."""
        if mode == "none":
            return {}
        names = ["AGENTS.md", "TOOLS.md"] if mode == "minimal" else list(BOOTSTRAP_FILES)
        result: dict[str, str] = {}
        total = 0
        for name in names:
            raw = self.load_file(name)
            if not raw:
                continue
            truncated = self.truncate_file(raw)
            if total + len(truncated) > MAX_TOTAL_CHARS:
                remaining = MAX_TOTAL_CHARS - total
                if remaining > 0:
                    truncated = self.truncate_file(raw, remaining)
                else:
                    break
            result[name] = truncated
            total += len(truncated)
        return result
