"""Built-in deterministic replay evaluators."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

from tinyclaw.replay.schema import (
    EvaluationOutcome,
    ReplayCase,
    ReplayObservation,
)

Evaluator = Callable[[ReplayCase, ReplayObservation], EvaluationOutcome]


def evaluate_route(
    case: ReplayCase,
    observation: ReplayObservation,
) -> EvaluationOutcome:
    expected = case.expected.get("route")
    passed = expected is None or dict(expected) == dict(observation.route)
    return _outcome(
        "route",
        passed,
        "route matched" if passed else "route mismatch",
        {"expected": expected, "actual": observation.route},
    )


def evaluate_state(
    case: ReplayCase,
    observation: ReplayObservation,
) -> EvaluationOutcome:
    required = tuple(case.expected.get("required_states") or ())
    required_present = _is_subsequence(required, observation.states)
    passed = required_present and not observation.invalid_transitions
    return _outcome(
        "state",
        passed,
        "state sequence valid" if passed else "state sequence invalid",
        {
            "required": required,
            "actual": observation.states,
            "invalid_transitions": observation.invalid_transitions,
        },
    )


def evaluate_interaction(
    case: ReplayCase,
    observation: ReplayObservation,
) -> EvaluationOutcome:
    expected_clarification = case.expected.get("clarification_requested")
    expected_confirmation = case.expected.get("confirmation_requested")
    clarification_ok = (
        expected_clarification is None
        or bool(expected_clarification) == observation.clarification_requested
    )
    confirmation_ok = (
        expected_confirmation is None
        or bool(expected_confirmation) == observation.confirmation_requested
    )
    passed = clarification_ok and confirmation_ok
    return _outcome(
        "interaction",
        passed,
        "interaction behavior matched" if passed else "clarification/confirmation mismatch",
        {
            "clarification": observation.clarification_requested,
            "confirmation": observation.confirmation_requested,
        },
    )


def evaluate_completion(
    case: ReplayCase,
    observation: ReplayObservation,
) -> EvaluationOutcome:
    expected = bool(case.expected.get("task_completed", True))
    passed = observation.task_completed == expected
    return _outcome(
        "completion",
        passed,
        "completion matched" if passed else "completion mismatch",
        {"expected": expected, "actual": observation.task_completed},
    )


def evaluate_delivery(
    case: ReplayCase,
    observation: ReplayObservation,
) -> EvaluationOutcome:
    seen: set[str] = set()
    duplicates: list[str] = []
    lane_sequences: dict[str, list[int]] = defaultdict(list)
    for delivery in observation.deliveries:
        identity = str(
            delivery.get("idempotency_key")
            or delivery.get("platform_message_id")
            or delivery.get("delivery_id")
            or ""
        )
        if identity and identity in seen:
            duplicates.append(identity)
        seen.add(identity)
        lane = str(delivery.get("lane_key") or "")
        if lane and delivery.get("sequence") is not None:
            lane_sequences[lane].append(int(delivery["sequence"]))
    out_of_order = {
        lane: values
        for lane, values in lane_sequences.items()
        if values != sorted(values) or len(values) != len(set(values))
    }
    max_duplicates = int(case.expected.get("max_duplicate_deliveries", 0))
    passed = len(duplicates) <= max_duplicates and not out_of_order
    return _outcome(
        "delivery",
        passed,
        "delivery order and uniqueness valid" if passed else "duplicate or out-of-order delivery",
        {"duplicates": duplicates, "out_of_order": out_of_order},
    )


def evaluate_rendering(
    case: ReplayCase,
    observation: ReplayObservation,
) -> EvaluationOutcome:
    limit = int(case.channel_capability.get("text_limit") or 4096)
    invalid: list[int] = []
    for index, message in enumerate(observation.rendered_messages):
        text = str(message.get("text") or "")
        if len(text) > limit or text.count("```") % 2:
            invalid.append(index)
    passed = not invalid
    return _outcome(
        "rendering",
        passed,
        "rendering valid" if passed else "rendering constraint violation",
        {"invalid_message_indexes": invalid, "text_limit": limit},
    )


def evaluate_notification(
    case: ReplayCase,
    observation: ReplayObservation,
) -> EvaluationOutcome:
    max_suppressed = int(case.expected.get("max_suppressed_notifications", 0))
    suppressed = [
        item
        for item in observation.notifications
        if str(item.get("kind") or "").lower() in {"suppressed", "deferred"}
    ]
    missing_reasons = [index for index, item in enumerate(suppressed) if not item.get("reason")]
    passed = len(suppressed) <= max_suppressed and not missing_reasons
    return _outcome(
        "notification",
        passed,
        "notification policy valid" if passed else "notification suppression exceeded expectation",
        {
            "suppressed_count": len(suppressed),
            "missing_reason_indexes": missing_reasons,
        },
    )


def evaluate_cost(
    case: ReplayCase,
    observation: ReplayObservation,
) -> EvaluationOutcome:
    limits: dict[str, float] = {
        "latency_ms": float(case.expected.get("max_latency_ms", float("inf"))),
        "token_count": float(case.expected.get("max_token_count", float("inf"))),
        "attempt_count": float(case.expected.get("max_attempt_count", float("inf"))),
    }
    exceeded = {
        name: {"actual": float(observation.metrics.get(name, 0)), "limit": limit}
        for name, limit in limits.items()
        if float(observation.metrics.get(name, 0)) > limit
    }
    passed = not exceeded
    return _outcome(
        "cost",
        passed,
        "cost within limits" if passed else "cost limit exceeded",
        {"exceeded": exceeded, "metrics": observation.metrics},
    )


EVALUATORS: dict[str, Evaluator] = {
    "route": evaluate_route,
    "state": evaluate_state,
    "interaction": evaluate_interaction,
    "completion": evaluate_completion,
    "delivery": evaluate_delivery,
    "rendering": evaluate_rendering,
    "notification": evaluate_notification,
    "cost": evaluate_cost,
}


def _outcome(
    evaluator: str,
    passed: bool,
    message: str,
    details: dict[str, Any],
) -> EvaluationOutcome:
    return EvaluationOutcome(
        evaluator=evaluator,
        passed=passed,
        score=1.0 if passed else 0.0,
        message=message,
        details=details,
    )


def _is_subsequence(required: tuple[str, ...], actual: tuple[str, ...]) -> bool:
    if not required:
        return True
    iterator = iter(actual)
    return all(any(value == candidate for candidate in iterator) for value in required)
