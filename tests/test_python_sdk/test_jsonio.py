"""Tests for ``arcbench_agent_runtime.jsonio`` helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcbench_agent_runtime.jsonio import append_jsonl, read_json, write_json_atomic


class TestAppendJsonl:
    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested" / "events.jsonl"
        append_jsonl(target, {"type": "signal", "reason": "x"})
        assert target.is_file()

    def test_writes_ascii_escaped_single_line(self, tmp_path: Path) -> None:
        target = tmp_path / "events.jsonl"
        payload = {"type": "signal", "reason": "测试"}
        append_jsonl(target, payload)
        content = target.read_text(encoding="utf-8")
        # ensure_ascii=True so non-ASCII is escaped
        assert "\\" in content  # escape present
        # exactly one line, terminated by newline
        assert content.endswith("\n")
        assert content.count("\n") == 1

    def test_appends_multiple_records(self, tmp_path: Path) -> None:
        target = tmp_path / "events.jsonl"
        for i in range(3):
            append_jsonl(target, {"i": i})
        lines = target.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        assert [json.loads(line)["i"] for line in lines] == [0, 1, 2]


class TestWriteJsonAtomic:
    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "data.json"
        write_json_atomic(target, {"a": 1})
        assert target.is_file()

    def test_preserves_non_ascii(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        write_json_atomic(target, {"name": "中文"})
        # ensure_ascii=False -> raw UTF-8 in output
        text = target.read_text(encoding="utf-8")
        assert "中文" in text

    def test_indent_is_two(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        write_json_atomic(target, {"a": 1, "b": 2})
        text = target.read_text(encoding="utf-8")
        # indent=2 means nested key is prefixed with "  "
        assert "\n  " in text

    def test_no_tmp_file_left_behind(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        write_json_atomic(target, {"a": 1})
        assert not target.with_suffix(".json.tmp").exists()
        assert not target.with_name(target.name + ".tmp").exists()

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        write_json_atomic(target, {"a": 1})
        write_json_atomic(target, {"a": 2})
        assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}


class TestReadJson:
    def test_missing_file_returns_default(self, tmp_path: Path) -> None:
        assert read_json(tmp_path / "missing.json", {"k": "v"}) == {"k": "v"}

    def test_corrupt_json_returns_default(self, tmp_path: Path) -> None:
        target = tmp_path / "broken.json"
        target.write_text("{ not valid json", encoding="utf-8")
        assert read_json(target, {"fallback": True}) == {"fallback": True}

    def test_non_dict_returns_default(self, tmp_path: Path) -> None:
        target = tmp_path / "list.json"
        target.write_text("[1, 2, 3]", encoding="utf-8")
        assert read_json(target, {"k": "v"}) == {"k": "v"}

    def test_valid_dict_returned(self, tmp_path: Path) -> None:
        target = tmp_path / "ok.json"
        target.write_text('{"k": "v"}', encoding="utf-8")
        assert read_json(target, {}) == {"k": "v"}

    def test_default_is_copied(self, tmp_path: Path) -> None:
        target = tmp_path / "missing.json"
        sentinel = {"k": "v"}
        result = read_json(target, sentinel)
        result["k"] = "mutated"
        # mutation must not bleed into the caller's default
        assert sentinel == {"k": "v"}