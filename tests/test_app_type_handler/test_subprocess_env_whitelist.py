"""Build/test subprocesses must not inherit the full host environment (issue #180).

Generated-app code (agent-editable npm scripts, test files) runs inside the
build/test subprocesses. With ``env={**os.environ}`` (or an omitted ``env``,
which inherits implicitly) that code could read the host's model credentials
(``core.config.load_project_env`` copies the repository ``.env`` into
``os.environ``) and exfiltrate them through the command output that flows back
into the model context. Filesystem deny rules cannot stop environment reads,
so the agreed fix is a whitelist: a child process gets only the variables the
toolchains demonstrably need (``core.processes.build_subprocess_env``), plus
arc's own ``ARC_*`` runtime-contract namespace and the caller's explicit
extras.

The failure mode of a whitelist miss is a diagnosable build failure (add the
variable to the allowlist with a reason); the failure mode of a blacklist miss
is an undiagnosable credential leak. The nail tests below inject sensitive
needles into the host environment and assert, through real Python child
processes (no Node needed in the fast suite), that the needles never reach a
child's environment or the returned output while PATH and the port contract
do. An AST guard keeps every spawn site in ``app_type_handler`` on the
whitelist constructor.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shutil
import socket
import sys
from pathlib import Path

import pytest

from app_type_handler import android, backend_runtime, cli
from app_type_handler import web as web_handler
from core.processes import build_subprocess_env

REPO_ROOT = Path(__file__).resolve().parents[2]

# Values that must never reach a generated-app subprocess. They are injected
# explicitly (the autouse model-env scrub deletes them first, so setenv wins)
# to make the test independent of whether the host happens to have them.
SENSITIVE_NEEDLES: dict[str, str] = {
    "OPENAI_API_KEY": "sk-needle-secret-value",
    "VISUAL_API_KEY": "vis-needle-secret-value",
    "AWS_SECRET_ACCESS_KEY": "aws-needle-secret-value",
    "GITHUB_TOKEN": "gh-needle-secret-value",
}

# Shell-free child code that prints the child's environment as JSON.
_DUMP_ENV_CODE = "import os, json; print(json.dumps(dict(os.environ)))"


def _inject_needles(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in SENSITIVE_NEEDLES.items():
        monkeypatch.setenv(name, value)


def _parse_env_json(text: str) -> dict[str, str]:
    """Extract the JSON object a child printed into a command-result body."""

    match = re.search(r"\{.*\}", text, re.DOTALL)
    assert match, f"no environment JSON found in child output:\n{text}"
    return json.loads(match.group(0))


# --- The whitelist constructor itself ----------------------------------------


def test_sensitive_host_vars_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _inject_needles(monkeypatch)

    env = build_subprocess_env()

    for name in SENSITIVE_NEEDLES:
        assert name not in env
    assert not any("needle-secret-value" in value for value in env.values())


def test_runtime_basics_survive(monkeypatch: pytest.MonkeyPatch) -> None:
    env = build_subprocess_env()

    assert env.get("PATH") == os.environ.get("PATH")
    assert env.get("HOME") == os.environ.get("HOME")
    if os.name == "nt":
        for name in ("SystemRoot", "COMSPEC", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA"):
            assert env.get(name) == os.environ.get(name), name
    else:
        assert env.get("TMPDIR") == os.environ.get("TMPDIR")


def test_arc_contract_keys_pass_through_and_secret_shaped_names_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the enumerated ARC_* contract keys reach the child, never a wildcard.

    The runtime-contract names are explicit (issue review: a prefix wildcard
    would also auto-pass a future credential-shaped ARC_* variable); extras
    layered by the callers cover the per-attempt values.
    """

    monkeypatch.setenv("ARC_WEB_PORT", "4599")
    monkeypatch.setenv("ARC_MODEL_TIMEOUT", "600")
    monkeypatch.setenv("ARC_PROVIDER_API_KEY", "arc-needle-secret-value")

    env = build_subprocess_env()

    assert env.get("ARC_WEB_PORT") == "4599"
    assert "ARC_MODEL_TIMEOUT" not in env
    assert "ARC_PROVIDER_API_KEY" not in env
    assert not any("arc-needle-secret-value" in value for value in env.values())


def test_lowercase_host_contract_key_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract keys emit in canonical casing however the host spelled them."""

    monkeypatch.setenv("arc_web_port", "4599")

    env = build_subprocess_env()

    assert env.get("ARC_WEB_PORT") == "4599"


def test_extra_env_wins_over_host_and_whitelist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ARC_WEB_PORT", "4599")

    env = build_subprocess_env({"ARC_WEB_PORT": "3301", "PORT": "3301"})

    assert env.get("ARC_WEB_PORT") == "3301"
    assert env.get("PORT") == "3301"


def test_proxy_vars_are_emitted_under_both_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """npm/node/git read different cases; emit both when the host sets one.

    Either host casing feeds the emission: the lookup runs through the
    upper-cased host map, so a lowercase-only host (a common Linux default)
    still reaches children under both spellings.
    """

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")

    env = build_subprocess_env()

    assert env.get("HTTP_PROXY") == "http://proxy.example:8080"
    assert env.get("http_proxy") == "http://proxy.example:8080"


def test_lowercase_only_host_proxy_still_emits_both_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("https_proxy", "http://proxy.example:8443")

    env = build_subprocess_env()

    assert env.get("HTTPS_PROXY") == "http://proxy.example:8443"
    assert env.get("https_proxy") == "http://proxy.example:8443"


# --- The web command runner (build/test path shared by web + e2e_attempt) ----


def test_web_test_command_runner_strips_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inject_needles(monkeypatch)
    script = tmp_path / "dump_env.py"
    script.write_text(_DUMP_ENV_CODE, encoding="utf-8")

    async def _run() -> backend_runtime._CommandResult:
        return await backend_runtime._execute_web_test_command(
            f'"{sys.executable}" "{script}"',
            cwd=str(tmp_path),
            web_port=4599,
        )

    result = asyncio.run(_run())
    child_env = _parse_env_json(result.text)

    for name in SENSITIVE_NEEDLES:
        assert name not in child_env
        assert "needle-secret-value" not in result.text
    assert child_env.get("PORT") == "4599"
    assert child_env.get("PATH")
    assert child_env.get("ARC_WEB_PORT") == "4599"


# --- The npm runner (install_dependencies / package installs) ----------------


def test_npm_command_runner_strips_secrets_exec_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inject_needles(monkeypatch)

    async def _run() -> tuple[int, str, str]:
        return await web_handler._run_npm_command(
            [sys.executable, "-c", _DUMP_ENV_CODE],
            str(tmp_path),
        )

    returncode, stdout, stderr = asyncio.run(_run())
    child_env = _parse_env_json(stdout)

    assert returncode == 0
    for name in SENSITIVE_NEEDLES:
        assert name not in child_env
        assert "needle-secret-value" not in stdout


def test_npm_command_runner_strips_secrets_shell_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inject_needles(monkeypatch)
    script = tmp_path / "dump_env.py"
    script.write_text(_DUMP_ENV_CODE, encoding="utf-8")

    async def _run() -> tuple[int, str, str]:
        return await web_handler._run_npm_command(
            f'"{sys.executable}" "{script}"',
            str(tmp_path),
        )

    returncode, stdout, _stderr = asyncio.run(_run())
    child_env = _parse_env_json(stdout)

    assert returncode == 0
    for name in SENSITIVE_NEEDLES:
        assert name not in child_env


# --- The CLI python runner ----------------------------------------------------


def test_cli_python_runner_strips_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inject_needles(monkeypatch)

    async def _run() -> str:
        return await cli._run_python_command(
            [sys.executable, "-c", _DUMP_ENV_CODE],
            cwd=str(tmp_path),
        )

    result = asyncio.run(_run())
    child_env = _parse_env_json(result)

    for name in SENSITIVE_NEEDLES:
        assert name not in child_env
        assert "needle-secret-value" not in result
    assert child_env.get("PATH")


# --- The Android gradle runners ----------------------------------------------


class _FakeProcess:
    def __init__(self) -> None:
        self.returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


def _capture_android_spawn(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    captured: list[dict] = []

    async def _fake_exec(*args, **kwargs):
        captured.append(kwargs)
        return _FakeProcess()

    async def _fake_shell(command, **kwargs):
        captured.append(kwargs)
        return _FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_shell)
    return captured


def test_android_gradle_test_runner_strips_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inject_needles(monkeypatch)
    captured = _capture_android_spawn(monkeypatch)

    asyncio.run(
        android._run_android_gradle_test(
            str(tmp_path), "app/src/test/java/com/example/unit/WidgetTest.java"
        )
    )

    assert captured, "the gradle test runner must spawn a subprocess"
    env = captured[0]["env"]
    for name in SENSITIVE_NEEDLES:
        assert name not in env
        assert "needle-secret-value" not in str(env)
    assert env.get("PATH")
    # The encoding overrides the android callers have always pinned stay.
    assert env.get("PYTHONIOENCODING") == "utf-8"
    assert env.get("JAVA_TOOL_OPTIONS") == "-Dfile.encoding=UTF-8"


def test_android_gradle_build_runner_strips_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inject_needles(monkeypatch)
    captured = _capture_android_spawn(monkeypatch)

    asyncio.run(android._run_android_gradle_build(str(tmp_path)))

    assert captured, "the gradle build runner must spawn a subprocess"
    env = captured[0]["env"]
    for name in SENSITIVE_NEEDLES:
        assert name not in env


# --- The E2E backend spawn (real node, mirrors the startup-echo tests) -------


def _make_backend_workspace(tmp_path: Path) -> Path:
    backend = tmp_path / "backend"
    (backend / "src").mkdir(parents=True)
    (backend / "package.json").write_text(
        '{"name": "backend", "scripts": {"start": "node src/server.js"}}\n',
        encoding="utf-8",
    )
    # A compact projection instead of the full env dump: the failure-echo tail
    # keeps only the newest 1500 bytes per stream, which a full env JSON can
    # overflow (the npm banner alone is large).
    (backend / "src" / "server.js").write_text(
        "console.log(JSON.stringify({\n"
        "  hasPath: Boolean(process.env.PATH),\n"
        "  port: process.env.PORT || null,\n"
        "  arcWebPort: process.env.ARC_WEB_PORT || null,\n"
        "  openaiKey: process.env.OPENAI_API_KEY || null,\n"
        "  visualKey: process.env.VISUAL_API_KEY || null,\n"
        "  awsSecret: process.env.AWS_SECRET_ACCESS_KEY || null,\n"
        "  githubToken: process.env.GITHUB_TOKEN || null\n"
        "}));\n"
        "process.exit(0);\n",
        encoding="utf-8",
    )
    return tmp_path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.slow
def test_backend_spawn_env_is_whitelisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The E2E backend server process must run on the whitelisted env too.

    The spawn's failure path echoes the crashed process's console output into
    the returned detail, which is exactly the leak channel under test: the
    dumped environment must reach the detail without the injected needles but
    with the port contract.
    """

    if shutil.which("node") is None:
        pytest.skip("node is not on PATH; backend spawn tests need a real node process")
    _inject_needles(monkeypatch)
    workspace = _make_backend_workspace(tmp_path)
    port = _free_port()

    async def _run() -> backend_runtime.BackendSpawn:
        return await backend_runtime.spawn_backend_process(
            str(workspace),
            {"PORT": str(port), "ARC_WEB_PORT": str(port)},
            web_port=port,
        )

    spawn = asyncio.run(_run())

    assert spawn.handle is None
    child_env = _parse_env_json(spawn.detail)
    assert child_env.get("openaiKey") is None
    assert child_env.get("visualKey") is None
    assert child_env.get("awsSecret") is None
    assert child_env.get("githubToken") is None
    assert "needle-secret-value" not in spawn.detail
    assert child_env.get("port") == str(port)
    assert child_env.get("arcWebPort") == str(port)
    assert child_env.get("hasPath") is True


# --- Mechanical guard: every spawn site must go through the whitelist --------


_APP_TYPE_HANDLER_ROOT = REPO_ROOT / "app_type_handler"

# The tree-aware spawn helpers (issue #181) live in core.processes and are
# the only spawn shape app_type_handler is allowed to call, so the whitelist
# guard covers them alongside the raw asyncio names they must never replace.
_SUBPROCESS_SPAWN_NAMES = {
    "create_subprocess_exec",
    "create_subprocess_shell",
    "start_subprocess_exec",
    "start_subprocess_shell",
}


def _attribute_chain_matches(node: ast.AST, *names: str) -> bool:
    """Match ``a.b``-style chains, e.g. ``os.environ``."""

    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return tuple(reversed(parts)) == tuple(names)


def _is_build_subprocess_env_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
        and getattr(node.func, "id", getattr(node.func, "attr", None)) == "build_subprocess_env"
    )


def _spawn_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _SUBPROCESS_SPAWN_NAMES
    ]


def test_every_app_type_spawn_site_uses_the_whitelist_constructor() -> None:
    """No ``create_subprocess_*`` call may run without ``build_subprocess_env``.

    An omitted ``env=`` kwarg inherits the full host environment implicitly —
    the exact shape ``_run_npm_command`` used to have. Every spawn site in
    ``app_type_handler`` must pass an ``env=`` whose value IS a direct
    ``build_subprocess_env(...)`` call: a subtree search would let a
    hand-built dict (``{**build_subprocess_env(), **os.environ}``-style
    mixing) slip through because it contains a qualifying call somewhere
    among its nodes, so the constructor must own the whole expression and
    caller extras ride in as its argument.
    """

    offenders: list[str] = []
    for path in sorted(_APP_TYPE_HANDLER_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _spawn_calls(tree):
            env_keyword = next((kw for kw in call.keywords if kw.arg == "env"), None)
            if env_keyword is None:
                offenders.append(f"{path.name}:{call.lineno} (no env= kwarg)")
            elif not _is_build_subprocess_env_call(env_keyword.value):
                offenders.append(
                    f"{path.name}:{call.lineno} (env= is not a direct build_subprocess_env(...) call"
                    "; pass extras as its argument)"
                )
    assert not offenders, (
        "app_type_handler spawn sites must build their environment through "
        f"core.processes.build_subprocess_env: {offenders}"
    )


def test_no_full_env_dict_shapes_in_app_type_handler() -> None:
    """``{**os.environ}``, ``os.environ.copy()`` and ``dict(os.environ)`` are banned.

    These are the shapes that produce a full host environment dict; any of
    them in ``app_type_handler`` is a leak path back into the model context.
    In-process reads (``os.environ.get``) stay legal.
    """

    offenders: list[str] = []
    for path in sorted(_APP_TYPE_HANDLER_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Dict)
                and None in node.keys
                and any(
                    key is None
                    and _attribute_chain_matches(value, "os", "environ")
                    for key, value in zip(node.keys, node.values)
                )
            ):
                offenders.append(f"{path.name}:{node.lineno} (**os.environ spread)")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "copy"
                and _attribute_chain_matches(node.func.value, "os", "environ")
            ):
                offenders.append(f"{path.name}:{node.lineno} (os.environ.copy())")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "dict"
                and len(node.args) == 1
                and _attribute_chain_matches(node.args[0], "os", "environ")
            ):
                offenders.append(f"{path.name}:{node.lineno} (dict(os.environ))")
    assert not offenders, (
        "full host environment dicts are banned in app_type_handler "
        f"(use core.processes.build_subprocess_env): {offenders}"
    )
