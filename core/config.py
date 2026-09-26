"""Configuration validation and health check for ARC."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from colorama import Fore, Style

from agents.context.pipeline import set_context_config
from core.runtime_config import runtime_config_warnings, validate_runtime_config


_workspace_root = Path(os.environ.get("ARC_WORKSPACE_ROOT", ".")).expanduser().resolve()
_app_type = os.environ.get("ARC_APP_TYPE", "web").strip().lower() or "web"
_web_port = 3301
_android_package = os.environ.get("ARC_ANDROID_PACKAGE", "com.example.template").strip() or "com.example.template"


def get_project_env_path() -> Path:
    custom_env = os.environ.get("ARC_ENV_FILE", "").strip()
    if custom_env:
        return Path(custom_env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent / ".env"


def load_project_env(env_path: str | os.PathLike[str] | None = None) -> None:
    """Load a simple KEY=VALUE .env file without overriding existing variables."""

    path = Path(env_path).expanduser() if env_path else get_project_env_path()
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    _copy_env_if_missing("OPENAI_KEY", "OPENAI_API_KEY")
    _copy_env_if_missing("OPENAI_BASE_URL", "OPENAI_API_BASE")


def set_workspace_root(path: str | os.PathLike[str]) -> None:
    global _workspace_root
    _workspace_root = Path(path).expanduser().resolve()
    os.environ["ARC_WORKSPACE_ROOT"] = str(_workspace_root)
    set_context_config(workspace_dir=str(_workspace_root))


def get_workspace_root() -> str:
    return str(_workspace_root)


def get_abs_path(path: str | os.PathLike[str]) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        return str(candidate.resolve())
    return str((_workspace_root / candidate).resolve())


def set_app_type(app_type: str) -> None:
    global _app_type
    errors = validate_runtime_config({"ARC_APP_TYPE": app_type})
    if errors:
        raise ValueError("Invalid app type configuration: " + " ".join(errors))
    _app_type = (app_type or "web").strip().lower() or "web"
    os.environ["ARC_APP_TYPE"] = _app_type
    set_context_config(app_type=_app_type)


def get_app_type() -> str:
    return _app_type


def set_web_port(port: int | str) -> None:
    global _web_port
    errors = validate_runtime_config({"ARC_WEB_PORT": str(port)})
    if errors:
        raise ValueError("Invalid web port configuration: " + " ".join(errors))
    _web_port = int(port)
    os.environ["ARC_WEB_PORT"] = str(_web_port)
    set_context_config(web_port=_web_port)


def get_web_port() -> int:
    return _web_port


def get_web_base_url(port: int | None = None) -> str:
    resolved = int(port) if port is not None else _web_port
    return f"http://localhost:{resolved}"


def build_web_runtime_env(web_port: int | None = None) -> dict[str, str]:
    resolved = int(web_port) if web_port is not None else _web_port
    return {
        "PORT": str(resolved),
        "ARC_WEB_PORT": str(resolved),
        "BASE_URL": get_web_base_url(resolved),
        "VITE_API_BASE_URL": get_web_base_url(resolved),
    }


def set_android_package(package_name: str) -> None:
    global _android_package
    _android_package = str(package_name or "").strip() or "com.example.template"
    os.environ["ARC_ANDROID_PACKAGE"] = _android_package
    set_context_config(android_package=_android_package)


def get_android_package() -> str:
    return _android_package


def _copy_env_if_missing(source: str, target: str) -> None:
    source_value = os.environ.get(source, "").strip()
    target_value = os.environ.get(target, "").strip()
    if source_value and not target_value:
        os.environ[target] = source_value


def check_config() -> dict[str, Any]:
    """
    Validate ARC configuration and environment.

    Returns a dict with:
      - "ok": bool (overall health)
      - "errors": list of critical issues
      - "warnings": list of non-critical issues
      - "info": list of informational messages
    """
    errors = []
    warnings = []
    info = []

    # Check required environment variables
    required_vars = {
        "OPENAI_API_KEY": "Main API key for model inference",
        "OPENAI_BASE_URL": "API base URL",
        "MODEL": "Main coding model name",
        "ARC_OPENAI_API_MODE": "API mode (responses or chat_completions)",
    }

    for var, description in required_vars.items():
        value = os.environ.get(var, "").strip()
        if not value:
            errors.append(f"Missing required variable: {var} ({description})")
        elif var == "OPENAI_API_KEY" and value.startswith("sk-your-"):
            errors.append(f"{var} still contains placeholder value")

    errors.extend(validate_runtime_config())
    warnings.extend(runtime_config_warnings())

    # Check optional visual model
    visual_key = os.environ.get("VISUAL_API_KEY", "").strip()
    visual_model = os.environ.get("VISUAL_MODEL", "").strip()
    if visual_key and not visual_model:
        warnings.append("VISUAL_API_KEY is set but VISUAL_MODEL is empty")
    elif visual_model and not visual_key:
        warnings.append("VISUAL_MODEL is set but VISUAL_API_KEY is empty")

    # Check .env file presence
    env_file = get_project_env_path()
    if not env_file.exists():
        warnings.append("No .env file found in current directory (copy .env_example to .env)")
    else:
        info.append(f"Configuration loaded from {env_file.resolve()}")

    # Check Python version
    if sys.version_info < (3, 11):
        errors.append(f"Python 3.11+ required, found: {sys.version_info.major}.{sys.version_info.minor}")
    else:
        info.append(f"Python version: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

    # Check agent runtime availability. Reading package metadata instead of
    # importing keeps ``doctor`` off the multi-second deepagents import chain
    # (the version line is informational; compilation loads the runtime).
    try:
        from importlib.metadata import version as _runtime_version
        from importlib.util import find_spec as _find_runtime_spec

        if _find_runtime_spec("deepagents") is not None:
            try:
                version = _runtime_version("deepagents")
            except Exception:  # noqa: BLE001 - metadata missing: still installed
                version = "installed"
            info.append(f"Agent runtime: {version}")
        else:
            errors.append("Agent runtime not installed (reinstall ARC or check dependencies)")
    except ImportError:
        errors.append("Agent runtime not installed (reinstall ARC or check dependencies)")

    # Check Node.js for web app type
    import shutil
    node_path = shutil.which("node")
    if node_path:
        info.append(f"Node.js: available at {node_path}")
    else:
        warnings.append("Node.js not found (required for app-type=web)")

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "info": info,
    }


def print_health_check() -> int:
    """
    Print configuration health check to terminal.
    Returns exit code: 0 if ok, 1 if errors found.
    """
    print(f"{Fore.CYAN}{'=' * 60}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}ARC Configuration Health Check{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'=' * 60}{Style.RESET_ALL}\n")

    result = check_config()

    if result["info"]:
        print(f"{Fore.WHITE}ℹ Info{Style.RESET_ALL}")
        for msg in result["info"]:
            print(f"  {Fore.WHITE}•{Style.RESET_ALL} {msg}")
        print()

    if result["warnings"]:
        print(f"{Fore.YELLOW}⚠ Warnings{Style.RESET_ALL}")
        for msg in result["warnings"]:
            print(f"  {Fore.YELLOW}•{Style.RESET_ALL} {msg}")
        print()

    if result["errors"]:
        print(f"{Fore.RED}✗ Errors{Style.RESET_ALL}")
        for msg in result["errors"]:
            print(f"  {Fore.RED}•{Style.RESET_ALL} {msg}")
        print()

    if result["ok"]:
        print(f"{Fore.GREEN}✓ Configuration is valid{Style.RESET_ALL}\n")
        return 0
    print(f"{Fore.RED}✗ Configuration has errors. Fix them before running ARC.{Style.RESET_ALL}\n")
    print(f"{Fore.WHITE}Quick fix:{Style.RESET_ALL}")
    print(f"  1. Copy .env_example to .env")
    print(f"  2. Edit .env and fill in your API credentials")
    print(f"  3. Run: arc doctor\n")
    return 1


def interactive_config_setup() -> int:
    """
    Interactively create or update .env file with core configuration.
    Returns exit code: 0 on success, 1 on user cancellation.
    """
    from pathlib import Path

    print(f"{Fore.CYAN}{'=' * 60}{Style.RESET_ALL}")
    print(f"{Fore.CYAN}ARC Configuration Setup{Style.RESET_ALL}")
    print(f"{Fore.CYAN}{'=' * 60}{Style.RESET_ALL}\n")

    env_file = Path(".env")

    if env_file.exists():
        print(f"{Fore.YELLOW}⚠ .env file already exists{Style.RESET_ALL}")
        overwrite = input("Overwrite existing values? (y/N): ").strip().lower()
        if overwrite not in {"y", "yes"}:
            print(f"{Fore.YELLOW}Configuration cancelled.{Style.RESET_ALL}\n")
            return 1
        print()

    # Read existing .env if present
    existing_config = {}
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    existing_config[key.strip()] = value.strip()
        except Exception:
            pass

    print(f"{Fore.WHITE}Enter configuration values (press Enter to keep existing):{Style.RESET_ALL}\n")

    # Collect required values
    configs = {}

    # OPENAI_API_KEY
    existing_key = existing_config.get("OPENAI_API_KEY", "")
    if existing_key and not existing_key.startswith("sk-your-"):
        prompt = f"OpenAI API Key [{existing_key[:8]}...{existing_key[-4:]}]: "
    else:
        prompt = "OpenAI API Key: "

    api_key = input(prompt).strip()
    if api_key:
        configs["OPENAI_API_KEY"] = api_key
    elif existing_key and not existing_key.startswith("sk-your-"):
        configs["OPENAI_API_KEY"] = existing_key
    else:
        print(f"{Fore.RED}✗ API key is required{Style.RESET_ALL}\n")
        return 1

    # OPENAI_BASE_URL
    existing_base = existing_config.get("OPENAI_BASE_URL", "")
    default_base = existing_base if existing_base else "https://api.openai.com/v1"
    base_url = input(f"OpenAI Base URL [{default_base}]: ").strip()
    configs["OPENAI_BASE_URL"] = base_url if base_url else default_base

    # MODEL
    existing_model = existing_config.get("MODEL", "")
    default_model = existing_model if existing_model else "gpt-4o"
    model = input(f"Model name [{default_model}]: ").strip()
    configs["MODEL"] = model if model else default_model

    # ARC_OPENAI_API_MODE
    existing_mode = existing_config.get("ARC_OPENAI_API_MODE", "")
    default_mode = existing_mode if existing_mode else "chat_completions"
    print(f"\nAPI mode (responses or chat_completions) [{default_mode}]: ", end="")
    mode = input().strip()
    configs["ARC_OPENAI_API_MODE"] = mode if mode else default_mode

    # Merge with existing config
    final_config = {**existing_config, **configs}

    # Write .env file
    lines = []
    lines.append("# ARC Configuration")
    lines.append("# Generated by: arc config")
    lines.append("")
    lines.append("# Required: OpenAI-compatible API")
    lines.append(f"OPENAI_API_KEY={final_config['OPENAI_API_KEY']}")
    lines.append(f"OPENAI_BASE_URL={final_config['OPENAI_BASE_URL']}")
    lines.append(f"MODEL={final_config['MODEL']}")
    lines.append(f"ARC_OPENAI_API_MODE={final_config['ARC_OPENAI_API_MODE']}")
    lines.append("")

    # Preserve other existing keys
    core_keys = {"OPENAI_API_KEY", "OPENAI_BASE_URL", "MODEL", "ARC_OPENAI_API_MODE"}
    other_keys = {k: v for k, v in existing_config.items() if k not in core_keys}

    if other_keys:
        lines.append("# Other configuration")
        for key, value in sorted(other_keys.items()):
            lines.append(f"{key}={value}")
        lines.append("")

    try:
        env_file.write_text("\n".join(lines), encoding="utf-8")
        print(f"\n{Fore.GREEN}✓ Configuration saved to {env_file.resolve()}{Style.RESET_ALL}\n")
        print(f"{Fore.WHITE}Next steps:{Style.RESET_ALL}")
        print(f"  1. Run: arc doctor")
        print(f"  2. Run: arc compile <input> -o <output>\n")
        return 0
    except Exception as e:
        print(f"\n{Fore.RED}✗ Failed to write .env file: {e}{Style.RESET_ALL}\n")
        return 1
