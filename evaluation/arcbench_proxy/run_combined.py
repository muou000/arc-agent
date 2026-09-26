#!/usr/bin/env python3
"""Run both hackathon tasks through the official local simulation wrapper."""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

DEFAULT_COST_PER_PASS = Decimal("0.4")
DEFAULT_GAMMA_REWARD = Decimal("0.1")
DEFAULT_GAMMA_PENALTY = Decimal("0.2")


def parse_decimal(value: str, name: str) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise SystemExit(f"{name} must be a decimal number: {value}") from exc
    if result < 0:
        raise SystemExit(f"{name} must be non-negative")
    return result


def combined_score(
    passed: int,
    total: int,
    cost: Decimal | None,
    *,
    cost_per_pass: Decimal = DEFAULT_COST_PER_PASS,
    reward_exponent: Decimal = DEFAULT_GAMMA_REWARD,
    penalty_exponent: Decimal = DEFAULT_GAMMA_PENALTY,
) -> dict[str, Any]:
    if total <= 0:
        return {
            "score": None,
            "pass_rate": None,
            "reasonable_cost": None,
            "exponent": None,
            "status": "unavailable",
        }
    pass_rate = (Decimal(passed) * Decimal("100")) / Decimal(total)
    reasonable_cost = cost_per_pass * Decimal(passed)
    if cost is None or cost <= 0 or passed <= 0:
        return {
            "score": None,
            "pass_rate": float(pass_rate),
            "reasonable_cost": float(reasonable_cost),
            "exponent": None,
            "status": "unavailable",
        }
    exponent = reward_exponent if cost <= reasonable_cost else penalty_exponent
    score = pass_rate / ((cost / reasonable_cost) ** exponent)
    return {
        "score": float(score),
        "pass_rate": float(pass_rate),
        "reasonable_cost": float(reasonable_cost),
        "exponent": float(exponent),
        "status": "measured",
    }


def run_task(args: argparse.Namespace, task: str, requirements: Path, tests: Path) -> dict[str, Any]:
    workspace = args.output_root / task
    if workspace.exists() and any(workspace.iterdir()):
        raise SystemExit(f"Output directory is not empty: {workspace}")
    command = [
        sys.executable,
        str(args.simulation_root / "local_submit.py"),
        "run",
        "--agent", str(args.agent),
        "--competition", "hackathon",
        "--task", task,
        "--requirements-dir", str(requirements),
        "--tests-dir", str(tests),
        "--output-dir", str(workspace),
        "--image", args.image,
        "--memory", args.memory,
        "--cpus", args.cpus,
    ]
    if args.env_file:
        command.extend(["--env-file", str(args.env_file)])
    if args.show_tests:
        command.append("--show-tests")
    completed = subprocess.run(command, check=False)
    result_path = workspace / "local-result.json"
    if not result_path.is_file():
        raise SystemExit(f"The official runner did not write {result_path} (exit={completed.returncode})")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["runner_exit_code"] = completed.returncode
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True, type=Path)
    parser.add_argument("--simulation-root", required=True, type=Path)
    parser.add_argument("--sheet-requirements", required=True, type=Path)
    parser.add_argument("--github-requirements", required=True, type=Path)
    parser.add_argument("--sheet-tests", type=Path, default=Path(__file__).parent / "tests" / "hackathon-sheet")
    parser.add_argument("--github-tests", type=Path, default=Path(__file__).parent / "tests" / "hackathon-github")
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--image", default="arcbench-local-submit:latest")
    parser.add_argument("--memory", default="2g")
    parser.add_argument("--cpus", default="1")
    parser.add_argument("--cost-per-pass", default=str(DEFAULT_COST_PER_PASS))
    parser.add_argument("--show-tests", action="store_true")
    args = parser.parse_args()

    args.simulation_root = args.simulation_root.resolve()
    args.agent = args.agent.resolve()
    args.output_root = args.output_root.resolve()
    args.output_root.mkdir(parents=True, exist_ok=True)
    cost_per_pass = parse_decimal(args.cost_per_pass, "--cost-per-pass")

    results = {
        "hackathon-sheet": run_task(args, "hackathon-sheet", args.sheet_requirements.resolve(), args.sheet_tests.resolve()),
        "hackathon-github": run_task(args, "hackathon-github", args.github_requirements.resolve(), args.github_tests.resolve()),
    }
    passed = sum(int(result.get("passed") or 0) for result in results.values())
    total = sum(int(result.get("total") or 0) for result in results.values())
    costs = [result.get("token_cost") for result in results.values()]
    total_cost = sum((Decimal(str(cost)) for cost in costs if cost is not None), Decimal("0"))
    cost_available = all(cost is not None for cost in costs)
    unmeasured_tasks = [
        task for task, result in results.items()
        if result.get("evaluation_status") != "completed" or not int(result.get("total") or 0)
    ]
    aggregate = {
        "tasks": results,
        "passed": passed,
        "total": total,
        "failed": total - passed,
        "evaluation_status": "completed" if not unmeasured_tasks else "failed",
        "unmeasured_tasks": unmeasured_tasks,
        "token_cost": float(total_cost) if cost_available else None,
        "score": combined_score(passed, total, total_cost if cost_available else None, cost_per_pass=cost_per_pass),
        "note": "Proxy score only; official hidden-test count and platform aggregation may differ.",
    }
    output_path = args.output_root / "combined-result.json"
    output_path.write_text(json.dumps(aggregate, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(aggregate, indent=2, ensure_ascii=False))
    successful = all(
        result.get("evaluation_status") == "completed"
        and int(result.get("runner_exit_code") or 0) == 0
        and int(result.get("failed") or 0) == 0
        for result in results.values()
    )
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
