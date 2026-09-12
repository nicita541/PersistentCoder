from __future__ import annotations

import re
from pathlib import Path


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

        candidate = (self.root / raw).resolve()

        if (
            candidate != self.root
            and self.root not in candidate.parents
        ):
            raise PolicyViolation(
                f"path escapes workspace: {raw}"
            )

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
