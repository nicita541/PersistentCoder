from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    command: str
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class TerminalTools:
    """
    Единственный низкоуровневый слой запуска команд.
    """

    def __init__(
        self,
        cwd: str | Path | None = None,
        *,
        timeout: int = 180,
    ) -> None:
        self.cwd = (
            Path(cwd).resolve()
            if cwd is not None
            else None
        )

        self.timeout = timeout

    def run(
        self,
        command: str,
        *,
        cwd: str | Path | None = None,
        timeout: int | None = None,
    ) -> CommandResult:
        if not command or not command.strip():
            raise ValueError(
                "command is required"
            )

        workdir = (
            Path(cwd).resolve()
            if cwd is not None
            else self.cwd
        )

        effective_timeout = (
            timeout
            if timeout is not None
            else self.timeout
        )

        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )

        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                command=command,
                returncode=124,
                stdout=(
                    exc.stdout or ""
                    if isinstance(
                        exc.stdout,
                        str,
                    )
                    else ""
                ),
                stderr=(
                    f"timeout after "
                    f"{effective_timeout}s"
                ),
            )

        return CommandResult(
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
