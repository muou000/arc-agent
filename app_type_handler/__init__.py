"""App-type registry.

The handler classes are loaded lazily: ``app_type_handler.android`` (and the
web/cli handlers behind it) pull the full agent runtime (``core.service`` ->
deepagents/langchain) into every process that only wants the registry's name
logic — ``arc doctor``, the CLI parser's help text, and any test that touches
``normalize_app_type``. Importing this package therefore stays cheap; the
concrete class loads on first real use (``get_app_type_handler_class`` or a
``from app_type_handler import WebAppType``).
"""

import importlib

from .base import AppTypeHandler


_HANDLER_SPECS: dict[str, tuple[str, str]] = {
    "web": ("web", "WebAppType"),
    "android": ("android", "AndroidAppType"),
    "cli": ("cli", "CliAppType"),
}

_HANDLER_CLASS_CACHE: dict[str, type[AppTypeHandler]] = {}

_PUBLIC_CLASS_NAMES = {class_name: app_type for app_type, (_, class_name) in _HANDLER_SPECS.items()}


def _load_handler_class(app_type: str) -> type[AppTypeHandler]:
    handler_class = _HANDLER_CLASS_CACHE.get(app_type)
    if handler_class is None:
        module_name, class_name = _HANDLER_SPECS[app_type]
        module = importlib.import_module(f".{module_name}", __name__)
        handler_class = getattr(module, class_name)
        _HANDLER_CLASS_CACHE[app_type] = handler_class
    return handler_class


def normalize_app_type(app_type: str) -> str:
    normalized = (app_type or "web").strip().lower()
    return normalized if normalized in _HANDLER_SPECS else "web"


def get_app_type_handler_class(app_type: str) -> type[AppTypeHandler]:
    return _load_handler_class(normalize_app_type(app_type))


def list_app_types() -> list[str]:
    return list(_HANDLER_SPECS.keys())


def template_shared_surfaces(app_type: str) -> frozenset[str]:
    """Workspace-relative template files stage discipline protects from
    whole-file rewrites (see ``AppTypeHandler.template_shared_surfaces``)."""

    return get_app_type_handler_class(app_type).template_shared_surfaces


def shared_test_resources(app_type: str) -> frozenset[str]:
    """Return runner-owned test configuration and fixture paths."""

    return get_app_type_handler_class(app_type).shared_test_resources


def create_app_type_handler(
    app_type: str,
    workspace_path: str,
    requirement_path: str,
    interface_designer,
    log_cb,
) -> AppTypeHandler:
    handler_class = get_app_type_handler_class(app_type)
    return handler_class(
        workspace_path=workspace_path,
        requirement_path=requirement_path,
        interface_designer=interface_designer,
        log_cb=log_cb,
    )


def read_stack_summary(project_path: str, app_type: str) -> str:
    return get_app_type_handler_class(app_type).read_stack_summary(project_path)


def __getattr__(name: str):
    """Legacy surface: handler classes and ``APP_TYPE_HANDLERS`` resolve on
    first access instead of at package import."""

    if name == "APP_TYPE_HANDLERS":
        handlers = {app_type: _load_handler_class(app_type) for app_type in _HANDLER_SPECS}
        globals()["APP_TYPE_HANDLERS"] = handlers
        return handlers
    app_type = _PUBLIC_CLASS_NAMES.get(name)
    if app_type is not None:
        handler_class = _load_handler_class(app_type)
        globals()[name] = handler_class
        return handler_class
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
