"""Reminder tools: write and search user reminders."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path


class ReminderStore:
    """Store and retrieve user reminders."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.reminder_dir = workspace / "memory" / "reminders"
        self.reminder_dir.mkdir(parents=True, exist_ok=True)

    def write_reminder(
        self,
        content: str,
        due_time: datetime | None = None,
        category: str = "reminder",
    ) -> str:
        """Write a reminder. If due_time is None, assume it's a relative time."""
        reminder_time = due_time or datetime.now(timezone.utc)

        # Parse relative time from content if no due_time provided
        due_str = ""
        if due_time:
            due_str = due_time.isoformat()

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "due": due_str,
            "content": content,
            "category": category,
            "done": False,
        }

        path = self.reminder_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return f"已保存提醒：{content}" + (f"，到期时间 {due_str}" if due_str else "")
        except Exception as exc:
            return f"保存提醒失败: {exc}"

    def get_due_reminders(self) -> list[dict]:
        """Get all reminders that are due (past their due time)."""
        now = datetime.now(timezone.utc)
        reminders = []

        if not self.reminder_dir.is_dir():
            return reminders

        for f in sorted(self.reminder_dir.glob("*.jsonl")):
            try:
                for line in f.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    if entry.get("done"):
                        continue
                    due_str = entry.get("due", "")
                    if not due_str:
                        continue
                    try:
                        due = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
                        if due <= now:
                            reminders.append(entry)
                    except ValueError:
                        continue
            except Exception:
                continue

        return reminders

    def mark_done(self, content: str) -> str:
        """Mark a reminder as done by matching content."""
        now = datetime.now(timezone.utc)
        today_path = self.reminder_dir / f"{now.strftime('%Y%m%d')}.jsonl"

        if not today_path.exists():
            return "未找到今日提醒"

        lines = today_path.read_text(encoding="utf-8").splitlines()
        found = False
        for line in lines:
            if not line.strip():
                continue
            entry = json.loads(line)
            if content.lower() in entry.get("content", "").lower():
                entry["done"] = True
                found = True

        if found:
            with open(today_path, "w", encoding="utf-8") as f:
                for entry in [json.loads(l) for l in lines if l.strip()]:
                    if not entry.get("done"):
                        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return f"已标记完成：{content}"
        return "未找到匹配的提醒"
