from __future__ import annotations

import stat
from pathlib import Path

from app.sandbox.limits import (
    DEFAULT_LIMITS,
    LimitExceeded,
)
from app.sandbox.policy import (
    PathPolicy,
    PolicyViolation,
)
from app.sandbox.project_path import ProjectGlob, ProjectPath, ProjectPathError


class FileToolsError(PolicyViolation):
    """
    Refused operation: absolute host path or escape from workspace.
    """



class FileTools:
    """
    Единственный низкоуровневый файловый слой.

    Все агенты читают и изменяют файлы только через него.
    Доступ ограничен корнем workspace и framework resource limits.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        limits=DEFAULT_LIMITS,
    ) -> None:
        self.root = Path(root).resolve()
        self.policy = PathPolicy(self.root)
        self.limits = limits

    def _resolve(
        self,
        path: str | Path,
    ) -> Path:
        try:
            return self.policy.resolve(path)

        except PolicyViolation as error:
            raise FileToolsError(
                str(error)
            ) from error

    def _workspace_bytes(self) -> int:
        total = 0

        for candidate in self.root.rglob("*"):
            try:
                if candidate.is_file():
                    total += candidate.stat().st_size

            except OSError:
                continue

        return total


    def exists(
        self,
        path: str | Path,
    ) -> bool:
        return self._resolve(path).exists()

    def read_text(
        self,
        path: str | Path,
    ) -> str:
        target = self._resolve(path)

        if not target.exists():
            raise FileToolsError(
                f"file does not exist: {path}"
            )

        size = target.stat().st_size

        if size > self.limits.max_read_bytes:
            raise LimitExceeded(
                "file too large to read: "
                f"{path} ({size} > "
                f"{self.limits.max_read_bytes})"
            )

        return target.read_text(
            encoding="utf-8"
        )

    def write_text(
        self,
        path: str | Path,
        content: str,
    ) -> Path:
        target = self._resolve(path)

        encoded = content.encode("utf-8")

        if len(encoded) > self.limits.max_file_bytes:
            raise LimitExceeded(
                "file write exceeds max_file_bytes: "
                f"{path} ({len(encoded)} > "
                f"{self.limits.max_file_bytes})"
            )

        existing = 0

        if target.exists():
            existing = target.stat().st_size

        projected = (
            self._workspace_bytes()
            - existing
            + len(encoded)
        )

        if projected > self.limits.max_workspace_bytes:
            raise LimitExceeded(
                "workspace growth exceeds "
                "max_workspace_bytes "
                f"({projected} > "
                f"{self.limits.max_workspace_bytes})"
            )

        target.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        target.write_text(
            content,
            encoding="utf-8",
        )

        return target

    def ensure_dir(
        self,
        path: str | Path,
    ) -> Path:
        target = self._resolve(path)

        target.mkdir(
            parents=True,
            exist_ok=True,
        )

        return target

    def delete(
        self,
        path: str | Path,
    ) -> bool:
        target = self.validate_delete(path)

        target.unlink()

        return True

    def validate_delete(
        self,
        path: str | Path,
    ) -> Path:
        """Return one safe, existing regular-file deletion target."""

        try:
            project_path = ProjectPath.parse(path)
        except ProjectPathError as error:
            raise FileToolsError(str(error)) from error

        # Inspect the lexical directory entry before resolve(): resolving
        # first would hide a symlink/reparse point behind its target.
        lexical = self.root.joinpath(*project_path.value.split("/"))

        try:
            metadata = lexical.lstat()
        except FileNotFoundError as error:
            raise FileToolsError(
                f"file does not exist: {project_path.value}"
            ) from error

        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if stat.S_ISLNK(metadata.st_mode) or attributes & 0x400:
            raise FileToolsError(
                "refusing to delete link or reparse point: "
                f"{project_path.value}"
            )

        target = self._resolve(project_path.value)

        if not target.is_file():
            raise FileToolsError(
                "refusing to delete anything except one regular file: "
                f"{project_path.value}"
            )

        return target

    def list_files(
        self,
        pattern: str = "**/*",
    ) -> list[str]:
        try:
            project_glob = ProjectGlob.parse(pattern)
        except ProjectPathError as error:
            raise FileToolsError(str(error)) from error
        return sorted(
            self.relative(candidate)
            for candidate in self.root.glob(
                project_glob.value
            )
            if candidate.is_file()
        )

    def relative(
        self,
        path: str | Path,
    ) -> str:
        candidate = Path(path)

        if not candidate.is_absolute():
            candidate = self.root / candidate

        candidate = candidate.resolve()

        if (
            candidate != self.root
            and self.root not in candidate.parents
        ):
            raise FileToolsError(
                f"path escapes workspace: {path}"
            )

        relative = candidate.relative_to(self.root).as_posix()
        try:
            return ProjectPath.parse(relative).value
        except ProjectPathError as error:
            raise FileToolsError(str(error)) from error

