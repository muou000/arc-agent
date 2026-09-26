from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable


LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]


def build_run_build_tool(
    *,
    app_handler: Any,
    node_id: str,
    log_cb: LogCallback | None = None,
):
    """Build the system-owned run_build tool for the current workspace."""

    async def run_build() -> str:
        """Run the system-defined build verification. Takes no arguments."""

        await _emit_log(log_cb, "Compiler", "System is executing build verification.", node_id=node_id)
        if not hasattr(app_handler, "run_build"):
            return (
                "Exit Code: 1\n"
                "STDERR:\n"
                "System build runner is not configured for this app handler.\n"
            )
        return await app_handler.run_build()

    return run_build


def build_install_dependencies_tool(
    *,
    app_handler: Any,
    node_id: str,
    log_cb: LogCallback | None = None,
    on_successful_install: Callable[[], Awaitable[None] | None] | None = None,
):
    """Build the system-owned install_dependencies tool for the current workspace.

    Lets the TDD agent recover from ``Cannot find module '<pkg>'`` environment
    failures without any shell access: the tool performs one bounded
    ``npm install <pkg> --no-save`` inside the workspace through the app
    handler, so a missing runtime dependency becomes an ordinary
    repair-and-revalidate cycle instead of a closed layer.
    """

    async def install_dependencies(package: str, target: str = "backend") -> str:
        """Install one npm package into the workspace (backend or frontend).

        Use this only when run_tests reports a missing package (for example
        "Cannot find module 'x'" or "missing dependency: x"). Pass the bare
        package name, e.g. package='cookie-parser', target='backend'.

        The install does not modify package.json or the lockfile; it only
        fixes the workspace node_modules tree. If the TDD layer is still open,
        re-run run_tests to validate the repair. If ARC_TDD_HARD_STOP closed it,
        do not retry; report the installation as unverified in this pass.
        """

        await _emit_log(
            log_cb,
            "Compiler",
            f"System is installing package '{package}' (target={target}).",
            node_id=node_id,
        )
        install = getattr(app_handler, "install_package", None)
        if not callable(install):
            return (
                "Exit Code: 1\n"
                "STDERR:\n"
                "Package installation is not configured for this app handler.\n"
            )
        # A handler exception must not escape into the agent graph: an
        # environment-level surprise (unspawnable package manager, missing
        # platform tool) is an ordinary failed install the agent can recover
        # from, while an escaping exception kills the whole IMPLEMENT task
        # (observed: WinError 2 from npm on Windows crashed REQ-1's task and
        # blocked REQ-2/ROOT on the 2026-09-20 test1 run).
        try:
            result = await install(package, target)
            if on_successful_install is not None and _has_success_exit_code(result):
                progress = on_successful_install()
                if inspect.isawaitable(progress):
                    await progress
            return result
        except Exception as exc:
            await _emit_log(
                log_cb,
                "Compiler",
                f"Package installation crashed: {type(exc).__name__}: {exc}",
                status="error",
                node_id=node_id,
            )
            return (
                "Exit Code: 1\n"
                "STDERR:\n"
                f"Package installation crashed: {type(exc).__name__}: {exc}\n"
                "Treat the package as unavailable and fall back to a "
                "standard-library or local implementation. If the TDD layer is "
                "still open, re-run run_tests; if ARC_TDD_HARD_STOP closed it, "
                "report the unverified repair without retrying.\n"
            )

    return install_dependencies


def _has_success_exit_code(output: Any) -> bool:
    """Whether an install response explicitly reports a successful command."""

    return any(line.strip() == "Exit Code: 0" for line in str(output or "").splitlines())


async def _emit_log(
    log_cb: LogCallback | None,
    agent_name: str,
    message: str,
    *,
    status: str | None = None,
    node_id: str | None = None,
) -> None:
    if log_cb is None:
        return
    result = log_cb(agent_name, message, status, node_id)
    if inspect.isawaitable(result):
        await result
