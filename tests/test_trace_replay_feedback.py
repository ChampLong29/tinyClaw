import json
from pathlib import Path

from tinyclaw.observability import (
    BadCaseCategory,
    FeedbackRecord,
    FeedbackSource,
    SQLiteFeedbackStore,
    TraceRecorder,
)
from tinyclaw.replay import (
    ReplayCase,
    ReplayCaseRecorder,
    ReplayMode,
    ReplayObservation,
    ReplayRunner,
)


def test_trace_recorder_redacts_offloads_and_recovers_sequences(tmp_path: Path):
    root = tmp_path / "observability"
    recorder = TraceRecorder(root, artifact_threshold_bytes=64)
    first = recorder.record(
        event_type="model_response",
        producer="agent",
        producer_version="agent-v1",
        session_id="session/one",
        task_id="task-1",
        payload={
            "api_key": "sk-very-secret-value",
            "body": "x" * 200,
        },
    )

    assert first.sequence == 0
    assert len(first.artifact_refs) == 1
    artifact = recorder.artifacts.get_json(first.artifact_refs[0])
    assert artifact["api_key"] == "[REDACTED]"
    assert "secret" not in json.dumps(artifact)

    restarted = TraceRecorder(root, artifact_threshold_bytes=64)
    second = restarted.record(
        event_type="task_succeeded",
        producer="gateway",
        session_id="session/one",
        task_id="task-1",
        payload={"ok": True},
    )
    assert second.sequence == 1

    first_annotation = restarted.annotate(
        session_id="session/one",
        task_id="task-1",
        trace_event_id=first.trace_event_id,
        labels=("bad-case",),
    )
    second_annotation = restarted.annotate(
        session_id="session/one",
        task_id="task-1",
        trace_event_id=first.trace_event_id,
        labels=("reviewed",),
    )
    assert [first_annotation.revision, second_annotation.revision] == [1, 2]
    assert [
        event.sequence
        for event in restarted.read_events(
            session_id="session/one",
            task_id="task-1",
        )
    ] == [0, 1]
    assert restarted.write_timeline(
        session_id="session/one",
        task_id="task-1",
    ).exists()


def test_feedback_is_classified_and_persisted_as_bad_case(tmp_path: Path):
    trace = TraceRecorder(tmp_path / "trace")
    tool_failure = trace.record(
        event_type="tool_failed",
        producer="runtime",
        session_id="session-1",
        task_id="task-1",
        payload={"tool": "search", "error": "offline"},
    )
    store = SQLiteFeedbackStore(tmp_path / "feedback.db")
    try:
        bad_case = store.add(
            FeedbackRecord(
                feedback_id="feedback-1",
                session_id="session-1",
                task_id="task-1",
                source=FeedbackSource.TOOL_FAILURE,
                text="retry failed",
            ),
            trace_events=(tool_failure,),
        )
        duplicate = store.add(
            FeedbackRecord(
                feedback_id="feedback-2",
                session_id="session-1",
                task_id="task-1",
                source=FeedbackSource.DELIVERY_FAILURE,
                metadata={"failure_kind": "duplicate delivery"},
            ),
        )

        assert bad_case is not None
        assert bad_case.category == BadCaseCategory.TOOL_FAILURE
        assert bad_case.trace_event_ids == (tool_failure.trace_event_id,)
        assert duplicate is not None
        assert duplicate.category == BadCaseCategory.DUPLICATE_DELIVERY
        assert [case.category for case in store.list_bad_cases()] == [
            BadCaseCategory.TOOL_FAILURE,
            BadCaseCategory.DUPLICATE_DELIVERY,
        ]
        revision = store.revise_bad_case(
            bad_case.bad_case_id,
            category=BadCaseCategory.RECOVERY_FAILURE,
            confidence=0.98,
            reason="human review found recovery failure",
            reviewer="reviewer-1",
        )
        assert revision.revision == 1
        assert store.list_bad_case_revisions(bad_case.bad_case_id) == [revision]
    finally:
        store.close()


def test_trace_to_replay_to_report_and_regression_comparison(tmp_path: Path):
    trace = TraceRecorder(tmp_path / "trace")
    inbound_event = trace.record(
        event_type="inbound_received",
        producer="gateway",
        producer_version="gateway-v2",
        session_id="session-1",
        task_id="task-1",
        payload={"text": "prepare report"},
    )
    trace.record(
        event_type="tool_result",
        producer="runtime",
        producer_version="runtime-v3",
        session_id="session-1",
        task_id="task-1",
        payload={"tool": "report", "result": "ok"},
    )
    events = trace.read_events(session_id="session-1", task_id="task-1")
    observation = ReplayObservation(
        route={"agent_id": "main", "session_id": "session-1"},
        states=("queued", "running", "succeeded"),
        task_completed=True,
        deliveries=(
            {
                "delivery_id": "delivery-1",
                "idempotency_key": "intent-1:0",
                "lane_key": "session-1",
                "sequence": 0,
            },
            {
                "delivery_id": "delivery-2",
                "idempotency_key": "intent-1:1",
                "lane_key": "session-1",
                "sequence": 1,
            },
        ),
        rendered_messages=({"text": "done"},),
        metrics={
            "latency_ms": 120,
            "token_count": 80,
            "attempt_count": 1,
        },
    )
    case = ReplayCaseRecorder().capture(
        case_id="case-1",
        inbound={
            "schema_version": "inbound_envelope.v1",
            "text": "prepare report",
        },
        trace_events=events,
        observation=observation,
        channel_capability={"text_limit": 80},
        expected={
            "route": {"agent_id": "main", "session_id": "session-1"},
            "required_states": ["queued", "running", "succeeded"],
            "task_completed": True,
            "max_latency_ms": 500,
            "max_token_count": 200,
            "max_attempt_count": 2,
        },
    )
    case_path = tmp_path / "cases" / "case-1.json"
    case.save(case_path)
    loaded = ReplayCase.load(case_path)

    baseline = ReplayRunner().run(loaded, mode=ReplayMode.GATEWAY_ONLY)
    assert baseline.passed is True
    assert baseline.score == 1.0
    assert f"trace-event://{inbound_event.trace_event_id}" in (loaded.source_trace_refs)
    json_report, markdown_report = ReplayRunner.write_report(
        baseline,
        tmp_path / "reports",
    )
    assert json.loads(json_report.read_text(encoding="utf-8"))["passed"] is True
    assert "Replay Report" in markdown_report.read_text(encoding="utf-8")

    candidate_case = ReplayCase(
        case_id="case-1-candidate",
        inbound=loaded.inbound,
        expected=loaded.expected,
        channel_capability=loaded.channel_capability,
        evaluators=loaded.evaluators,
        recorded_observation={
            **observation.to_dict(),
            "deliveries": [
                {
                    "delivery_id": "delivery-2",
                    "idempotency_key": "duplicate",
                    "lane_key": "session-1",
                    "sequence": 1,
                },
                {
                    "delivery_id": "delivery-1",
                    "idempotency_key": "duplicate",
                    "lane_key": "session-1",
                    "sequence": 0,
                },
            ],
            "metrics": {
                "latency_ms": 900,
                "token_count": 300,
                "attempt_count": 4,
            },
        },
    )
    candidate = ReplayRunner().run(candidate_case)
    comparison = ReplayRunner.compare(baseline, candidate)

    assert candidate.passed is False
    assert comparison["regressed"] is True
    assert comparison["score_delta"] < 0


def test_failed_trace_can_become_replay_case_and_report(tmp_path: Path):
    trace = TraceRecorder(tmp_path / "trace")
    trace.record(
        event_type="inbound_received",
        producer="gateway",
        producer_version="gateway-v2",
        session_id="session-failed",
        task_id="task-failed",
        payload={"text": "prepare report"},
    )
    failed_event = trace.record(
        event_type="tool_failed",
        producer="runtime",
        producer_version="runtime-v3",
        session_id="session-failed",
        task_id="task-failed",
        payload={
            "tool": "report",
            "error_type": "transient",
            "message": "upstream timeout",
        },
    )
    trace.record(
        event_type="task_failed",
        producer="interaction",
        producer_version="interaction-v1",
        session_id="session-failed",
        task_id="task-failed",
        payload={"reason": "tool retry budget exhausted"},
    )
    events = trace.read_events(
        session_id="session-failed",
        task_id="task-failed",
    )
    observation = ReplayObservation(
        route={"agent_id": "main", "session_id": "session-failed"},
        states=("queued", "running", "failed"),
        task_completed=False,
        tool_results=(
            {
                "tool": "report",
                "status": "failed",
                "error_type": "transient",
            },
        ),
    )
    case = ReplayCaseRecorder().capture(
        case_id="failed-case",
        inbound={
            "schema_version": "inbound_envelope.v1",
            "text": "prepare report",
        },
        trace_events=events,
        observation=observation,
        expected={
            "required_states": ["queued", "running", "failed"],
            "task_completed": False,
        },
        evaluators=("state", "completion"),
    )

    assert f"trace-event://{failed_event.trace_event_id}" in case.source_trace_refs
    assert case.tool_recordings[failed_event.trace_event_id]["error_type"] == "transient"

    report = ReplayRunner().run(case, mode=ReplayMode.AGENT_STUB_TOOLS)
    json_report, markdown_report = ReplayRunner.write_report(
        report,
        tmp_path / "reports",
    )

    assert report.passed is True
    assert json.loads(json_report.read_text(encoding="utf-8"))["case_id"] == "failed-case"
    assert "PASS" in markdown_report.read_text(encoding="utf-8")
