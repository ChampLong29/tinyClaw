"""Skills manager: discovers and loads skills from workspace directories."""

from __future__ import annotations

from pathlib import Path
from typing import Any

MAX_SKILLS = 150
MAX_SKILLS_PROMPT = 30000


class SkillsManager:
    """Discovers and manages skills from workspace skill directories.

    A skill is a directory containing a SKILL.md with YAML frontmatter:
      ---
      name: skill-name
      description: What the skill does
      invocation: /invocation
      ---
      # Body: instructions for the agent
    """

    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir
        self.skills: list[dict[str, str]] = []

    def _parse_frontmatter(self, text: str) -> dict[str, str]:
        """Parse simple YAML frontmatter without pyyaml."""
        meta: dict[str, str] = {}
        if not text.startswith("---"):
            return meta
        parts = text.split("---", 2)
        if len(parts) < 3:
            return meta
        for line in parts[1].strip().splitlines():
            if ":" not in line:
                continue
            key, _, value = line.strip().partition(":")
            meta[key.strip()] = value.strip()
        return meta

    def _scan_dir(self, base: Path) -> list[dict[str, str]]:
        """Scan a directory for skill subdirectories."""
        found: list[dict[str, str]] = []
        if not base.is_dir():
            return found
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                content = skill_md.read_text(encoding="utf-8")
            except Exception:
                continue
            meta = self._parse_frontmatter(content)
            if not meta.get("name"):
                continue
            body = ""
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    body = parts[2].strip()
            found.append({
                "name": meta.get("name", ""),
                "description": meta.get("description", ""),
                "invocation": meta.get("invocation", ""),
                "body": body,
                "path": str(child),
            })
        return found

    def discover(self, extra_dirs: list[Path] | None = None) -> None:
        """Scan skill directories in priority order. Later discoveries override earlier ones."""
        scan_order: list[Path] = []
        if extra_dirs:
            scan_order.extend(extra_dirs)
        scan_order.extend([
            self.workspace_dir / "skills",
            self.workspace_dir / ".skills",
            self.workspace_dir / ".agents" / "skills",
            Path.cwd() / ".agents" / "skills",
            Path.cwd() / "skills",
        ])

        seen: dict[str, dict[str, str]] = {}
        for d in scan_order:
            for skill in self._scan_dir(d):
                seen[skill["name"]] = skill
        self.skills = list(seen.values())[:MAX_SKILLS]

    def format_prompt_block(self) -> str:
        """Format all discovered skills as a Markdown prompt block."""
        if not self.skills:
            return ""
        lines = ["## Available Skills", ""]
        total = 0
        for skill in self.skills:
            block = (
                f"### Skill: {skill['name']}\n"
                f"Description: {skill['description']}\n"
                f"Invocation: {skill['invocation']}\n"
            )
            if skill.get("body"):
                block += f"\n{skill['body']}\n"
            block += "\n"
            if total + len(block) > MAX_SKILLS_PROMPT:
                lines.append("(... more skills truncated)")
                break
            lines.append(block)
            total += len(block)
        return "\n".join(lines)
