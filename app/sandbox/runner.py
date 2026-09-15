from __future__ import annotations

import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from app.sandbox.limits import (
    DEFAULT_LIMITS,
    LimitExceeded,
)
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
from app.sandbox.project_path import ProjectPath, ProjectPathError
from app.tools.terminal_tools import CommandResult


# ------------------------------------------------------------------
# Container hardening (fixed by the framework, NOT by the model).
# ------------------------------------------------------------------

CONTAINER_PIDS_LIMIT = 256
CONTAINER_MEMORY = "1g"
CONTAINER_CPUS = "1.0"

# Flags that must NEVER be present in a sandbox container run.
FORBIDDEN_FLAG_TOKENS = (
    "--privileged",
    "--pid=host",
    "--ipc=host",
    "--uts=host",
    "--network=host",
    "--net=host",
    "--device",
    "-v/var/run/docker.sock",
    "docker.sock",
    "--env",
    "-e",
    "--env-file",
    "--privileged=true",
)

# Backward-compatible alias.
DEFAULT_IMAGE = SANDBOX_IMAGE

_CONTAINER_WRAPPER = (
    'set -eu; cp -a /input/. /workspace/; '
    'cd "$1"; exec sh -lc "$2"'
)


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()



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
        timeout: int | None = None,
        policy: CommandPolicy | None = None,
        daemon_probe: Callable[[], bool] | None = None,
        memory: str = CONTAINER_MEMORY,
        cpus: str = CONTAINER_CPUS,
        pids_limit: int = CONTAINER_PIDS_LIMIT,
        user: str = SANDBOX_CONTAINER_USER,
        limits=DEFAULT_LIMITS,
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
        self.timeout = (
            limits.command_timeout if timeout is None else timeout
        )
        self.policy = policy or CommandPolicy()
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.user = user
        self.limits = limits
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
                f"{self.sandbox_root}:/input:ro"
            ),
            "workspace": "tmpfs",
            "workspace_bytes": self.limits.max_command_workspace_bytes,
        }

    def forbidden_flags_present(
        self,
        arguments: list[str] | None = None,
    ) -> list[str]:
        """
        Return any forbidden Docker flag tokens found in the argv.

        Used by tests and as a defence-in-depth assertion.
        """

        argv = (
            arguments
            if arguments is not None
            else self.command_arguments("true")
        )

        found: list[str] = []

        for token in FORBIDDEN_FLAG_TOKENS:
            for argument in argv:
                if argument == token or argument.startswith(
                    token + "="
                ):
                    found.append(token)
                    break

        return sorted(set(found))

    def _truncate(self, text: str) -> str:
        limit = self.limits.max_output_bytes

        if limit is None or len(text) <= limit:
            return text

        return (
            text[:limit]
            + "\n...[truncated "
            + str(len(text) - limit)
            + " bytes]"
        )

    def command_arguments(
        self,
        command: str,
        *,
        cwd: str | Path | None = None,
    ) -> list[str]:
        if cwd:
            try:
                relative_cwd = ProjectPath.parse(cwd).value
            except ProjectPathError as error:
                raise PolicyViolation(str(error)) from error
            workdir = "/workspace/" + relative_cwd
        else:
            workdir = "/workspace"

        mount = (
            f"type=bind,source={self.sandbox_root},"
            "target=/input,readonly"
        )
        tmpfs = (
            "/workspace:rw,size="
            f"{self.limits.max_command_workspace_bytes},"
            "uid=1000,gid=1000,mode=1770"
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
            "--mount",
            mount,
            "--tmpfs",
            tmpfs,
            "-w",
            "/workspace",
            self.image,
            "sh",
            "-lc",
            _CONTAINER_WRAPPER,
            "persistentcoder",
            workdir,
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
        cancellation_token: CancellationToken | None = None,
    ) -> CommandResult:
        validated = self.policy.validate(command)

        if cancellation_token is not None and cancellation_token.cancelled:
            return CommandResult(
                command=validated,
                returncode=130,
                stdout="",
                stderr="command cancelled before start",
            )

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

        container_name = "persistentcoder-" + uuid.uuid4().hex[:12]
        arguments[arguments.index("run") + 1 : arguments.index("run") + 1] = [
            "--name",
            container_name,
        ]

        try:
            process = subprocess.Popen(
                arguments,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as error:
            return CommandResult(
                command=validated,
                returncode=125,
                stderr=f"sandbox process failed to start: {error}",
            )

        deadline = time.monotonic() + self.timeout
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if cancellation_token is not None and cancellation_token.cancelled:
                    self._stop_container(container_name, process)
                    return CommandResult(
                        command=validated,
                        returncode=130,
                        stderr="command cancelled",
                    )
                if time.monotonic() >= deadline:
                    self._stop_container(container_name, process)
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
            returncode=process.returncode,
            stdout=self._truncate(stdout or ""),
            stderr=self._truncate(stderr or ""),
        )

    def _stop_container(
        self,
        container_name: str,
        process: subprocess.Popen,
    ) -> None:
        try:
            subprocess.run(
                [self.docker, "kill", container_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        if process.poll() is None:
            process.kill()
        try:
            process.communicate(timeout=5)
        except subprocess.SubprocessError:
            pass



