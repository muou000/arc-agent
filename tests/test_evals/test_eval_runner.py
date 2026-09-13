"""End-to-end tests for the A/B eval harness against a fake runner script.

The fake runner (``fake_eval_runner.py``) is invoked through the same
subprocess contract as ``arc_main.py compile`` and fabricates a workspace with
runner events and a processing queue, so the full artifacts pipeline
(runs.jsonl, sessions snapshots, report.json/report.txt, workspace cleanup)
runs without any model access.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from core.evals import ArmConfig, eval_table

import pytest

FAKE_RUNNER = Path(__file__).parent / "fake_eval_runner.py"


def _make_requirement(tmp_path: Path) -> Path:
    requirement = tmp_path / "req"
    requirement.mkdir(exist_ok=True)
    (requirement / "requirements.yaml").write_text("root:\n  id: root\n", encoding="utf-8")
    return requirement


def _arms() -> tuple[ArmConfig, ArmConfig]:
    baseline = ArmConfig(
        label="no-skill",
        env={
            "EVAL_FAKE_TOKENS": "10000",
            "EVAL_FAKE_NODES": "n1:PASSED,n2:FAILED",
            "EVAL_FAKE_EXIT": "1",
            "EVAL_FAKE_SLEEP": "0.2",
        },
    )
    candidate = ArmConfig(
        label="with-skill",
        env={
            "EVAL_FAKE_TOKENS": "9000",
            "EVAL_FAKE_NODES": "n1:PASSED,n2:PASSED",
            "EVAL_FAKE_SLEEP": "0.02",
        },
    )
    return baseline, candidate


def test_eval_table_end_to_end(tmp_path):
    artifacts = tmp_path / "artifacts"
    work = tmp_path / "work"
    result = eval_table(
        "fake ab",
        *_arms(),
        requirement_path=_make_requirement(tmp_path),
        repetitions=2,
        runner_command=[sys.executable, str(FAKE_RUNNER)],
        artifacts_dir=artifacts,
        work_root=work,
        log=lambda _message: None,
    )
    report = result.report
    comparison = report["comparison"]
    assert report["schema"] == "arc.eval.report/1"
    assert report["set_name"] == "fake ab"
    assert comparison["pairs"] == 2
    assert comparison["pass_rate"] == {
        "baseline": 0.0,
        "candidate": 100.0,
        "delta_pp": 100.0,
        "n": {"baseline": 2, "candidate": 2},
    }
    assert comparison["tokens"]["baseline"] == 10000.0
    assert comparison["tokens"]["candidate"] == 9000.0
    assert comparison["tokens"]["delta"] == -1000.0
    assert comparison["est_cost"]["baseline"] == pytest.approx(0.02)
    assert comparison["est_cost"]["delta"] == pytest.approx(-0.002)
    assert comparison["latency_ms"]["baseline"] > comparison["latency_ms"]["candidate"] > 0

    # runs.jsonl: one record per run, in baseline-then-candidate interleaved order
    run_lines = (artifacts / "runs.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(run_lines) == 4
    first = json.loads(run_lines[0])
    assert first["run_id"] == "baseline-rep001"
    assert first["arm"] == "baseline"
    assert first["label"] == "no-skill"
    assert first["repetition"] == 1
    assert first["exit_code"] == 1
    assert first["passed"] is False
    assert first["nodes_failed"] == 1
    assert first["env"]["EVAL_FAKE_TOKENS"] == "10000"
    last = json.loads(run_lines[-1])
    assert last["run_id"] == "candidate-rep002"
    assert last["passed"] is True
    assert last["usage"]["total_tokens"] == 9000

    # report.json / report.txt
    assert json.loads((artifacts / "report.json").read_text(encoding="utf-8")) == report
    text = (artifacts / "report.txt").read_text(encoding="utf-8")
    assert "Eval Comparisons" in text
    assert "    Candidate  with-skill (2/2 pairs)" in text
    assert "    Pass rate  +100.0 pp (candidate 100.0%, baseline 0.0%)" in text
    assert "       Tokens  -1000.0 (candidate 9000.0, baseline 10000.0)" in text

    # session evidence snapshotted before workspace cleanup
    snapshot = artifacts / "sessions" / "candidate-rep001"
    assert (snapshot / ".arc" / "runner-events.jsonl").exists()
    assert (snapshot / ".arc" / "processing_queue.json").exists()
    assert (snapshot / "console.log").exists()

    # throwaway workspaces are gone
    assert not (work / "baseline-rep001").exists()
    assert not (work / "candidate-rep002").exists()


def test_eval_table_keep_workspaces(tmp_path):
    work = tmp_path / "work"
    eval_table(
        "keep",
        ArmConfig(label="b"),
        ArmConfig(label="c"),
        requirement_path=_make_requirement(tmp_path),
        repetitions=1,
        runner_command=[sys.executable, str(FAKE_RUNNER)],
        artifacts_dir=tmp_path / "artifacts",
        work_root=work,
        keep_workspaces=True,
        log=lambda _message: None,
    )
    assert (work / "baseline-rep001" / ".arc" / "runner-events.jsonl").exists()
    assert (work / "candidate-rep001" / ".arc" / "runner-events.jsonl").exists()


def test_eval_table_survives_runner_launch_failure(tmp_path):
    result = eval_table(
        "broken runner",
        ArmConfig(label="b"),
        ArmConfig(label="c"),
        requirement_path=_make_requirement(tmp_path),
        repetitions=1,
        # a missing *executable* (not a missing script argument) raises
        # FileNotFoundError at spawn time on every platform
        runner_command=[str(tmp_path / "no_such_interpreter.exe"), "compile"],
        artifacts_dir=tmp_path / "artifacts",
        work_root=tmp_path / "work",
        log=lambda _message: None,
    )
    run = result.runs[0]
    assert run["exit_code"] is None
    assert "runner not executable" in run["error"]
    assert run["passed"] is False
    assert run["usage"] is None
    # report is still produced with all deltas unavailable
    comparison = result.report["comparison"]
    assert comparison["pairs"] == 1
    assert comparison["tokens"]["delta"] is None
    text = (tmp_path / "artifacts" / "report.txt").read_text(encoding="utf-8")
    assert text.count("unavailable (missing telemetry)") == 2  # tokens and cost


def test_eval_table_records_timeout(tmp_path):
    result = eval_table(
        "timeout",
        ArmConfig(label="slow", env={"EVAL_FAKE_SLEEP": "30"}),
        ArmConfig(label="fast"),
        requirement_path=_make_requirement(tmp_path),
        repetitions=1,
        runner_command=[sys.executable, str(FAKE_RUNNER)],
        artifacts_dir=tmp_path / "artifacts",
        work_root=tmp_path / "work",
        timeout_seconds=0.5,
        log=lambda _message: None,
    )
    slow = result.runs[0]
    assert slow["exit_code"] is None
    assert slow["error"] == "timed out after 0.5s"
    assert slow["passed"] is False
    fast = result.runs[1]
    assert fast["exit_code"] == 0
    assert fast["passed"] is True


def test_eval_table_rejects_bad_inputs(tmp_path):
    with pytest.raises(ValueError, match="repetitions"):
        eval_table(
            "x",
            ArmConfig(label="b"),
            ArmConfig(label="c"),
            requirement_path=_make_requirement(tmp_path),
            repetitions=0,
            artifacts_dir=tmp_path / "a1",
        )
    with pytest.raises(FileNotFoundError, match="requirement"):
        eval_table(
            "x",
            ArmConfig(label="b"),
            ArmConfig(label="c"),
            requirement_path=tmp_path / "missing",
            artifacts_dir=tmp_path / "a2",
        )
    with pytest.raises(ValueError, match="label"):
        eval_table(
            "x",
            ArmConfig(label="  "),
            ArmConfig(label="c"),
            requirement_path=_make_requirement(tmp_path),
            artifacts_dir=tmp_path / "a3",
        )
