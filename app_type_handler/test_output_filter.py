"""Feature-based noise filter for web test command output (#216).

The web test command runner used to truncate output over 4000 chars into
head 2000 + tail 2000, which destroyed mid-output failure evidence — in
arc-output-serial-4 the frontend STDERR's DOM dump (carrying the "Unable
to find a label" text that explained the failures) was cut out, and the
persisted ``.arc/tdd_runs`` log held the same truncated text while the
``ARC_RUN_OUTPUT_LOG`` pointer promised the complete output. The agent
then spent 36 minutes and 88 greps trying to reconstruct evidence that no
longer existed anywhere.

The fallback is now *no truncation*: information completeness first. Token
consumption is controlled by stripping formatting noise through the rule
table below. Rules are append-only — a new noise shape observed in a run
shows up in the per-execution filter footer (the observation window) as
"raw still large, little removed", and the fix is one more table row, not
a mechanical change.

Contract notes:

- Every rule must be provably content-free (pure terminal formatting). A
  rule that could remove diagnostic text does not belong here.
- The filtered text is the single artifact: what the model sees is what
  ``persist_run_output`` writes under ``.arc/tdd_runs``.
"""

from __future__ import annotations

import re

# (name, pattern). Each row strips one formatting-noise feature; counts are
# reported per rule name in the footer so a silent rule is visible.
_FILTER_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # ANSI CSI sequences (SGR color codes, cursor moves): pure terminal paint.
    ("ansi", re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")),
    # Carriage returns: Windows line-ending noise that survives ANSI strip.
    ("carriage-return", re.compile(r"\r")),
)

FOOTER_TAG = "[output-filter]"


def filter_output_noise(text: str) -> tuple[str, dict[str, int]]:
    """Strip formatting noise from one command output stream.

    Returns the filtered text and the number of characters each rule
    removed. Rules run in table order; each one only ever sees the text the
    previous rules left behind, so per-rule counts compose.
    """
    removed: dict[str, int] = {}
    for name, pattern in _FILTER_RULES:
        stripped = pattern.sub("", text)
        removed[name] = len(text) - len(stripped)
        text = stripped
    return text, removed


def render_filter_footer(raw_chars: int, removed: dict[str, int]) -> str:
    """One observation line: sizes, per-rule removals, no-truncation fact.

    The counts cover the two command output streams (stdout + stderr) before
    and after filtering — not the assembled result text, which adds the exit
    header and the ``STDOUT:``/``STDERR:`` scaffolding ("raw streams", not
    "raw output").

    The footer is model-visible on purpose — it tells the agent the output
    is complete (only formatting was dropped), closing the trust gap that
    made arc-output-serial-4's agent hunt for content it believed was cut.
    Rules that removed nothing are still listed so a silent rule (or a new
    noise shape none of the rules match) is observable run over run.
    """
    rule_parts = ", ".join(f"{name} -{chars}" for name, chars in removed.items())
    filtered_chars = raw_chars - sum(removed.values())
    return (
        f"{FOOTER_TAG} raw streams {raw_chars} -> {filtered_chars} chars "
        f"({rule_parts}); untruncated, formatting noise only."
    )
