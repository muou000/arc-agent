from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from app_type_handler import list_app_types, normalize_app_type
from core.cli import (
    cli_log,
    init_debug_logger,
    print_cli_banner,
    print_cli_startup,
    print_compilation_summary,
    print_usage_report,
    stop_cli_spinner,
)
from core.config import set_web_port
from core.path_safety import validate_clean_target


@dataclass(slots=True)
class CompilationConfig:
    output_dir: str
    requirement_dir: str
    requirement_path: str
    user_requested_clear_all: bool = False
    app_type: str = "web"
    web_port: int = 3301
    resume_from_queue: bool = False
    retry_failed: bool = False
    retry_node_ids: list[str] | None = None
    model_api_mode: str | None = None


def _get_repo_root() -> str:
    return str(Path(__file__).resolve().parent)


def _ensure_dotenv_loaded() -> None:
    """Load .env file if present, respecting ARC_ENV_FILE override."""
    from core.config import load_project_env

    custom_env = os.environ.get("ARC_ENV_FILE", "").strip()
    if custom_env:
        if not os.path.isfile(custom_env):
            raise FileNotFoundError(f"ARC_ENV_FILE does not exist: {custom_env}")
        load_project_env(custom_env)
        return

    repo_root = _get_repo_root()
    default_env = os.path.join(repo_root, ".env")
    load_project_env(default_env)


def _locate_requirement_file(input_path: str) -> tuple[str, str, str]:
    """
    Locate requirements.yaml given an input path.
    Returns: (requirement_dir, requirement_path, requirement_name)
    """
    abs_input = os.path.abspath(input_path)

    if os.path.isfile(abs_input):
        if not abs_input.endswith((".yaml", ".yml")):
            raise ValueError(f"Input file must be .yaml or .yml: {abs_input}")
        requirement_dir = os.path.dirname(abs_input)
        requirement_path = abs_input
        requirement_name = os.path.basename(abs_input)
        return requirement_dir, requirement_path, requirement_name

    if os.path.isdir(abs_input):
        # Input directory should directly contain requirements.yaml
        candidates = ["requirements.yaml", "requirements.yml"]
        for candidate in candidates:
            candidate_path = os.path.join(abs_input, candidate)
            if os.path.isfile(candidate_path):
                return abs_input, candidate_path, candidate
        raise FileNotFoundError(f"No requirements.yaml found in {abs_input}")

    raise FileNotFoundError(f"Input path not found: {abs_input}")


# ============================================================
# Subcommand: compile
# ============================================================
def build_compile_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "compile",
        help="Compile requirements into a working application",
        description="Run ARC compilation from requirement tree to interfaces, tests, and implementation.",
    )
    parser.add_argument(
        "requirement_path",
        help="Path to requirements directory or .yaml file",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        required=True,
        help="Output workspace directory",
    )
    parser.add_argument(
        "-t",
        "--type",
        dest="app_type",
        default="web",
        help=f"Application type (choices: {', '.join(list_app_types())})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=3301,
        help="Web server port (only for app-type=web, default: 3301)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove existing output directory before compilation",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from saved compilation queue",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry all failed nodes from previous run (requires --resume)",
    )
    parser.add_argument(
        "--retry",
        nargs="+",
        metavar="NODE_ID",
        help="Retry specific node IDs (requires --resume)",
    )
    parser.set_defaults(func=cmd_compile)


async def cmd_compile(args: argparse.Namespace) -> int:
    """Execute compile subcommand."""
    try:
        _ensure_dotenv_loaded()
    except FileNotFoundError as exc:
        print(f"Error: {exc}")
        return 2
    from core.workflow import ARCWorkflowManager
    
    # Validate mutual exclusivity
    if args.clean and args.resume:
        print("Error: --clean and --resume are mutually exclusive")
        return 2
    if (args.retry_failed or args.retry) and not args.resume:
        print("Error: --retry-failed and --retry require --resume")
        return 2
    if args.retry_failed and args.retry:
        print("Error: --retry-failed and --retry are mutually exclusive")
        return 2
    
    # Normalize paths
    requirement_dir, requirement_path, _ = _locate_requirement_file(args.requirement_path)
    output_dir = os.path.abspath(args.output_dir)
    
    # Handle --clean
    if args.clean and os.path.exists(output_dir):
        clean_error = validate_clean_target(
            output_dir,
            requirement_dir,
            repo_root=_get_repo_root(),
        )
        if clean_error:
            print(f"Error: --clean {clean_error}.")
            return 2
        shutil.rmtree(output_dir)
    
    # Normalize app type
    normalized_app_type = normalize_app_type(args.app_type)
    
    # Set web port
    set_web_port(args.port)
    
    # Model API mode
    model_api_mode = os.environ.get("ARC_OPENAI_API_MODE", "").strip() or None
    
    config = CompilationConfig(
        output_dir=output_dir,
        requirement_dir=requirement_dir,
        requirement_path=requirement_path,
        user_requested_clear_all=args.clean,
        app_type=normalized_app_type,
        web_port=args.port,
        resume_from_queue=args.resume,
        retry_failed=args.retry_failed,
        retry_node_ids=args.retry or None,
        model_api_mode=model_api_mode,
    )
    
    # Print banner and startup info
    print_cli_banner()
    log_path = init_debug_logger(config.output_dir, reset_existing=not config.resume_from_queue)
    print_cli_startup(
        project_path=config.output_dir,
        requirement_path=config.requirement_path,
        app_type=config.app_type,
        clear_all=config.user_requested_clear_all,
        log_path=log_path,
        web_port=config.web_port,
        resume_from_queue=config.resume_from_queue,
        retry_failed=config.retry_failed,
        retry_node_ids=config.retry_node_ids,
        model_api_mode=config.model_api_mode,
    )
    
    # Run compilation
    start_time = time.time()
    try:
        workflow_manager = ARCWorkflowManager(
            workspace_path=config.output_dir,
            requirement_path=config.requirement_path,
            app_type=config.app_type,
            web_port=config.web_port,
            log_cb=cli_log,
        )
        result = await workflow_manager.start_compilation(
            clear_all=False,
            resume_from_queue=config.resume_from_queue,
            retry_failed=config.retry_failed,
            retry_node_ids=config.retry_node_ids,
        )
    finally:
        stop_cli_spinner()
    
    elapsed = time.time() - start_time
    print_compilation_summary(result, config.output_dir, elapsed)
    
    return 0 if result.get("ok") else 1


# ============================================================
# Subcommand: doctor
# ============================================================
def build_doctor_parser(subparsers) -> None:
    build_config_parser(subparsers)
    parser = subparsers.add_parser(
        "doctor",
        help="Check ARC configuration and environment",
        description="Validate configuration, check dependencies, and diagnose common issues.",
    )
    parser.set_defaults(func=cmd_doctor)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Execute doctor subcommand."""
    try:
        _ensure_dotenv_loaded()
    except FileNotFoundError as exc:
        print(f"Error: {exc}")
        return 2
    from core.config import print_health_check
    return print_health_check()


# ============================================================
# Subcommand: init
# ============================================================

# ============================================================
# Subcommand: config
# ============================================================
def build_config_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "config",
        help="Configure ARC interactively",
        description="Create or update .env file with core configuration.",
    )
    parser.set_defaults(func=cmd_config)


def cmd_config(args: argparse.Namespace) -> int:
    """Execute config subcommand."""
    from core.config import interactive_config_setup
    return interactive_config_setup()


# ============================================================
# Subcommand: usage
# ============================================================
def build_usage_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "usage",
        help="Report LLM token usage, cost and tool round-trips from runner events",
        description="Aggregate llm_usage and tool_usage events from .arc/runner-events.jsonl into per-node, per-phase, per-model/tool and run totals.",
    )
    parser.add_argument(
        "--project-dir",
        dest="project_dir",
        default="",
        help="Compiled workspace directory (default: ARCBENCH_OUTPUT_DIR/ARCBENCH_PROJECT_DIR or the current directory)",
    )
    parser.add_argument(
        "--events",
        dest="events_path",
        default="",
        help="Explicit path to a runner-events.jsonl file (overrides --project-dir)",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Print the full aggregation as JSON",
    )
    parser.set_defaults(func=cmd_usage)


def cmd_usage(args: argparse.Namespace) -> int:
    """Execute usage subcommand."""
    try:
        _ensure_dotenv_loaded()
    except FileNotFoundError:
        pass  # usage reporting works without provider configuration

    from arcbench_agent_runtime.context import RuntimePaths
    from arcbench_agent_runtime.usage import aggregate_llm_usage, aggregate_tool_usage

    if args.events_path:
        events_path = Path(args.events_path).expanduser().resolve()
    else:
        events_path = RuntimePaths.from_env(project_dir=args.project_dir or None).runner_events_path
    if not events_path.exists():
        print(f"Error: runner events file not found: {events_path}")
        return 2

    summary = aggregate_llm_usage(events_path)
    summary["tools"] = aggregate_tool_usage(events_path)
    if args.as_json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    print_usage_report(summary, events_path)
    return 0


# ============================================================
# Subcommand: eval
# ============================================================
def build_eval_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "eval",
        help="Run an A/B comparison between two compile configurations",
        description=(
            "Run a baseline and a candidate compile configuration against the same "
            "requirement tree and report candidate-minus-baseline lift on pass rate, "
            "tokens, cache hit rate, latency and estimated cost."
        ),
    )
    parser.add_argument(
        "requirement_path",
        help="Path to requirements directory or .yaml file (shared by both arms)",
    )
    parser.add_argument(
        "--baseline-env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Environment override for the baseline arm (repeatable)",
    )
    parser.add_argument(
        "--candidate-env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Environment override for the candidate arm (repeatable)",
    )
    parser.add_argument(
        "--baseline-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="Extra compile argument for the baseline arm, appended last (repeatable)",
    )
    parser.add_argument(
        "--candidate-arg",
        action="append",
        default=[],
        metavar="ARG",
        help="Extra compile argument for the candidate arm, appended last (repeatable)",
    )
    parser.add_argument(
        "--label-baseline",
        dest="label_baseline",
        default="baseline",
        help="Display name of the baseline arm (default: baseline)",
    )
    parser.add_argument(
        "--label-candidate",
        dest="label_candidate",
        default="candidate",
        help="Display name of the candidate arm (default: candidate)",
    )
    parser.add_argument(
        "--name",
        default="",
        help="Eval set name shown in the report (default: '<baseline> vs <candidate>')",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=1,
        help="Runs per arm; use one while iterating and 5 when reporting lift (default: 1)",
    )
    parser.add_argument(
        "-t",
        "--type",
        dest="app_type",
        default="web",
        help=f"Application type (choices: {', '.join(list_app_types())})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=3301,
        help="Web server port passed to each run (default: 3301)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Per-run timeout; the run is recorded as failed when it expires",
    )
    parser.add_argument(
        "--out-dir",
        dest="out_dir",
        default=None,
        help="Artifacts directory (default: records/evals/<timestamp>-<slug>)",
    )
    parser.add_argument(
        "--work-root",
        dest="work_root",
        default=None,
        help="Directory for throwaway run workspaces (default: system temp)",
    )
    parser.add_argument(
        "--keep-workspaces",
        dest="keep_workspaces",
        action="store_true",
        help="Keep run workspaces instead of deleting them after evidence snapshot",
    )
    parser.add_argument(
        "--runner-script",
        dest="runner_script",
        default=None,
        help=(
            "Run each arm with [python, SCRIPT] instead of the repository "
            "arc_main.py compile entry; SCRIPT receives '<requirement> -o "
            "<workspace> -t <type> --port <port> [arm argv]'"
        ),
    )
    parser.set_defaults(func=cmd_eval)


def cmd_eval(args: argparse.Namespace) -> int:
    """Execute eval subcommand."""
    from core.evals import ArmConfig, eval_table, parse_env_overrides, render_report_text

    try:
        baseline_env = parse_env_overrides(args.baseline_env, flag="--baseline-env")
        candidate_env = parse_env_overrides(args.candidate_env, flag="--candidate-env")
    except ValueError as exc:
        print(f"Error: {exc}")
        return 2
    if args.repetitions < 1:
        print("Error: --repetitions must be at least 1")
        return 2

    runner_command = None
    if args.runner_script:
        script = os.path.abspath(args.runner_script)
        if not os.path.isfile(script):
            print(f"Error: runner script not found: {script}")
            return 2
        runner_command = [sys.executable, script]

    baseline = ArmConfig(label=args.label_baseline, env=baseline_env, argv=list(args.baseline_arg))
    candidate = ArmConfig(label=args.label_candidate, env=candidate_env, argv=list(args.candidate_arg))
    name = args.name.strip() or f"{baseline.label} vs {candidate.label}"
    try:
        result = eval_table(
            name,
            baseline,
            candidate,
            requirement_path=args.requirement_path,
            repetitions=args.repetitions,
            app_type=args.app_type,
            web_port=args.port,
            timeout_seconds=args.timeout,
            runner_command=runner_command,
            artifacts_dir=args.out_dir,
            work_root=args.work_root,
            keep_workspaces=args.keep_workspaces,
        )
    except (ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}")
        return 2

    print()
    print(render_report_text(result.report))
    print()
    all_runs_lost = all(run.get("error") for run in result.runs)
    if all_runs_lost:
        print("Error: every run failed to execute; see console.log under the sessions directory")
        return 2
    return 0


# ============================================================
# Main CLI entry
# ============================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arc",
        description="ARC: Agentic Requirement Compiler",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="ARC 1.2.0",
    )
    
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        help="Available commands",
    )
    
    build_compile_parser(subparsers)
    build_doctor_parser(subparsers)
    build_usage_parser(subparsers)
    build_eval_parser(subparsers)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    
    # Call subcommand handler
    if asyncio.iscoroutinefunction(args.func):
        exit_code = asyncio.run(args.func(args))
    else:
        exit_code = args.func(args)
    
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
