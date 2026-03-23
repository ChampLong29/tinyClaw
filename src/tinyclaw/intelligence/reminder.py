"""Reminder tools: write and search user reminders."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tinyclaw.utils.timezone import format_iso_to_beijing


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
    ) -> str:
        """Write a reminder with due time."""
        due_str = due_time.isoformat() if due_time else ""

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "due": due_str,
            "content": content,
            "done": False,
        }

        path = self.reminder_dir / f"{datetime.now(timezone.utc).strftime('%Y%m%d')}.jsonl"
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            due_display = format_iso_to_beijing(due_str, fmt="%Y-%m-%d %H:%M", empty="")
            return f"已保存提醒：{content}" + (f"，到期 {due_display}" if due_display else "")
        except Exception as exc:
            return f"保存失败: {exc}"

    def get_due_reminders(self) -> list[dict]:
        """Get reminders past their due time that haven't been alerted yet."""
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
                    if entry.get("done") or entry.get("reminded"):
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

    def get_all_reminders(self) -> list[dict]:
        """Get all pending reminders."""
        reminders = []
        if not self.reminder_dir.is_dir():
            return reminders
        for f in sorted(self.reminder_dir.glob("*.jsonl")):
            try:
                for line in f.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    if not entry.get("done"):
                        reminders.append(entry)
            except Exception:
                continue
        return reminders

    def mark_done(self, task_id: str) -> str:
        """Mark a reminder as done (removes from list)."""
        if not self.reminder_dir.is_dir():
            return "没有提醒"
        now = datetime.now(timezone.utc)
        today_path = self.reminder_dir / f"{now.strftime('%Y%m%d')}.jsonl"
        if not today_path.exists():
            return "没有找到今日提醒"
        lines = []
        found = False
        for line in today_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if task_id and task_id in entry.get("content", ""):
                entry["done"] = True
                found = True
            lines.append(entry)
        if found:
            with open(today_path, "w", encoding="utf-8") as f:
                for e in lines:
                    if not e.get("done"):
                        f.write(json.dumps(e, ensure_ascii=False) + "\n")
            return "已标记完成"
        return "未找到提醒"

    def mark_reminded(self, ts: str) -> None:
        """Mark a reminder as already alerted (prevents duplicate alerts)."""
        if not self.reminder_dir.is_dir():
            return
        for f in sorted(self.reminder_dir.glob("*.jsonl")):
            try:
                lines = []
                for line in f.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    if entry.get("ts") == ts:
                        entry["reminded"] = True
                    lines.append(entry)
                with open(f, "w", encoding="utf-8") as out:
                    for entry in lines:
                        out.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                continue
