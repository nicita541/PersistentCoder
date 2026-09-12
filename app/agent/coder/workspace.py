from __future__ import annotations

from pathlib import Path

from app.tools.file_tools import FileTools
from app.tools.project_tools import ProjectTools
from app.tools.terminal_tools import TerminalTools


class Workspace:
    """
    Рабочее пространство CodingAgent.

    Не содержит собственной файловой логики —
    делегирует единому Tools Layer.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        file_tools: FileTools | None = None,
        project_tools: ProjectTools | None = None,
        terminal: TerminalTools | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.files = file_tools or FileTools(
            self.root
        )
        self.project = (
            project_tools
            or ProjectTools(
                self.root,
                files=self.files,
            )
        )
        self.terminal = (
            terminal
            or TerminalTools(cwd=self.root)
        )

    def exists(
        self,
        path: str,
    ) -> bool:
        return self.files.exists(path)

    def read(
        self,
        path: str,
    ) -> str:
        return self.files.read_text(path)

    def write(
        self,
        path: str,
        content: str,
    ) -> Path:
        return self.files.write_text(
            path,
            content,
        )

    def list_files(
        self,
        pattern: str = "**/*",
    ) -> list[str]:
        return self.files.list_files(pattern)

    def relative(
        self,
        path: str,
    ) -> str:
        return self.files.relative(path)

