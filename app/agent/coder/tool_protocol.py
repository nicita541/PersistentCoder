from __future__ import annotations

import re
from dataclasses import dataclass

from app.sandbox.policy import contains_absolute_host_path, is_absolute_path
from app.sandbox.project_path import ProjectPath, ProjectPathError
from app.tasks.change_scope import AllowedChangeSet, ChangeScopeError


class ToolProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    argv: tuple[str, ...]
    label: str


_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_FORBIDDEN_MODULES = frozenset(
    {"ensurepip", "http.server", "pip", "venv", "webbrowser"}
)
_PYTEST_OPTIONS = frozenset(
    {
        "-q",
        "-x",
        "--disable-warnings",
        "--strict-config",
        "--strict-markers",
        "--maxfail=1",
        "--maxfail=2",
    }
)


def _project_path(value: object, allowed: AllowedChangeSet | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolProtocolError("tool path must be a non-empty string")
    try:
        path = ProjectPath.parse(value)
        if allowed is not None:
            allowed.assert_can_read(path.value)
    except (ProjectPathError, ChangeScopeError) as error:
        raise ToolProtocolError(str(error)) from error
    return path.value


def _paths(value: object, field: str, allowed: AllowedChangeSet | None) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ToolProtocolError(f"'{field}' must be a non-empty list")
    if len(value) > 25:
        raise ToolProtocolError(f"'{field}' contains too many paths")
    result = [_project_path(item, allowed) for item in value]
    if len(set(result)) != len(result):
        raise ToolProtocolError(f"'{field}' contains duplicate paths")
    return result


def _args(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 32:
        raise ToolProtocolError("'args' must contain at most 32 strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 256 or "\x00" in item:
            raise ToolProtocolError("tool args must be short non-empty strings")
        if is_absolute_path(item) or contains_absolute_host_path(item):
            raise ToolProtocolError("tool args must not contain absolute host paths")
        normalized = item.replace("\\", "/")
        if ".." in normalized.split("/") or "%" in item:
            raise ToolProtocolError("tool args must not escape the project")
        result.append(item)
    return result


def decode_tool_calls(
    value: object,
    *,
    allowed_changes: AllowedChangeSet | None = None,
) -> list[ToolCall]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ToolProtocolError("'tools' must be a list")
    if len(value) > 8:
        raise ToolProtocolError("too many tool calls in one envelope")

    calls: list[ToolCall] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ToolProtocolError("each tool call must be an object")
        name = raw.get("tool")
        if not isinstance(name, str):
            raise ToolProtocolError("tool call requires a 'tool' name")

        if name == "py_compile":
            if set(raw) != {"tool", "paths"}:
                raise ToolProtocolError("py_compile accepts only 'paths'")
            paths = _paths(raw.get("paths"), "paths", allowed_changes)
            calls.append(
                ToolCall(name, ("python", "-m", "py_compile", *paths), f"py_compile({', '.join(paths)})")
            )
            continue

        if name == "pytest":
            if not set(raw).issubset({"tool", "targets", "options"}):
                raise ToolProtocolError("pytest accepts only 'targets' and 'options'")
            targets = _paths(raw.get("targets"), "targets", allowed_changes)
            options = raw.get("options", ["-q"])
            if not isinstance(options, list) or not all(
                isinstance(item, str) and item in _PYTEST_OPTIONS for item in options
            ):
                raise ToolProtocolError("pytest options are not allowlisted")
            calls.append(
                ToolCall(
                    name,
                    ("python", "-m", "pytest", *options, *targets),
                    f"pytest({', '.join(targets)})",
                )
            )
            continue

        if name == "python_file":
            if not set(raw).issubset({"tool", "path", "args"}):
                raise ToolProtocolError("python_file accepts only 'path' and 'args'")
            path = _project_path(raw.get("path"), allowed_changes)
            if not path.endswith(".py"):
                raise ToolProtocolError("python_file path must end with .py")
            calls.append(
                ToolCall(name, ("python", path, *_args(raw.get("args"))), f"python_file({path})")
            )
            continue

        if name == "python_module":
            if not set(raw).issubset({"tool", "module", "args"}):
                raise ToolProtocolError("python_module accepts only 'module' and 'args'")
            module = raw.get("module")
            if (
                not isinstance(module, str)
                or not _MODULE_RE.fullmatch(module)
                or module.casefold() in _FORBIDDEN_MODULES
            ):
                raise ToolProtocolError("python_module requires an allowed module name")
            calls.append(
                ToolCall(
                    name,
                    ("python", "-m", module, *_args(raw.get("args"))),
                    f"python_module({module})",
                )
            )
            continue

        raise ToolProtocolError(f"unknown tool: {name}")

    return calls
