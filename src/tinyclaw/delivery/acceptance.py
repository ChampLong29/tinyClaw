"""Offline deployment acceptance drill for durable delivery semantics."""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from tinyclaw.contracts import DeliveryState
from tinyclaw.contracts._common import utc_now
from tinyclaw.delivery.durable import DurableDeliveryQueue
from tinyclaw.delivery.retry import DeliveryRetryPolicy
from tinyclaw.delivery.store import SQLiteDeliveryStore
from tinyclaw.delivery.worker import (
    DeliveryFaultPoint,
    DeliveryReceipt,
    InjectedDeliveryCrash,
    LeaseDeliveryWorker,
)
from tinyclaw.presentation import CapabilityRegistry, ChannelCapability, OutboundRenderer


@dataclass(frozen=True, kw_only=True)
class DrillScenarioResult:
    name: str
    passed: bool
    details: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "details": dict(self.details),
            "error": self.error,
        }


@dataclass(frozen=True, kw_only=True)
class DeliveryDrillReport:
    scenarios: tuple[DrillScenarioResult, ...]
    generated_at: str = field(default_factory=lambda: utc_now().isoformat())
    schema_version: str = "delivery_drill_report.v1"

    @property
    def passed(self) -> bool:
        return all(scenario.passed for scenario in self.scenarios)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "passed": self.passed,
            "scenarios": [scenario.to_dict() for scenario in self.scenarios],
        }

    def save(self, path: Path | str) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return target


class _IdempotentProbeSender:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.accepted: dict[str, str] = {}

    def send(self, record) -> DeliveryReceipt:
        self.calls.append(record.idempotency_key)
        platform_id = self.accepted.setdefault(
            record.idempotency_key,
            f"platform:{len(self.accepted) + 1}",
        )
        return DeliveryReceipt(platform_message_id=platform_id)


class _AtLeastOnceProbeSender:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def send(self, record) -> DeliveryReceipt:
        self.calls.append(record.idempotency_key)
        return DeliveryReceipt(platform_message_id=f"platform-attempt:{len(self.calls)}")


class _TransientFailureSender:
    def __init__(self, fail_key: str) -> None:
        self.fail_key = fail_key
        self.calls: list[str] = []
        self.failed = False

    def send(self, record) -> DeliveryReceipt:
        self.calls.append(record.idempotency_key)
        if record.idempotency_key == self.fail_key and not self.failed:
            self.failed = True
            raise TimeoutError("injected transient network timeout")
        return DeliveryReceipt(platform_message_id=f"platform:{record.idempotency_key}")


def _enqueue_pair(queue: DurableDeliveryQueue, *, channel: str) -> tuple[str, str]:
    common = {
        "account_id": "acceptance-bot",
        "lane_key": "acceptance-session",
        "session_id": "acceptance-session",
    }
    first = queue.enqueue(
        channel,
        "acceptance-user",
        "first",
        {**common, "intent_id": "intent-1", "idempotency_key": "key-1"},
    )
    second = queue.enqueue(
        channel,
        "acceptance-user",
        "second",
        {**common, "intent_id": "intent-2", "idempotency_key": "key-2"},
    )
    return first, second


def _crash_once_at(point_to_crash: DeliveryFaultPoint):
    crashed = False

    def inject(point, _record, _context) -> None:
        nonlocal crashed
        if point == point_to_crash and not crashed:
            crashed = True
            raise InjectedDeliveryCrash(point.value)

    return inject


def _lease_recovery_fifo(root: Path) -> Mapping[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / "delivery.db"
    now = utc_now()
    sender = _IdempotentProbeSender()
    store = SQLiteDeliveryStore(db_path)
    queue = DurableDeliveryQueue(store)
    first_id, second_id = _enqueue_pair(queue, channel="acceptance")
    try:
        worker = LeaseDeliveryWorker(
            store=store,
            sender=sender,
            worker_id="before-crash",
            lease_duration=timedelta(seconds=5),
            fault_injector=_crash_once_at(DeliveryFaultPoint.AFTER_CLAIM),
        )
        try:
            worker.run_once(now=now)
        except InjectedDeliveryCrash:
            pass
        assert store.get(first_id).state == DeliveryState.IN_FLIGHT
        assert store.get(second_id).state == DeliveryState.PENDING
    finally:
        store.close()

    reopened = SQLiteDeliveryStore(db_path)
    try:
        recovered = LeaseDeliveryWorker(
            store=reopened,
            sender=sender,
            worker_id="after-crash",
            lease_duration=timedelta(seconds=5),
        )
        first = recovered.run_once(now=now + timedelta(seconds=6))
        second = recovered.run_once(now=now + timedelta(seconds=6))
        assert [record.delivery_id for record in first] == [first_id]
        assert [record.delivery_id for record in second] == [second_id]
        assert sender.calls == ["key-1:0", "key-2:0"]
        return {"send_order": sender.calls, "final_state": second[0].state.value}
    finally:
        reopened.close()


def _idempotent_ack_loss(root: Path) -> Mapping[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / "delivery.db"
    now = utc_now()
    sender = _IdempotentProbeSender()
    store = SQLiteDeliveryStore(db_path)
    renderer = OutboundRenderer(
        CapabilityRegistry({"acceptance-idempotent": ChannelCapability(outbound_idempotency=True)})
    )
    queue = DurableDeliveryQueue(store, renderer=renderer)
    first_id, _ = _enqueue_pair(queue, channel="acceptance-idempotent")
    try:
        worker = LeaseDeliveryWorker(
            store=store,
            sender=sender,
            worker_id="before-crash",
            lease_duration=timedelta(seconds=5),
            fault_injector=_crash_once_at(DeliveryFaultPoint.AFTER_SEND_BEFORE_SETTLE),
        )
        try:
            worker.run_once(now=now)
        except InjectedDeliveryCrash:
            pass
        assert store.get(first_id).state == DeliveryState.IN_FLIGHT
        semantics = store.get(first_id).payload["meta"]["delivery_semantics"]
        assert semantics == "idempotent_retry"
    finally:
        store.close()

    reopened = SQLiteDeliveryStore(db_path)
    try:
        recovered = LeaseDeliveryWorker(
            store=reopened,
            sender=sender,
            worker_id="after-crash",
            lease_duration=timedelta(seconds=5),
        ).run_once(now=now + timedelta(seconds=6))
        assert recovered[0].state == DeliveryState.ACKED
        assert sender.calls == ["key-1:0", "key-1:0"]
        assert len(sender.accepted) == 1
        return {
            "attempt_count": len(sender.calls),
            "platform_accept_count": len(sender.accepted),
            "delivery_semantics": semantics,
        }
    finally:
        reopened.close()


def _at_least_once_ack_loss(root: Path) -> Mapping[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / "delivery.db"
    now = utc_now()
    sender = _AtLeastOnceProbeSender()
    store = SQLiteDeliveryStore(db_path)
    queue = DurableDeliveryQueue(store)
    first_id, _ = _enqueue_pair(queue, channel="dingtalk")
    try:
        worker = LeaseDeliveryWorker(
            store=store,
            sender=sender,
            worker_id="before-crash",
            lease_duration=timedelta(seconds=5),
            fault_injector=_crash_once_at(DeliveryFaultPoint.AFTER_SEND_BEFORE_SETTLE),
        )
        try:
            worker.run_once(now=now)
        except InjectedDeliveryCrash:
            pass
        semantics = store.get(first_id).payload["meta"]["delivery_semantics"]
        assert semantics == "at_least_once"
    finally:
        store.close()

    reopened = SQLiteDeliveryStore(db_path)
    try:
        recovered = LeaseDeliveryWorker(
            store=reopened,
            sender=sender,
            worker_id="after-crash",
            lease_duration=timedelta(seconds=5),
        ).run_once(now=now + timedelta(seconds=6))
        assert recovered[0].state == DeliveryState.ACKED
        assert sender.calls == ["key-1:0", "key-1:0"]
        return {
            "attempt_count": len(sender.calls),
            "platform_accept_count": len(sender.calls),
            "delivery_semantics": semantics,
            "duplicate_risk_observed": True,
        }
    finally:
        reopened.close()


def _retry_wait_preserves_fifo(root: Path) -> Mapping[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    store = SQLiteDeliveryStore(root / "delivery.db")
    queue = DurableDeliveryQueue(store)
    first_id, second_id = _enqueue_pair(queue, channel="acceptance")
    sender = _TransientFailureSender("key-1:0")
    worker = LeaseDeliveryWorker(
        store=store,
        sender=sender,
        worker_id="retry-worker",
        retry_policy=DeliveryRetryPolicy(
            delays_seconds=(5,),
            jitter_ratio=0,
            max_attempts=3,
        ),
    )
    try:
        failed = worker.run_once(now=now)
        blocked = worker.run_once(now=now + timedelta(seconds=1))
        first = worker.run_once(now=now + timedelta(seconds=6))
        second = worker.run_once(now=now + timedelta(seconds=6))
        assert failed[0].delivery_id == first_id
        assert failed[0].state == DeliveryState.RETRY_WAIT
        assert blocked == []
        assert first[0].delivery_id == first_id
        assert second[0].delivery_id == second_id
        assert sender.calls == ["key-1:0", "key-1:0", "key-2:0"]
        return {
            "send_order": sender.calls,
            "head_blocked_during_backoff": True,
            "final_states": [first[0].state.value, second[0].state.value],
        }
    finally:
        store.close()


_SCENARIOS: tuple[tuple[str, Callable[[Path], Mapping[str, Any]]], ...] = (
    ("lease_recovery_fifo", _lease_recovery_fifo),
    ("idempotent_ack_loss", _idempotent_ack_loss),
    ("at_least_once_ack_loss", _at_least_once_ack_loss),
    ("retry_wait_preserves_fifo", _retry_wait_preserves_fifo),
)


def run_offline_delivery_drill(workspace: Path | str) -> DeliveryDrillReport:
    """Run deterministic delivery drills without contacting external platforms."""

    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)
    results: list[DrillScenarioResult] = []
    for name, scenario in _SCENARIOS:
        try:
            details = scenario(root / name)
        except Exception as exc:
            results.append(
                DrillScenarioResult(
                    name=name,
                    passed=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
        else:
            results.append(DrillScenarioResult(name=name, passed=True, details=details))
    return DeliveryDrillReport(scenarios=tuple(results))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the offline durable-delivery deployment acceptance drill.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        help="Keep drill SQLite files in this directory; defaults to a temporary directory.",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON report path.")
    args = parser.parse_args(argv)

    if args.workspace is None:
        with tempfile.TemporaryDirectory(prefix="tinyclaw-delivery-drill-") as directory:
            report = run_offline_delivery_drill(directory)
    else:
        report = run_offline_delivery_drill(args.workspace)

    if args.output is not None:
        report.save(args.output)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
