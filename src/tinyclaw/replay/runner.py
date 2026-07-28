"""Replay execution, evaluation, comparison, and report persistence."""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Protocol

from tinyclaw.replay.evaluators import EVALUATORS, Evaluator
from tinyclaw.replay.schema import (
    ReplayCase,
    ReplayObservation,
    ReplayReport,
)


class ReplayMode(str, Enum):
    GATEWAY_ONLY = "gateway-only"
    AGENT_STUB_TOOLS = "agent-stub-tools"
    LIVE = "live"


class ReplayExecutor(Protocol):
    def execute(
        self,
        case: ReplayCase,
        *,
        mode: ReplayMode,
    ) -> ReplayObservation: ...


class RecordedReplayExecutor:
    def execute(
        self,
        case: ReplayCase,
        *,
        mode: ReplayMode,
    ) -> ReplayObservation:
        if mode == ReplayMode.LIVE:
            raise ValueError("recorded executor cannot run live replay")
        if not case.recorded_observation:
            raise ValueError("replay case has no recorded observation")
        return ReplayObservation.from_dict(case.recorded_observation)


class ReplayRunner:
    def __init__(
        self,
        executor: ReplayExecutor | None = None,
        *,
        evaluators: dict[str, Evaluator] | None = None,
    ) -> None:
        self.executor = executor or RecordedReplayExecutor()
        self.evaluators = dict(EVALUATORS)
        if evaluators:
            self.evaluators.update(evaluators)

    def run(
        self,
        case: ReplayCase,
        *,
        mode: ReplayMode = ReplayMode.GATEWAY_ONLY,
    ) -> ReplayReport:
        observation = self.executor.execute(case, mode=mode)
        outcomes = []
        for name in case.evaluators:
            evaluator = self.evaluators.get(name)
            if evaluator is None:
                raise ValueError(f"unknown replay evaluator: {name}")
            outcomes.append(evaluator(case, observation))
        return ReplayReport(
            case_id=case.case_id,
            mode=mode.value,
            outcomes=tuple(outcomes),
            observation=observation,
        )

    @staticmethod
    def write_report(
        report: ReplayReport,
        directory: Path | str,
    ) -> tuple[Path, Path]:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        json_path = root / f"{report.case_id}.report.json"
        markdown_path = root / f"{report.case_id}.report.md"
        json_path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        lines = [
            f"# Replay Report: {report.case_id}",
            "",
            f"- Mode: `{report.mode}`",
            f"- Passed: `{report.passed}`",
            f"- Score: `{report.score:.3f}`",
            "",
            "## Evaluators",
            "",
        ]
        for outcome in report.outcomes:
            marker = "PASS" if outcome.passed else "FAIL"
            lines.append(f"- **{marker}** `{outcome.evaluator}` — {outcome.message}")
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return json_path, markdown_path

    @staticmethod
    def compare(
        baseline: ReplayReport,
        candidate: ReplayReport,
    ) -> dict[str, object]:
        baseline_scores = {outcome.evaluator: outcome.score for outcome in baseline.outcomes}
        candidate_scores = {outcome.evaluator: outcome.score for outcome in candidate.outcomes}
        names = sorted(set(baseline_scores) | set(candidate_scores))
        return {
            "baseline_report_id": baseline.report_id,
            "candidate_report_id": candidate.report_id,
            "score_delta": candidate.score - baseline.score,
            "evaluator_deltas": {
                name: candidate_scores.get(name, 0.0) - baseline_scores.get(name, 0.0)
                for name in names
            },
            "regressed": candidate.score < baseline.score,
        }
