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
            return False, "HEARTBEAT.md not found"
        if not self.heartbeat_path.read_text(encoding="utf-8").strip():
            return False, "HEARTBEAT.md is empty"
        now = time.time()
        elapsed = now - self.last_run_at
        if elapsed < self.interval:
            return False, f"interval not elapsed ({self.interval - elapsed:.0f}s remaining)"
        hour = datetime.now().hour
        s, e = self.active_hours
        in_hours = (s <= hour < e) if s <= e else not (e <= hour < s)
        if not in_hours:
            return False, f"outside active hours ({s}:00-{e}:00)"
        if self.running:
            return False, "already running"
        return True, "all checks passed"

    def _build_prompt(self) -> tuple[str, str]:
        """Build heartbeat instructions and system prompt."""
        from tinyclaw.intelligence.soul import SoulSystem
        from tinyclaw.intelligence.memory import MemoryStore

        instructions = self.heartbeat_path.read_text(encoding="utf-8").strip()
        soul = SoulSystem(self.workspace)
        memory = MemoryStore(self.workspace)
        mem_text = memory.load_evergreen()
        extra = ""
        if mem_text:
            extra = f"## Known Context\n\n{mem_text}\n\n"
        extra += f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
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
                self._output_queue.append(f"[heartbeat error: {exc}]")
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
            return "main lane occupied, cannot trigger"
        self.running = True
        try:
            instructions, sys_prompt = self._build_prompt()
            if not instructions:
                return "HEARTBEAT.md is empty"
            if self.client_factory:
                response = run_agent_single_turn(
                    instructions, sys_prompt, self.client_factory, self.model
                )
            else:
                return "no client factory"
            meaningful = self._parse_response(response)
            if meaningful is None:
                return "HEARTBEAT_OK (nothing to report)"
            if meaningful.strip() == self._last_output:
                return "duplicate content (skipped)"
            self._last_output = meaningful.strip()
            with self._queue_lock:
                self._output_queue.append(meaningful)
            return f"triggered, output queued ({len(meaningful)} chars)"
        except Exception as exc:
            return f"trigger failed: {exc}"
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
            "last_run": (datetime.fromtimestamp(self.last_run_at).isoformat()
                         if self.last_run_at > 0 else "never"),
            "next_in": f"{round(next_in)}s",
            "interval": f"{self.interval}s",
            "active_hours": f"{self.active_hours[0]}:00-{self.active_hours[1]}:00",
            "queue_size": qsize,
        }
