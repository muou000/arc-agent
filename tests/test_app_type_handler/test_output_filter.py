"""Web test command output is noise-filtered and never truncated (#216).

arc-output-serial-4 REQ-1: the 4000-char head+tail truncation destroyed the
frontend STDERR's mid-output DOM dump (the "Unable to find a label" evidence),
and the persisted ``.arc/tdd_runs`` log held the same truncated text while
``ARC_RUN_OUTPUT_LOG`` promised the complete output — the agent then spent 36
minutes / 88 greps reconstructing evidence that no longer existed. These
tests pin the new contract: formatting noise is stripped by the rule table,
everything else survives verbatim, and every execution carries a filter
footer that makes the removal observable.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from app_type_handler.backend_runtime import _execute_web_test_command
from app_type_handler.test_output_filter import (
    FOOTER_TAG,
    filter_output_noise,
    render_filter_footer,
)


def test_filter_output_noise_strips_ansi_and_carriage_returns() -> None:
    noisy = "\x1b[31mRED\x1b[0m plain\r\nsecond\r\n"
    filtered, removed = filter_output_noise(noisy)

    assert filtered == "RED plain\nsecond\n"
    assert removed["ansi"] == len("\x1b[31m") + len("\x1b[0m")
    assert removed["carriage-return"] == 2


def test_filter_output_noise_keeps_failure_evidence() -> None:
    # The incident shape: a Testing Library error whose diagnostic text must
    # survive; only the terminal paint around it may go.
    stderr = (
        "\x1b[31m\x1b[1mTestingLibraryElementError\x1b[39m\x1b[22m: "
        "Unable to find a label with the text of: 用户名\r\n"
        "\x1b[36m<body>\x1b[39m\n"
        "  \x1b[36m<div\x1b[39m\x1b[33m class=\x1b[39m\x1b[32m\"flex\"\x1b[39m\x1b[36m>\x1b[39m\n"
        "  \x1b[36m</body>\x1b[39m\r\n"
    )
    filtered, removed = filter_output_noise(stderr)

    assert "Unable to find a label with the text of: 用户名" in filtered
    assert '<body>' in filtered and '</body>' in filtered
    assert "\x1b[" not in filtered and "\r" not in filtered
    assert removed["ansi"] > 0


def test_render_filter_footer_lists_every_rule_including_silent_ones() -> None:
    footer = render_filter_footer(1000, {"ansi": 300, "carriage-return": 0})

    # A silent rule must stay visible: it is the observation window's signal
    # that a noise shape exists which no rule currently matches.
    assert footer.startswith(f"{FOOTER_TAG} raw streams 1000 -> 700 chars ")
    assert "(ansi -300, carriage-return -0)" in footer
    assert "untruncated, formatting noise only." in footer


def test_execute_web_test_command_untruncated_and_filtered(tmp_path: Path) -> None:
    # Rebuild the incident's geometry: an ~9k-char stream with a failure
    # marker sitting at char offset ~4.5k — deep inside the zone the old
    # head-2000/tail-2000 cut destroyed.
    lines = []
    for index in range(180):
        line = f"\x1b[32mline {index:03d}\x1b[0m some vitest output padding text\r\n"
        if index == 90:
            line = f"\x1b[31mMIDDLE-EVIDENCE-用户名\x1b[0m at offset ~4500\r\n"
        lines.append(line)
    script = tmp_path / "emit_output.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.buffer.write((" + repr("".join(lines)) + ").encode('utf-8'))\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stderr.buffer.write('\\x1b[31mstderr noise\\x1b[0m\\r\\n'.encode('utf-8'))\n"
        "sys.stderr.buffer.flush()\n",
        encoding="utf-8",
    )

    command = f'"{sys.executable}" "{script}"'
    result = asyncio.run(_execute_web_test_command(command, cwd=str(tmp_path)))

    text = result.text
    assert result.exit_code == 0
    assert "MIDDLE-EVIDENCE-用户名" in text  # the old cut zone survives verbatim
    assert "OUTPUT TRUNCATED" not in text
    assert "\x1b[" not in text and "\r" not in text
    assert text.rstrip("\n").endswith("untruncated, formatting noise only.")
    assert f"{FOOTER_TAG} raw streams " in text
    # stdout carried ~180 lines of noise plus a colored stderr line: the raw
    # count proves the full stream reached the footer accounting.
    raw_reported = int(text.split(f"{FOOTER_TAG} raw streams ")[1].split(" ")[0])
    assert raw_reported == sum(len(line) for line in lines) + len("\x1b[31mstderr noise\x1b[0m\r\n")


def test_execute_web_test_command_omits_footer_when_no_output(tmp_path: Path) -> None:
    script = tmp_path / "silent.py"
    script.write_text("pass\n", encoding="utf-8")

    command = f'"{sys.executable}" "{script}"'
    result = asyncio.run(_execute_web_test_command(command, cwd=str(tmp_path)))

    assert result.exit_code == 0
    assert FOOTER_TAG not in result.text
