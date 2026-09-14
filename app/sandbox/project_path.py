from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


class ProjectPathError(ValueError):
    """Raised when text cannot name one file inside a project."""


@dataclass(frozen=True, slots=True)
class ProjectPath:
    value: str

    @classmethod
    def parse(cls, value: str | Path) -> "ProjectPath":
        if value is None:
            raise ProjectPathError("path is required")

        raw = str(value).strip()
        if not raw:
            raise ProjectPathError("path is required")
        if "\x00" in raw:
            raise ProjectPathError("path contains a NUL byte")

        normalized = raw.replace("\\", "/")
        if (
            normalized.startswith("/")
            or normalized.startswith("//")
            or _DRIVE_PREFIX.match(normalized)
        ):
            raise ProjectPathError(
                f"absolute paths are forbidden: {raw}"
            )

        # Remove the exact relative marker.  Never use lstrip("./"):
        # it would corrupt valid dot-directories such as `.github`.
        if normalized.startswith("./"):
            normalized = normalized[2:]

        parts: list[str] = []
        for part in normalized.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                raise ProjectPathError(
                    f"path escapes project: {raw}"
                )
            parts.append(part)

        if not parts:
            raise ProjectPathError("path must name a project file")

        canonical = PurePosixPath(*parts).as_posix()
        return cls(canonical)

    @property
    def comparison_key(self) -> str:
        if os.name == "nt":
            return self.value.casefold()
        return self.value

    def resolve_under(self, root: str | Path) -> Path:
        resolved_root = Path(root).resolve()
        candidate = (resolved_root / Path(*self.value.split("/"))).resolve()
        if not candidate.is_relative_to(resolved_root):
            raise ProjectPathError(
                f"path escapes project through filesystem: {self.value}"
            )
        return candidate

    def __str__(self) -> str:
        return self.value
