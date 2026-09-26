#!/usr/bin/env python3
"""Report atomic-requirement coverage of an independent proxy test directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any

import yaml


COVERS_RE = re.compile(r"covers:\s*([^\r\n]+)")


def atomic_requirement_ids(requirements_dir: Path) -> list[str]:
    document = yaml.safe_load((requirements_dir / "requirements.yaml").read_text(encoding="utf-8"))
    found: list[str] = []

    def visit(node: dict[str, Any]) -> None:
        if node.get("type") == "ATOMIC" and node.get("id"):
            found.append(str(node["id"]))
        for child in node.get("children", []) or []:
            if isinstance(child, dict):
                visit(child)

    visit(document)
    return sorted(set(found))


def covered_requirement_ids(tests_dir: Path) -> dict[str, list[str]]:
    covered_by_file: dict[str, list[str]] = {}
    for path in sorted(tests_dir.rglob("*.spec.ts")):
        ids = sorted({item for group in COVERS_RE.findall(path.read_text(encoding="utf-8")) for item in group.split()})
        if ids:
            covered_by_file[path.name] = ids
    return covered_by_file


def build_report(requirements_dir: Path, tests_dir: Path) -> dict[str, Any]:
    atomic = atomic_requirement_ids(requirements_dir)
    by_file = covered_requirement_ids(tests_dir)
    covered = sorted({item for ids in by_file.values() for item in ids})
    known = set(atomic)
    covered_known = sorted(set(covered) & known)
    return {
        "requirements_dir": str(requirements_dir.resolve()),
        "tests_dir": str(tests_dir.resolve()),
        "atomic_total": len(atomic),
        "covered_atomic": len(covered_known),
        "coverage_percent": round((len(covered_known) / len(atomic)) * 100, 1) if atomic else 0.0,
        "covered_ids": covered_known,
        "missing_ids": sorted(known - set(covered_known)),
        "unknown_covered_ids": sorted(set(covered) - known),
        "files": by_file,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements-dir", required=True, type=Path)
    parser.add_argument("--tests-dir", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = build_report(args.requirements_dir.resolve(), args.tests_dir.resolve())
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"Covered {report['covered_atomic']}/{report['atomic_total']} atomic requirements ({report['coverage_percent']}%).")
        print("Covered IDs: " + (", ".join(report["covered_ids"]) or "none"))
        print("Missing IDs: " + (", ".join(report["missing_ids"]) or "none"))
        if report["unknown_covered_ids"]:
            print("Unknown covered IDs: " + ", ".join(report["unknown_covered_ids"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
