from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from app.sandbox.paths import (
    PROJECT_ROOT,
    SANDBOX_CONTAINER_USER,
    SANDBOX_DOCKERFILE_DIR,
    SANDBOX_IMAGE,
)
from app.sandbox.policy import (
    CommandPolicy,
    PolicyViolation,
)
from app.tools.terminal_tools import CommandResult


# ------------------------------------------------------------------
# Container hardening (fixed by the framework, NOT by the model).
# ------------------------------------------------------------------

CONTAINER_PIDS_LIMIT = 256
CONTAINER_MEMORY = "1g"
CONTAINER_CPUS = "1.0"

# Backward-compatible alias.
DEFAULT_IMAGE = SANDBOX_IMAGE



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
        image: str = SANDBOX_IMAGE,
        docker_executable: str = "docker",
        timeout: int = 180,
        policy: CommandPolicy | None = None,
        daemon_probe: Callable[[], bool] | None = None,
        memory: str = CONTAINER_MEMORY,
        cpus: str = CONTAINER_CPUS,
        pids_limit: int = CONTAINER_PIDS_LIMIT,
        user: str = SANDBOX_CONTAINER_USER,
    ) -> None:
        self.sandbox_root = Path(
            sandbox_root
        ).resolve()

        # Defence in depth: never bind-mount the host project root.
        if self.sandbox_root == Path(
            PROJECT_ROOT
        ).resolve():
            raise ValueError(
                "refusing to mount the host project "
                "root into the sandbox"
            )

        self.image = image
        self.docker = docker_executable
        self.timeout = timeout
        self.policy = policy or CommandPolicy()
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.user = user
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
    # IMAGE
    # ==================================

    def image_available(self) -> bool:
        try:
            completed = subprocess.run(
                [
                    self.docker,
                    "image",
                    "inspect",
                    self.image,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

        except (
            OSError,
            subprocess.SubprocessError,
        ):
            return False

        return completed.returncode == 0

    def ensure_image(self) -> bool:
        """
        Build the project-local sandbox image if it is missing.

        The Dockerfile lives under PROJECT_ROOT/sandbox and the build
        context is that directory (never the whole project).
        """

        if self.image_available():
            return True

        if not self.daemon_available():
            return False

        dockerfile = (
            SANDBOX_DOCKERFILE_DIR / "Dockerfile"
        )

        if not dockerfile.exists():
            return False

        try:
            completed = subprocess.run(
                [
                    self.docker,
                    "build",
                    "-t",
                    self.image,
                    str(SANDBOX_DOCKERFILE_DIR),
                ],
                capture_output=True,
                text=True,
                timeout=1800,
            )

        except (
            OSError,
            subprocess.SubprocessError,
        ):
            return False

        return completed.returncode == 0

    # ==================================
    # HARDENING
    # ==================================

    def hardening_flags(self) -> dict[str, object]:
        """
        Introspection helper (tests / reports).

        Never privileged, never mounts docker.sock,
        never mounts the host project root.
        """

        return {
            "network": "none",
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges"],
            "pids_limit": self.pids_limit,
            "memory": self.memory,
            "cpus": self.cpus,
            "user": self.user,
            "privileged": False,
            "mount_docker_socket": False,
            "mount_project_root": False,
            "mount": (
                f"{self.sandbox_root}:/workspace:rw"
            ),
        }

    def command_arguments(
        self,
        command: str,
        *,
        cwd: str | Path | None = None,
    ) -> list[str]:
        workdir = (
            "/workspace/"
            + Path(cwd).as_posix().strip("/")
            if cwd
            else "/workspace"
        )

        return [
            self.docker,
            "run",
            "--rm",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.pids_limit),
            "--memory",
            self.memory,
            "--cpus",
            self.cpus,
            "--user",
            self.user,
            "-v",
            f"{self.sandbox_root}:/workspace:rw",
            "-w",
            workdir,
            self.image,
            "sh",
            "-lc",
            command,
        ]

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

        if not self.ensure_image():
            return CommandResult(
                command=validated,
                returncode=126,
                stdout="",
                stderr=(
                    "sandbox image unavailable: "
                    f"{self.image} could not be "
                    "built or found"
                ),
            )

        arguments = self.command_arguments(
            validated,
            cwd=cwd,
        )

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

