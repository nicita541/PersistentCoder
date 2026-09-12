from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from app.sandbox.policy import (
    CommandPolicy,
    PolicyViolation,
)
from app.tools.terminal_tools import CommandResult


DEFAULT_IMAGE = "python:3.12-slim"


class SandboxCommandRunner:
    """
    Executes model-provided commands ONLY inside a container.

    Key guarantees:

      - the command never runs on the host shell;
      - shell=True is never used;
      - package installation and absolute host paths are rejected;
      - if Docker is unavailable the command is REFUSED
        (fail-closed) instead of falling back to the host.
    """

    def __init__(
        self,
        *,
        sandbox_root: str | Path,
        image: str = DEFAULT_IMAGE,
        docker_executable: str = "docker",
        timeout: int = 180,
        policy: CommandPolicy | None = None,
        daemon_probe: Callable[[], bool] | None = None,
    ) -> None:
        self.sandbox_root = Path(
            sandbox_root
        ).resolve()
        self.image = image
        self.docker = docker_executable
        self.timeout = timeout
        self.policy = policy or CommandPolicy()
        self._probe = (
            daemon_probe or self._docker_info
        )
        self._cached_availability: bool | None = (
            None
        )

    # ==================================
    # AVAILABILITY
    # ==================================

    def _docker_info(self) -> bool:
        try:
            completed = subprocess.run(
                [
                    self.docker,
                    "info",
                    "--format",
                    "{{.ServerVersion}}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )

        except (
            OSError,
            subprocess.SubprocessError,
        ):
            return False

        return completed.returncode == 0

    def daemon_available(self) -> bool:
        if self._cached_availability is None:
            self._cached_availability = bool(
                self._probe()
            )

        return self._cached_availability

    def status(self) -> str:
        if self.daemon_available():
            return "available"

        return (
            "unavailable: Docker daemon is a "
            "prerequisite for sandbox command "
            "execution"
        )

    # ==================================
    # RUN
    # ==================================

    def run(
        self,
        command: str,
        *,
        cwd: str | Path | None = None,
    ) -> CommandResult:
        validated = self.policy.validate(command)

        if not self.daemon_available():
            return CommandResult(
                command=validated,
                returncode=127,
                stdout="",
                stderr=(
                    "sandbox unavailable: "
                    "Docker daemon is a prerequisite; "
                    "refusing to execute on host"
                ),
            )

        workdir = (
            "/workspace/"
            + Path(cwd).as_posix().strip("/")
            if cwd
            else "/workspace"
        )

        arguments = [
            self.docker,
            "run",
            "--rm",
            "--network",
            "none",
            "-v",
            f"{self.sandbox_root}:/workspace",
            "-w",
            workdir,
            self.image,
            "sh",
            "-lc",
            validated,
        ]

        try:
            completed = subprocess.run(
                arguments,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )

        except subprocess.TimeoutExpired:
            return CommandResult(
                command=validated,
                returncode=124,
                stderr=(
                    "timeout after "
                    f"{self.timeout}s"
                ),
            )

        return CommandResult(
            command=validated,
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
