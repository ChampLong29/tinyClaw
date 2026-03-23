"""Heartbeat runner: proactive background checks."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


def run_agent_single_turn(
    prompt: str,
    system_prompt: str,
    client_factory: Callable[[], Any],  # () -> Anthropic
    model: str,
) -> str:
    """Single-turn LLM call for heartbeat and cron tasks."""
    try:
        client = client_factory()
        response = client.messages.create(
            model=model, max_tokens=2048, system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in response.content if hasattr(b, "text")).strip()
    except Exception as exc:
        return f"[agent error: {exc}]"


class HeartbeatRunner:
    """Proactive background task runner.

    Runs a heartbeat check at a configured interval during active hours.
    Uses non-blocking lock acquisition to yield to user interactions.
    """

    def __init__(
        self,
        workspace: Path,
        lane_lock: threading.Lock | None = None,
        interval: float = 1800.0,
        active_hours: tuple[int, int] = (9, 22),
        client_factory: Callable[[], Any] | None = None,
        model: str = "claude-sonnet-4-20250514",
    ) -> None:
        self.workspace = workspace
        self.heartbeat_path = workspace / "HEARTBEAT.md"
        self.lane_lock = lane_lock or threading.Lock()
        self.interval = interval
        self.active_hours = active_hours
        self.client_factory = client_factory
        self.model = model
        self.last_run_at: float = 0.0
        self.running: bool = False
        self._stopped: bool = False
        self._thread: threading.Thread | None = None
        self._output_queue: list[str] = []
        self._queue_lock = threading.Lock()
        self._last_output: str = ""

    def should_run(self) -> tuple[bool, str]:
        """Pre-flight checks. Lock check is handled in _execute()."""
        if not self.heartbeat_path.exists():
            return False, "HEARTBEAT.md 未找到"
        if not self.heartbeat_path.read_text(encoding="utf-8").strip():
            return False, "HEARTBEAT.md 为空"
        now = time.time()
        elapsed = now - self.last_run_at
        if elapsed < self.interval:
            return False, f"间隔未到（还剩 {self.interval - elapsed:.0f}秒）"
        hour = datetime.now().hour
        s, e = self.active_hours
        in_hours = (s <= hour < e) if s <= e else not (e <= hour < s)
        if not in_hours:
            return False, f"不在活跃时段内 ({s}:00-{e}:00)"
        if self.running:
            return False, "正在运行中"
        return True, "所有检查通过"

    def _build_prompt(self) -> tuple[str, str]:
        """Build heartbeat instructions and system prompt."""
        from tinyclaw.intelligence.soul import SoulSystem
        from tinyclaw.intelligence.memory import MemoryStore
        from tinyclaw.intelligence.reminder import ReminderStore

        instructions = self.heartbeat_path.read_text(encoding="utf-8").strip()
        soul = SoulSystem(self.workspace)
        memory = MemoryStore(self.workspace)
        reminder_store = ReminderStore(self.workspace)
        mem_text = memory.load_evergreen()

        # Check for due reminders
        due_reminders = reminder_store.get_due_reminders()
        reminder_section = ""
        if due_reminders:
            reminder_section = "\n\n## 到期提醒\n\n"
            for r in due_reminders:
                reminder_section += f"- {r['content']} (到期时间: {r.get('due', '未知')})\n"

        extra = ""
        if mem_text:
            extra = f"## Known Context\n\n{mem_text}\n\n"
        extra += f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        if reminder_section:
            extra += reminder_section
        return instructions, soul.build_system_prompt(extra)

    def _parse_response(self, response: str) -> str | None:
        """Return None for HEARTBEAT_OK (no report needed)."""
        if "HEARTBEAT_OK" in response:
            stripped = response.replace("HEARTBEAT_OK", "").strip()
            return stripped if len(stripped) > 5 else None
        return response.strip() or None

    def _execute(self) -> None:
        """Execute one heartbeat run. Non-blocking lock acquisition."""
        acquired = self.lane_lock.acquire(blocking=False)
        if not acquired:
            return
        self.running = True
        try:
            instructions, sys_prompt = self._build_prompt()
            if not instructions:
                return
            if self.client_factory:
                response = run_agent_single_turn(
                    instructions, sys_prompt, self.client_factory, self.model
                )
            else:
                return
            meaningful = self._parse_response(response)
            if meaningful is None:
                return
            if meaningful.strip() == self._last_output:
                return
            self._last_output = meaningful.strip()
            with self._queue_lock:
                self._output_queue.append(meaningful)
        except Exception as exc:
            with self._queue_lock:
                self._output_queue.append(f"[心跳错误: {exc}]")
        finally:
            self.running = False
            self.last_run_at = time.time()
            self.lane_lock.release()

    def _loop(self) -> None:
        while not self._stopped:
            try:
                ok, _ = self.should_run()
                if ok:
                    self._execute()
            except Exception:
                pass
            time.sleep(1.0)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopped = False
        self._thread = threading.Thread(target=self._loop, daemon=True, name="heartbeat")
        self._thread.start()

    def stop(self) -> None:
        self._stopped = True
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    def drain_output(self) -> list[str]:
        with self._queue_lock:
            items = list(self._output_queue)
            self._output_queue.clear()
            return items

    def trigger(self) -> str:
        """Manually trigger heartbeat, bypassing interval check."""
        acquired = self.lane_lock.acquire(blocking=False)
        if not acquired:
            return "主队列被占用，无法触发"
        self.running = True
        try:
            instructions, sys_prompt = self._build_prompt()
            if not instructions:
                return "HEARTBEAT.md 为空"
            if self.client_factory:
                response = run_agent_single_turn(
                    instructions, sys_prompt, self.client_factory, self.model
                )
            else:
                return "未配置客户端"
            meaningful = self._parse_response(response)
            if meaningful is None:
                return "无需报告的内容"
            if meaningful.strip() == self._last_output:
                return "内容重复，已跳过"
            self._last_output = meaningful.strip()
            with self._queue_lock:
                self._output_queue.append(meaningful)
            return f"已触发，输出已加入队列 ({len(meaningful)} 字符)"
        except Exception as exc:
            return f"触发失败: {exc}"
        finally:
            self.running = False
            self.last_run_at = time.time()
            self.lane_lock.release()

    def status(self) -> dict[str, Any]:
        now = time.time()
        elapsed = now - self.last_run_at if self.last_run_at > 0 else None
        next_in = max(0.0, self.interval - elapsed) if elapsed is not None else self.interval
        ok, reason = self.should_run()
        with self._queue_lock:
            qsize = len(self._output_queue)
        return {
            "enabled": self.heartbeat_path.exists(),
            "running": self.running,
            "should_run": ok,
            "reason": reason,
            "last_run": (datetime.fromtimestamp(self.last_run_at).strftime("%Y-%m-%d %H:%M:%S")
                         if self.last_run_at > 0 else "从未"),
            "next_in": round(next_in),
            "interval": self.interval,
            "active_hours": f"{self.active_hours[0]}:00-{self.active_hours[1]}:00",
            "queue_size": qsize,
        }
