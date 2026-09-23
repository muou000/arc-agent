"""Rendered tool-result texts shared by consumers' contract tests.

The mixed/all-zero build transcriptions are the exact shapes the
multi-segment exit-code fix (#176) judges: both the write-lock discipline
(``_tool_result_failed``) and the tool-usage observation must classify them
identically, so the texts are pinned in one place instead of drifting apart
between the two consumers' test files.
"""

MIXED_BUILD_RESULT = (
    "=== Frontend Build Result ===\n"
    "Exit Code: 1\n"
    "STDOUT:\n"
    "error TS2304: Cannot find name 'x'.\n"
    "\n"
    "=== Backend Build Result ===\n"
    "Exit Code: 0\n"
)

ALL_ZERO_BUILD_RESULT = (
    "=== Frontend Build Result ===\n"
    "Exit Code: 0\n"
    "\n"
    "=== Backend Build Result ===\n"
    "Exit Code: 0\n"
)
