"""Create stable replay cases from normalized input and recorded traces."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from tinyclaw.contracts import TraceEvent
from tinyclaw.replay.schema import ReplayCase, ReplayObservation


class ReplayCaseRecorder:
    def capture(
        self,
        *,
        inbound: Mapping[str, Any],
        trace_events: Iterable[TraceEvent],
        observation: ReplayObservation,
        identity_policy: Mapping[str, Any] | None = None,
        channel_capability: Mapping[str, Any] | None = None,
        expected: Mapping[str, Any] | None = None,
        evaluators: tuple[str, ...] | None = None,
        case_id: str | None = None,
    ) -> ReplayCase:
        events = tuple(trace_events)
        versions = {
            event.producer: event.producer_version
            for event in events
            if event.producer_version != "unknown"
        }
        tool_recordings = {
            event.trace_event_id: event.payload
            for event in events
            if event.event_type in {"tool_result", "tool_failed"}
        }
        source_refs = tuple(
            dict.fromkeys(
                [
                    *(f"trace-event://{event.trace_event_id}" for event in events),
                    *(artifact_ref for event in events for artifact_ref in event.artifact_refs),
                ]
            )
        )
        values: dict[str, Any] = {
            "inbound": inbound,
            "identity_policy": identity_policy or {},
            "versions": versions,
            "tool_recordings": tool_recordings,
            "channel_capability": channel_capability or {},
            "expected": expected or {},
            "source_trace_refs": source_refs,
            "recorded_observation": observation.to_dict(),
        }
        if evaluators is not None:
            values["evaluators"] = evaluators
        if case_id is not None:
            values["case_id"] = case_id
        return ReplayCase(**values)
