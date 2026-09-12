from __future__ import annotations

from pathlib import Path

from app.tools.file_tools import FileTools


DEFAULT_IGNORED_DIRECTORIES = (
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    "models",
    ".pytest_cache",
    ".mypy_cache",
)


class ProjectTools:
    """
    Анализ проекта поверх FileTools.

    Используется ContextBuilder и CodingAgent,
    чтобы агент видел реальные файлы проекта.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        files: FileTools | None = None,
        ignored: tuple[str, ...] = (
            DEFAULT_IGNORED_DIRECTORIES
        ),
    ) -> None:
        self.root = Path(root).resolve()
        self.files = files or FileTools(self.root)
        self.ignored = ignored

    def _is_ignored(
        self,
        relative_path: str,
    ) -> bool:
        parts = Path(relative_path).parts

        return any(
            part in self.ignored
            for part in parts
        )

    def list_files(
        self,
    ) -> list[str]:
        return [
            relative
            for relative in self.files.list_files()
            if not self._is_ignored(relative)
        ]

    def python_files(
        self,
        *,
        max_files: int = 200,
    ) -> list[str]:
        return [
            relative
            for relative in self.list_files()
            if relative.endswith(".py")
        ][:max_files]

    def read(
        self,
        path: str,
    ) -> str:
        return self.files.read_text(path)

    def search(
        self,
        needle: str,
        *,
        pattern: str = "**/*.py",
        max_results: int = 50,
    ) -> list[str]:
        if not needle:
            return []

        results: list[str] = []

        for relative in self.files.list_files(
            pattern
        ):
            if self._is_ignored(relative):
                continue

            try:
                text = self.files.read_text(
                    relative
                )

            except Exception:
                continue

            for number, line in enumerate(
                text.splitlines(),
                start=1,
            ):
                if needle in line:
                    results.append(
                        f"{relative}:{number}: "
                        f"{line.strip()}"
                    )

                    if len(results) >= max_results:
                        return results

        return results

    def snapshot(
        self,
        *,
        max_files: int = 40,
        max_chars: int = 6000,
    ) -> str:
        """
        Текстовый анализ проекта для промпта агента.
        """

        files = self.list_files()

        if not files:
            return (
                "PROJECT SNAPSHOT:\n"
                "(файлы проекта не найдены)"
            )

        lines = [
            "PROJECT SNAPSHOT:",
            f"root: {self.root.name}",
            f"files: {len(files)}",
            "",
            "FILES:",
        ]

        for relative in files[:max_files]:
            lines.append(f"- {relative}")

        if len(files) > max_files:
            lines.append(
                f"- ... ({len(files) - max_files} more)"
            )

        snapshot = "\n".join(lines)

        if len(snapshot) > max_chars:
            snapshot = snapshot[:max_chars] + "..."

        return snapshot
