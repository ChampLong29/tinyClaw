import json
from pathlib import Path

from tinyclaw.delivery.acceptance import main, run_offline_delivery_drill


def test_offline_delivery_drill_passes_and_persists_report(tmp_path: Path):
    report = run_offline_delivery_drill(tmp_path / "drill")

    assert report.passed is True
    assert {scenario.name for scenario in report.scenarios} == {
        "lease_recovery_fifo",
        "idempotent_ack_loss",
        "at_least_once_ack_loss",
        "retry_wait_preserves_fifo",
    }
    assert all(scenario.passed for scenario in report.scenarios)

    report_path = report.save(tmp_path / "reports" / "delivery.json")
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == "delivery_drill_report.v1"
    assert saved["passed"] is True


def test_delivery_drill_cli_writes_machine_readable_report(tmp_path: Path, capsys):
    output = tmp_path / "delivery-report.json"

    exit_code = main(
        [
            "--workspace",
            str(tmp_path / "cli-drill"),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True
    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is True
