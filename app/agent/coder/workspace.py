from __future__ import annotations

from pathlib import Path

from app.tools.file_tools import FileTools
from app.tools.project_tools import ProjectTools


class Workspace:
    """
    Production workspace for CodingAgent.

    Contains NO host command runner. All model-provided commands are
    executed exclusively by app.sandbox.runner.SandboxCommandRunner
    inside the Docker sandbox. There is no host subprocess fallback.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        file_tools: FileTools | None = None,
        project_tools: ProjectTools | None = None,
    ) -> None:
        self.root = Path(root).resolve()

        # Defence in depth: the CodingAgent workspace is ALWAYS the
        # sandbox snapshot, never the host project root. Refusing the
        # project root here means a wiring mistake can never turn the
        # agent into a direct host-file editor.
        from app.sandbox.paths import (
            PROJECT_ROOT,
        )

        if self.root == PROJECT_ROOT:
            raise ValueError(
                "refusing the host project root as the agent "
                "workspace"
            )

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

