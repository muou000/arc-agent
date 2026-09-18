import asyncio
import inspect
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Awaitable, Callable

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_ID_BY_APP_TYPE = {
    "web": "web-react-express",
    "android": "mobile-android-java",
    "cli": "cli-python",
}


@dataclass(frozen=True)
class GlueAnchorSpec:
    """Declarative description of one shared integration-point file.

    The workspace map extracts a compact, always-current summary of these
    glue files (route registration, mounted endpoints, database tables) so
    stage agents stop re-reading and re-discovering the integration surface
    on every node. ``extractors`` names the summary strategies implemented in
    ``agents/context/repo_map.py``.
    """

    path: str
    label: str
    extractors: tuple[str, ...] = field(default=())

LogCallback = Callable[[str, str, str | None, str | None], Awaitable[None] | None]


REPO_ROOT = os.path.abspath(os.path.join(BASE_DIR, ".."))


def _resolve_templates_root() -> str:
    env_root = os.environ.get("ARC_AGENT_TEMPLATES_ROOT", "").strip()
    if env_root:
        return os.path.abspath(env_root)
    return os.path.join(REPO_ROOT, "arc-template", "templates")


def template_candidates(template_id: str) -> list[str]:
    """Template directory for an app type.

    ``ARC_AGENT_TEMPLATES_ROOT`` overrides the in-repo location, which is how
    tests point the compiler at a scratch tree. A list is returned so
    ``template_dir`` keeps its "first usable candidate wins" shape.
    """
    return [os.path.join(_resolve_templates_root(), template_id)]


class AppTypeHandler(ABC):
    name = "web"

    def __init__(
        self,
        workspace_path: str,
        requirement_path: str,
        interface_designer,
        log_cb: LogCallback,
    ):
        self.workspace_path = workspace_path
        self.requirement_path = requirement_path
        self.interface_designer = interface_designer
        self.log_cb = log_cb

    @classmethod
    def template_dir(cls) -> str:
        template_id = TEMPLATE_ID_BY_APP_TYPE.get(cls.name)
        if template_id is None:
            raise ValueError(
                f"No external template mapping is configured for app_type={cls.name!r}."
            )
        candidates = template_candidates(template_id)
        for candidate in candidates:
            if cls._template_looks_usable(candidate):
                return candidate
        # Nothing usable found: return the primary path so the caller's error
        # message points at the location we expected the template to live in.
        return candidates[0]

    async def initialize_workspace(self) -> bool:
        prereqs_ok = await self.check_prerequisites()
        if not prereqs_ok:
            return False

        copied = await self.copy_template()
        if not copied:
            return False

        setup_ok = await self.post_template_setup()
        if not setup_ok:
            return False

        deps_ok = await self.install_dependencies()
        if not deps_ok:
            return False

        return await self.verify_workspace()

    async def shutdown_e2e_runtime(self) -> None:
        """Release any E2E runtime kept alive across a TDD session.

        The web handler keeps one backend server running between E2E
        ``run_tests`` calls so a TDD fix loop does not pay server start/stop
        per attempt. App types without a long-lived E2E runtime do nothing.
        Called when a node's IMPLEMENT phase finishes.
        """
        return

    async def verify_workspace(self) -> bool:
        """Post-install gate. Returning False aborts the compilation.

        Runs before any node is scheduled, so a broken template or a
        half-finished dependency install fails once instead of poisoning every
        requirement's build and test steps.
        """
        return True

    @classmethod
    async def check_runtime_versions(cls, log_cb=None) -> bool:
        """Validate tool versions after the PATH existence check passes.

        A tool can be on PATH yet too old to run the template's stack; the
        default accepts everything and app types with version-sensitive
        runtimes override this.
        """
        return True

    async def check_prerequisites(self) -> bool:
        from core.processes import check_prerequisites

        return await check_prerequisites(self.name, self.log_cb)

    async def copy_template(self) -> bool:
        template_dir = self.template_dir()
        if not os.path.exists(template_dir):
            await self._log("System", f"Error: Template directory not found at {template_dir}", "error", None)
            return False

        if not self._template_looks_usable(template_dir):
            await self._log(
                "System",
                "Error: Template directory is empty or incomplete at "
                f"{template_dir}. Expected at least a template.yaml and one source "
                "tree; refusing to continue with a blank workspace.",
                "error",
                None,
            )
            return False

        await self._log("System", f"Using app_type={self.name}, template={template_dir}")
        await self._log("System", f"Copying template from {template_dir} to {self.workspace_path}...")
        try:
            await asyncio.to_thread(
                shutil.copytree,
                template_dir,
                self.workspace_path,
                dirs_exist_ok=True,
            )
            await self._log("System", "Template files copied successfully.")
            return True
        except Exception as exc:
            await self._log("System", f"Error copying template: {str(exc)}", "error", None)
            return False

    @staticmethod
    def _template_looks_usable(template_dir: str) -> bool:
        """A usable template has a manifest plus at least one file to copy.

        Guards against the failure mode where the templates root exists but is
        an empty directory tree: ``shutil.copytree`` would happily succeed and
        leave the agents to hand-roll the whole project from scratch. The
        manifest alone does not count, since on its own it still yields a blank
        workspace.
        """
        manifest = os.path.join(template_dir, "template.yaml")
        if not os.path.isfile(manifest):
            return False

        manifest_path = os.path.abspath(manifest)
        for root, _dirs, files in os.walk(template_dir):
            for name in files:
                if os.path.abspath(os.path.join(root, name)) != manifest_path:
                    return True
        return False

    async def _log(
        self,
        agent_name: str,
        message: str,
        status: str | None = None,
        node_id: str | None = None,
    ) -> None:
        result = self.log_cb(agent_name, message, status, node_id)
        if inspect.isawaitable(result):
            await result

    async def post_template_setup(self) -> bool:
        return True

    async def install_dependencies(self) -> bool:
        return True

    async def run_build(self) -> str:
        return (
            "Exit Code: 1\n"
            "STDERR:\n"
            f"System build runner is not configured for app_type={self.name}.\n"
        )

    @abstractmethod
    async def run_test_file(self, test_type: str, file_path: str) -> str:
        """Run one concrete test file through the system-side test executor."""
        raise NotImplementedError

    async def run_test_group(self, test_type: str, file_paths: list[str]) -> str:
        """Run a batch of test files through the system-side test executor.

        App types can override this with a real grouped runner. The default
        implementation preserves compatibility by running files one by one.
        """
        if not file_paths:
            return (
                "Exit Code: 1\n"
                "STDERR:\n"
                f"No test files were configured for the current {test_type} batch.\n"
            )

        outputs: list[str] = []
        exit_codes: list[int] = []
        for file_path in file_paths:
            output = await self.run_test_file(test_type, file_path)
            outputs.append(f"=== Test File: {file_path} ===\n{output}")
            exit_code = -1
            for line in output.splitlines():
                stripped = line.strip()
                if stripped.startswith("Exit Code:"):
                    try:
                        exit_code = int(stripped.split("Exit Code:", 1)[1].strip())
                    except ValueError:
                        exit_code = -1
                    break
            exit_codes.append(exit_code)

        batch_exit_code = 0 if exit_codes and all(code == 0 for code in exit_codes) else 1
        header = [
            f"Exit Code: {batch_exit_code}",
            f"Batch Test Type: {test_type}",
            "Batch Test Files:",
        ]
        header.extend(f"- {file_path}" for file_path in file_paths)
        return f"{chr(10).join(header)}\n\n" + "\n\n".join(outputs)

    def validate_test_path(self, test_type: str, file_path: str) -> str | None:
        """Return an error message when a generated test path is invalid."""
        return None

    @classmethod
    def prerequisite_commands(cls) -> list[str]:
        return []

    @classmethod
    def runtime_contract_lines(
        cls,
        *,
        web_port: int | None = None,
        android_package: str | None = None,
    ) -> list[str]:
        del web_port, android_package
        return []

    @classmethod
    def project_structure_lines(
        cls,
        *,
        web_port: int | None = None,
        android_package: str | None = None,
    ) -> list[str]:
        del web_port, android_package
        return []

    @classmethod
    def test_harness_lines(
        cls,
        *,
        web_port: int | None = None,
        android_package: str | None = None,
    ) -> list[str]:
        del web_port, android_package
        return []

    @classmethod
    def workspace_glue_anchor_specs(cls) -> list[GlueAnchorSpec]:
        """Shared integration-point files the workspace map summarises.

        Unlike ``scaffold_context_files`` these are node integration points
        that agents edit during the run, so their content is not injected
        verbatim; only compact, regex-extracted summaries are. Returning an
        empty list (the default) means the app type declares no anchors and
        the map shows the plain file inventory.
        """
        return []

    @classmethod
    def scaffold_context_files(cls) -> list[str]:
        """Workspace-relative scaffold files shared verbatim by every node.

        The context pipeline injects their content once as a static layer so
        stage agents stop re-reading the same template-owned files (database
        harness, build/test configs) via ``read_file`` on every node. Only
        template-owned files whose staleness is detectable belong here; node
        integration points (route registration glue, entry files) must stay
        out because agents edit them during the run.
        """
        return []

    @classmethod
    @abstractmethod
    def build_stack_block(
        cls,
        *,
        web_port: int | None = None,
        android_package: str | None = None,
    ) -> str:
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def default_stack_summary(cls) -> str:
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def parse_stack_summary(cls, metadata_content: str) -> str:
        raise NotImplementedError

    @classmethod
    def read_stack_summary(cls, project_path: str) -> str:
        del project_path
        return cls.parse_stack_summary(cls.build_stack_block())
