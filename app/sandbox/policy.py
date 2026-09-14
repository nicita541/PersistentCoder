from __future__ import annotations

import re
from pathlib import Path

from app.sandbox.project_path import ProjectPath, ProjectPathError


class PolicyViolation(RuntimeError):
    """
    Operation would leave the project / sandbox boundary.
    """


_DRIVE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"
)
_UNC_PATH_RE = re.compile(r"\\\\[^\\/\s]+[\\/]")

_PACKAGE_MANAGER_PATTERNS = (
    re.compile(r"\bpip3?\s+install\b", re.IGNORECASE),
    re.compile(
        r"\bpython[\w.]*\s+-m\s+pip\s+install\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bnpm\s+(install|i|add)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(yarn|pnpm)\s+(add|install)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bwinget\s+install\b", re.IGNORECASE),
    re.compile(
        r"\bchoco(latey)?\s+install\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bscoop\s+install\b", re.IGNORECASE),
    re.compile(r"\bmsiexec\b", re.IGNORECASE),
    re.compile(
        r"\bapt(-get)?\s+install\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bbrew\s+install\b", re.IGNORECASE),
    re.compile(r"\bconda\s+install\b", re.IGNORECASE),
    # Host shells: model commands must never target the host OS.
    re.compile(r"\bpowershell(\.exe)?\b", re.IGNORECASE),
    re.compile(r"\bpwsh(\.exe)?\b", re.IGNORECASE),
    re.compile(r"\bcmd(\.exe)?\b", re.IGNORECASE),
    # Network fetch from the sandbox runtime.
    re.compile(r"\bcurl\b", re.IGNORECASE),
    re.compile(r"\bwget\b", re.IGNORECASE),
    re.compile(r"\bgit\s+clone\b", re.IGNORECASE),
    re.compile(r"\bpip3?\s+download\b", re.IGNORECASE),
    re.compile(r"\bnpm\s+(publish|login)\b", re.IGNORECASE),
    # Windows administration / script-host tools. The sandbox runs a
    # Linux container as non-root, but these must be refused anyway:
    # defence in depth, and the model must never be able to reach the
    # host registry, services, scheduled tasks or script hosts.
    re.compile(r"\breg(\.exe)?\s+\w+", re.IGNORECASE),
    re.compile(
        r"\bsc(\.exe)?\s+(query|config|start|stop|create|delete)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bschtasks(\.exe)?\b", re.IGNORECASE),
    re.compile(r"\bwscript(\.exe)?\b", re.IGNORECASE),
    re.compile(r"\bcscript(\.exe)?\b", re.IGNORECASE),
    re.compile(r"\bmshta(\.exe)?\b", re.IGNORECASE),
    re.compile(r"\brundll32(\.exe)?\b", re.IGNORECASE),
    re.compile(r"\bregsvr32(\.exe)?\b", re.IGNORECASE),
    re.compile(r"\bcertutil(\.exe)?\b", re.IGNORECASE),
    re.compile(r"\bbitsadmin(\.exe)?\b", re.IGNORECASE),
    re.compile(r"\bwmic(\.exe)?\b", re.IGNORECASE),
    re.compile(r"\bnetsh(\.exe)?\b", re.IGNORECASE),
    re.compile(r"\bbcdedit(\.exe)?\b", re.IGNORECASE),
    re.compile(r"\bdiskpart(\.exe)?\b", re.IGNORECASE),
    re.compile(r"\btaskkill(\.exe)?\b", re.IGNORECASE),
    re.compile(r"\bnet(\.exe)?\s+(user|localgroup|use|share)\b", re.IGNORECASE),
    # Remote fetch / code execution through PowerShell cmdlets.
    re.compile(
        r"\binvoke-(webrequest|restmethod|expression|command)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bstart-process\b", re.IGNORECASE),
    re.compile(
        r"\b(new|set)-itemproperty\b",
        re.IGNORECASE,
    ),
    # Credential / remote-shell tooling (an argument is required, so
    # ordinary file names such as "scp.py" are unaffected).
    re.compile(
        r"\bssh(-keygen|-add|-agent)?(\.exe)?\s+\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\bscp(\.exe)?\s+\S+", re.IGNORECASE),
)


def is_absolute_path(
    value: str | Path,
) -> bool:
    """
    True for host absolute paths: C:\\x, D:/x, \\\\server\\share, /etc/x.
    """

    if value is None:
        return False

    text = str(value).strip()

    if not text:
        return False

    if text.startswith("\\\\") or text.startswith("//"):
        return True

    if (
        len(text) >= 2
        and text[1] == ":"
        and text[0].isalpha()
    ):
        return True

    if text.startswith("/") or text.startswith("\\"):
        return True

    return False


def contains_absolute_host_path(
    value: str,
) -> bool:
    if not value:
        return False

    text = str(value)

    return bool(
        _DRIVE_PATH_RE.search(text)
        or _UNC_PATH_RE.search(text)
    )


class PathPolicy:
    """
    Resolves only RELATIVE paths inside a fixed root.

    Absolute paths (C:\\ / D:\\ / F:\\ / UNC) are always rejected,
    even if they point back into the project.
    """

    def __init__(
        self,
        root: str | Path,
    ) -> None:
        self.root = Path(root).resolve()

    def resolve(
        self,
        path: str | Path,
    ) -> Path:
        if path is None or not str(path).strip():
            raise PolicyViolation("path is required")

        raw = str(path).strip()

        if is_absolute_path(raw):
            raise PolicyViolation(
                f"absolute paths are forbidden: {raw}"
            )

        if "%" in raw:
            # Environment-variable expansion (e.g. %USERPROFILE%) must
            # never be usable to reach host locations.
            raise PolicyViolation(
                f"environment expansion is forbidden: {raw}"
            )

        try:
            project_path = ProjectPath.parse(raw)
            candidate = project_path.resolve_under(self.root)
        except ProjectPathError as error:
            raise PolicyViolation(
                str(error)
            ) from error

        return candidate

    def contains(
        self,
        path: str | Path,
    ) -> bool:
        try:
            self.resolve(path)
        except PolicyViolation:
            return False

        return True


class CommandPolicy:
    """
    Model-provided shell commands must be safe to run in the sandbox.
    """

    def __init__(
        self,
        *,
        allow_package_install: bool = False,
    ) -> None:
        self.allow_package_install = (
            allow_package_install
        )

    def validate(
        self,
        command: str,
    ) -> str:
        if not command or not command.strip():
            raise PolicyViolation(
                "command is required"
            )

        text = command.strip()

        if contains_absolute_host_path(text):
            raise PolicyViolation(
                "command references an absolute host path"
            )

        if not self.allow_package_install:
            for pattern in (
                _PACKAGE_MANAGER_PATTERNS
            ):
                if pattern.search(text):
                    raise PolicyViolation(
                        "package installation is forbidden"
                    )

        return text
