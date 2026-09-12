from __future__ import annotations

from pathlib import Path

from app.sandbox.policy import (
    PathPolicy,
    PolicyViolation,
)


class FileToolsError(PolicyViolation):
    """
    Refused operation: absolute host path or escape from workspace.
    """



class FileTools:
    """
    Единственный низкоуровневый файловый слой.

    Все агенты читают и изменяют файлы только через него.
    Доступ ограничен корнем workspace.
    """

    def __init__(
        self,
        root: str | Path,
    ) -> None:
        self.root = Path(root).resolve()
        self.policy = PathPolicy(self.root)

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

        return target.read_text(
            encoding="utf-8"
        )

    def write_text(
        self,
        path: str | Path,
        content: str,
    ) -> Path:
        target = self._resolve(path)

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
        target = self._resolve(path)

        if not target.exists():
            return False

        if target.is_dir():
            raise FileToolsError(
                f"refusing to delete directory: {path}"
            )

        target.unlink()

        return True

    def list_files(
        self,
        pattern: str = "**/*",
    ) -> list[str]:
        return sorted(
            candidate.relative_to(
                self.root
            ).as_posix()
            for candidate in self.root.glob(
                pattern
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

        return candidate.relative_to(
            self.root
        ).as_posix()

