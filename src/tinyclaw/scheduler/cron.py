"""Cron service: schedule-based background tasks."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from croniter import croniter
    HAS_CRONITER = True
except ImportError:
    HAS_CRONITER = False


AUTO_DISABLE_THRESHOLD = 5


@dataclass
class CronJob:
    """A scheduled cron job."""
    id: str
    name: str
    enabled: bool
    schedule_kind: str        # "at" | "every" | "cron"
    schedule_config: dict
    payload: dict
    delete_after_run: bool = False
    consecutive_errors: int = 0
    last_run_at: float = 0.0
    next_run_at: float = 0.0


class CronService:
    """Cron scheduler supporting at/every/cron expressions.

    Supports three schedule types:
      - at: One-shot timestamp (ISO format)
      - every: Fixed interval in seconds from anchor time
      - cron: 5-field cron expression
    """

    def __init__(
        self,
        cron_file: Path,
        run_log: Path | None = None,
        client_factory: Callable[[], Any] | None = None,
        model: str = "claude-sonnet-4-20250514",
    ) -> None:
        self.cron_file = cron_file
        self.run_log = run_log or (cron_file.parent / "cron-runs.jsonl")
        self.client_factory = client_factory
        self.model = model
        self.jobs: list[CronJob] = []
        self._output_queue: list[str] = []
        self._queue_lock = __import__("threading").Lock()
        self.load_jobs()

    def load_jobs(self) -> None:
        """Load jobs from CRON.json."""
        self.jobs.clear()
        if not self.cron_file.exists():
            return
        try:
            raw = json.loads(self.cron_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        now = time.time()
        for jd in raw.get("jobs", []):
            sched = jd.get("schedule", {})
            kind = sched.get("kind", "")
            if kind not in ("at", "every", "cron"):
                continue
            job = CronJob(
                id=jd.get("id", ""),
                name=jd.get("name", ""),
                enabled=jd.get("enabled", True),
                schedule_kind=kind,
                schedule_config=sched,
                payload=jd.get("payload", {}),
                delete_after_run=jd.get("delete_after_run", False),
            )
            job.next_run_at = self._compute_next(job, now)
            self.jobs.append(job)

    def _compute_next(self, job: CronJob, now: float) -> float:
        """Compute the next run timestamp."""
        cfg = job.schedule_config
        if job.schedule_kind == "at":
            try:
                ts = datetime.fromisoformat(cfg.get("at", "")).timestamp()
                return ts if ts > now else 0.0
            except (ValueError, OSError):
                return 0.0
        if job.schedule_kind == "every":
            every = cfg.get("every_seconds", 3600)
            try:
                anchor = datetime.fromisoformat(cfg.get("anchor", "")).timestamp()
            except (ValueError, TypeError):
                anchor = now
            if now < anchor:
                return anchor
            steps = int((now - anchor) / every) + 1
            return anchor + steps * every
        if job.schedule_kind == "cron":
            if not HAS_CRONITER:
                return 0.0
            expr = cfg.get("expr", "")
            if not expr:
                return 0.0
            try:
                return croniter(
                    expr, datetime.fromtimestamp(now)
                ).get_next(datetime).timestamp()
            except (ValueError, KeyError):
                return 0.0
        return 0.0

    def tick(self) -> None:
        """Check and run due jobs. Call once per second."""
        now = time.time()
        remove_ids: list[str] = []
        for job in self.jobs:
            if not job.enabled or job.next_run_at <= 0 or now < job.next_run_at:
                continue
            self._run_job(job, now)
            if job.delete_after_run and job.schedule_kind == "at":
                remove_ids.append(job.id)
        if remove_ids:
            self.jobs = [j for j in self.jobs if j.id not in remove_ids]

    def _run_job(self, job: CronJob, now: float) -> None:
        """Execute a single job."""
        payload = job.payload
        kind = payload.get("kind", "")
        output, status, error = "", "ok", ""

        try:
            if kind == "agent_turn":
                msg = payload.get("message", "")
                if not msg:
                    output, status = "[empty message]", "skipped"
                else:
                    from tinyclaw.scheduler.heartbeat import run_agent_single_turn
                    sys_prompt = (
                        "You are performing a scheduled background task. Be concise. "
                        f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    if self.client_factory:
                        output = run_agent_single_turn(
                            msg, sys_prompt, self.client_factory, self.model
                        )
            elif kind == "system_event":
                output = payload.get("text", "")
                if not output:
                    status = "skipped"
            else:
                output, status, error = f"[unknown kind: {kind}]", "error", f"unknown kind: {kind}"
        except Exception as exc:
            status, error, output = "error", str(exc), f"[cron error: {exc}]"

        job.last_run_at = now
        if status == "error":
            job.consecutive_errors += 1
            if job.consecutive_errors >= AUTO_DISABLE_THRESHOLD:
                job.enabled = False
                msg = (f"Job '{job.name}' auto-disabled after "
                       f"{job.consecutive_errors} consecutive errors: {error}")
                with self._queue_lock:
                    self._output_queue.append(msg)
        else:
            job.consecutive_errors = 0

        job.next_run_at = self._compute_next(job, now)

        entry = {
            "job_id": job.id,
            "run_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "status": status,
            "output_preview": output[:200],
        }
        if error:
            entry["error"] = error
        try:
            with open(self.run_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

        if output and status != "skipped":
            with self._queue_lock:
                self._output_queue.append(f"[{job.name}] {output}")

    def trigger_job(self, job_id: str) -> str:
        """Manually trigger a job by ID."""
        for job in self.jobs:
            if job.id == job_id:
                self._run_job(job, time.time())
                return f"'{job.name}' triggered (errors={job.consecutive_errors})"
        return f"Job '{job_id}' not found"

    def drain_output(self) -> list[str]:
        with self._queue_lock:
            items = list(self._output_queue)
            self._output_queue.clear()
            return items

    def list_jobs(self) -> list[dict[str, Any]]:
        now = time.time()
        result = []
        for j in self.jobs:
            nxt = max(0.0, j.next_run_at - now) if j.next_run_at > 0 else None
            result.append({
                "id": j.id,
                "name": j.name,
                "enabled": j.enabled,
                "kind": j.schedule_kind,
                "errors": j.consecutive_errors,
                "last_run": (datetime.fromtimestamp(j.last_run_at).isoformat()
                             if j.last_run_at > 0 else "never"),
                "next_run": (datetime.fromtimestamp(j.next_run_at).isoformat()
                             if j.next_run_at > 0 else "n/a"),
                "next_in": round(nxt) if nxt is not None else None,
            })
        return result
