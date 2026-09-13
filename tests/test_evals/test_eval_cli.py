"""Tests for the ``arc eval`` subcommand wiring (argument parsing, validation,
and the end-to-end CLI path against the fake runner)."""

from __future__ import annotations

import sys
from pathlib import Path

from arc_main import build_parser, cmd_eval

FAKE_RUNNER = Path(__file__).parent / "fake_eval_runner.py"


def _parse(argv: list[str]):
    return build_parser().parse_args(["eval", *argv])


def _make_requirement(tmp_path: Path) -> str:
    requirement = tmp_path / "req"
    requirement.mkdir(exist_ok=True)
    (requirement / "requirements.yaml").write_text("root:\n  id: root\n", encoding="utf-8")
    return str(requirement)


def test_eval_parser_defaults_and_flags():
    args = _parse(
        [
            "req",
            "--baseline-env",
            "ARC_AUTO_TDD_RETRY=0",
            "--candidate-env",
            "ARC_AUTO_TDD_RETRY=1",
            "--baseline-arg=--clean",
            "--label-candidate",
            "with-tdd",
            "--repetitions",
            "5",
            "--timeout",
            "1800",
            "--out-dir",
            "somewhere",
            "--keep-workspaces",
        ]
    )
    assert args.requirement_path == "req"
    assert args.baseline_env == ["ARC_AUTO_TDD_RETRY=0"]
    assert args.candidate_env == ["ARC_AUTO_TDD_RETRY=1"]
    # the "=" form lets an arm argument start with a dash
    assert args.baseline_arg == ["--clean"]
    assert args.candidate_arg == []
    assert args.label_baseline == "baseline"
    assert args.label_candidate == "with-tdd"
    assert args.repetitions == 5
    assert args.timeout == 1800.0
    assert args.out_dir == "somewhere"
    assert args.keep_workspaces is True
    assert args.runner_script is None
    assert args.func is cmd_eval


def test_cmd_eval_rejects_bad_env_override(tmp_path, capsys):
    args = _parse(["req", "--baseline-env", "MISSING_EQUALS"])
    assert cmd_eval(args) == 2
    assert "KEY=VALUE" in capsys.readouterr().out


def test_cmd_eval_rejects_non_positive_repetitions(tmp_path):
    args = _parse(["req", "--repetitions", "0"])
    assert cmd_eval(args) == 2


def test_cmd_eval_rejects_missing_runner_script(tmp_path):
    args = _parse(
        [
            "req",
            "--runner-script",
            str(tmp_path / "missing.py"),
        ]
    )
    assert cmd_eval(args) == 2


def test_cmd_eval_end_to_end_with_fake_runner(tmp_path, capsys):
    out_dir = tmp_path / "artifacts"
    args = _parse(
        [
            _make_requirement(tmp_path),
            "--baseline-env",
            "EVAL_FAKE_TOKENS=10000",
            "--baseline-env",
            "EVAL_FAKE_EXIT=1",
            "--candidate-env",
            "EVAL_FAKE_TOKENS=8000",
            "--label-baseline",
            "no-skill",
            "--label-candidate",
            "with-skill",
            "--name",
            "skill lift",
            "--repetitions",
            "1",
            "--runner-script",
            str(FAKE_RUNNER),
            "--out-dir",
            str(out_dir),
            "--work-root",
            str(tmp_path / "work"),
        ]
    )
    assert cmd_eval(args) == 0
    out = capsys.readouterr().out
    assert "Eval Comparisons" in out
    assert "  skill lift" in out
    assert "    Candidate  with-skill (1/1 pairs)" in out
    assert "+100.0 pp" in out

    report = (out_dir / "report.json").read_text(encoding="utf-8")
    assert '"set_name": "skill lift"' in report
    assert (out_dir / "runs.jsonl").exists()


def test_cmd_eval_produces_report_when_all_runs_fail(tmp_path, capsys):
    # the runner executes but every run exits nonzero: the harness still writes
    # a full report (failed runs are observations, not harness failures)
    broken = tmp_path / "broken_runner.py"
    broken.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    args = _parse(
        [
            _make_requirement(tmp_path),
            "--runner-script",
            str(broken),
            "--out-dir",
            str(tmp_path / "artifacts"),
            "--work-root",
            str(tmp_path / "work"),
        ]
    )
    assert cmd_eval(args) == 0
    out = capsys.readouterr().out
    assert "Pass rate  +0.0 pp (candidate 0.0%, baseline 0.0%)" in out
    assert "unavailable (missing telemetry)" in out
