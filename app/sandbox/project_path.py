from __future__ import annotations

import os
import re
from fnmatch import fnmatchcase
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_WINDOWS_DEVICE_NAMES = (
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"COM{suffix}" for suffix in "123456789¹²³"),
    *(f"LPT{suffix}" for suffix in "123456789¹²³"),
)
_WINDOWS_RESERVED = re.compile(
    r"^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?$",
    re.IGNORECASE,
)


class ProjectPathError(ValueError):
    """Raised when text cannot name one file inside a project."""


@dataclass(frozen=True, slots=True)
class ProjectPath:
    value: str

    @classmethod
    def parse(cls, value: str | Path) -> "ProjectPath":
        if value is None:
            raise ProjectPathError("path is required")

        source = str(value)
        raw = source.strip()
        if not raw:
            raise ProjectPathError("path is required")
        if raw != source:
            raise ProjectPathError("leading or trailing whitespace is forbidden")
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
            if ":" in part:
                raise ProjectPathError(
                    f"alternate data streams are forbidden: {raw}"
                )
            if any(ord(character) < 32 for character in part):
                raise ProjectPathError(f"control characters are forbidden: {raw}")
            if any(character in part for character in "*?[]"):
                raise ProjectPathError(f"path must name one exact file: {raw}")
            if part.endswith((" ", ".")):
                raise ProjectPathError(
                    f"Windows-normalized path component is forbidden: {raw}"
                )
            if _WINDOWS_RESERVED.match(part):
                raise ProjectPathError(
                    f"reserved Windows path component is forbidden: {raw}"
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


@dataclass(frozen=True, slots=True)
class ProjectGlob:
    value: str

    @classmethod
    def parse(cls, value: str) -> "ProjectGlob":
        if value is None:
            raise ProjectPathError("glob is required")
        source = str(value)
        raw = source.strip()
        if not raw or raw != source or "\x00" in raw or "%" in raw:
            raise ProjectPathError("invalid project glob")
        normalized = raw.replace("\\", "/")
        if (
            normalized.startswith("/")
            or normalized.startswith("//")
            or _DRIVE_PREFIX.match(normalized)
        ):
            raise ProjectPathError(f"absolute globs are forbidden: {raw}")
        if normalized.startswith("./"):
            normalized = normalized[2:]
        parts: list[str] = []
        for part in normalized.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                raise ProjectPathError(f"glob escapes project: {raw}")
            if ":" in part:
                raise ProjectPathError(
                    f"alternate data streams are forbidden: {raw}"
                )
            if any(ord(character) < 32 for character in part):
                raise ProjectPathError(f"control characters are forbidden: {raw}")
            if part.endswith((" ", ".")):
                raise ProjectPathError(
                    f"Windows-normalized glob component is forbidden: {raw}"
                )
            literal = "".join(
                character for character in part if character not in "*?[]-"
            )
            if literal and _WINDOWS_RESERVED.match(literal):
                raise ProjectPathError(
                    f"reserved Windows glob component is forbidden: {raw}"
                )
            base_pattern = part.split(".", 1)[0].upper()
            if literal and base_pattern not in {"*", "**", "?"} and any(
                fnmatchcase(device, base_pattern)
                for device in _WINDOWS_DEVICE_NAMES
            ):
                raise ProjectPathError(
                    f"glob can select a reserved Windows device: {raw}"
                )
            parts.append(part)
        if not parts:
            raise ProjectPathError("glob must select project files")
        return cls("/".join(parts))
