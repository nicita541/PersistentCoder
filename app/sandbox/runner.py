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
_ARGV_WRAPPER = (
    'set -eu; cp -a /input/. /workspace/; '
    'cd "$1"; shift; exec "$@"'
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
        self._admission = threading.BoundedSemaphore(
            value=max(1, int(self.limits.max_concurrent_commands))
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
            "root_filesystem": "read-only",
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

        tmpfs_tmp = (
            "/tmp:rw,size="
            f"{self.limits.max_command_tmp_bytes},"
            "uid=1000,gid=1000,mode=1777"
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
            "--read-only",
            "--pids-limit",
            str(self.pids_limit),
            "--memory",
            self.memory,
            "--memory-swap",
            self.memory,
            "--cpus",
            self.cpus,
            "--user",
            self.user,
            "--mount",
            mount,
            "--tmpfs",
            tmpfs,
            "--tmpfs",
            tmpfs_tmp,
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

    def argv_arguments(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        cwd: str | Path | None = None,
    ) -> list[str]:
        if not argv or any(not isinstance(item, str) or "\x00" in item for item in argv):
            raise PolicyViolation("framework argv must contain non-NUL strings")
        base = self.command_arguments("true", cwd=cwd)
        script_index = base.index("-lc") + 1
        workdir = base[-2]
        return [
            *base[:script_index],
            _ARGV_WRAPPER,
            "persistentcoder",
            workdir,
            *argv,
        ]

    def environment_identity(self) -> dict[str, object]:
        return {
            "image": self.image,
            "network": "none",
            "memory": self.memory,
            "cpus": self.cpus,
            "pids_limit": self.pids_limit,
            "user": self.user,
            "workspace_bytes": self.limits.max_command_workspace_bytes,
            "tmp_bytes": self.limits.max_command_tmp_bytes,
            "output_bytes": self.limits.max_output_bytes,
            "timeout_seconds": self.timeout,
            "max_concurrent_commands": self.limits.max_concurrent_commands,
        }

    def verification_environment(
        self,
    ) -> tuple[bool, str, dict[str, object]]:
        identity = self.environment_identity()
        if not self._admission.acquire(blocking=False):
            return False, "sandbox command capacity exhausted", {
                **identity,
                "image_id": None,
            }
        try:
            return self._verification_environment_with_admission(identity)
        finally:
            self._admission.release()

    def _verification_environment_with_admission(
        self,
        identity: dict[str, object],
    ) -> tuple[bool, str, dict[str, object]]:
        if not self.daemon_available():
            return False, "Docker daemon is unavailable", {
                **identity,
                "image_id": None,
            }
        if not self.ensure_image():
            return False, "sandbox image is unavailable", {
                **identity,
                "image_id": None,
            }
        try:
            completed = subprocess.run(
                [
                    self.docker,
                    "image",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    self.image,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return False, f"sandbox image identity unavailable: {error}", {
                **identity,
                "image_id": None,
            }
        image_id = completed.stdout.strip() if completed.returncode == 0 else ""
        if not image_id:
            return False, "sandbox image identity unavailable", {
                **identity,
                "image_id": None,
            }
        return True, "available", {
            **identity,
            "image_id": image_id,
        }

    # ==================================
    # RUN
    # ==================================

    def _run_in_container(
        self,
        *,
        command_label: str,
        arguments: list[str],
        cancellation_token: CancellationToken | None,
        on_output: Callable[[str, str], None] | None,
    ) -> CommandResult:
        if not self._admission.acquire(blocking=False):
            return CommandResult(
                command=command_label,
                returncode=125,
                stderr="sandbox command capacity exhausted",
            )
        try:
            if not self.daemon_available():
                return CommandResult(
                    command=command_label,
                    returncode=127,
                    stderr=(
                        "sandbox unavailable: Docker daemon is a prerequisite; "
                        "refusing to execute on host"
                    ),
                )
            if not self.ensure_image():
                return CommandResult(
                    command=command_label,
                    returncode=126,
                    stderr=(
                        f"sandbox image unavailable: {self.image} could not be "
                        "built or found"
                    ),
                )
            return self._execute(
                arguments,
                command_label=command_label,
                cancellation_token=cancellation_token,
                on_output=on_output,
                admission_held=True,
            )
        finally:
            self._admission.release()

    def run(
        self,
        command: str,
        *,
        cwd: str | Path | None = None,
        cancellation_token: CancellationToken | None = None,
        on_output: Callable[[str, str], None] | None = None,
    ) -> CommandResult:
        validated = self.policy.validate(command)

        if cancellation_token is not None and cancellation_token.cancelled:
            return CommandResult(
                command=validated,
                returncode=130,
                stdout="",
                stderr="command cancelled before start",
            )

        arguments = self.command_arguments(
            validated,
            cwd=cwd,
        )

        return self._run_in_container(
            command_label=validated,
            arguments=arguments,
            cancellation_token=cancellation_token,
            on_output=on_output,
        )

    def run_argv(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        cwd: str | Path | None = None,
        cancellation_token: CancellationToken | None = None,
        on_output: Callable[[str, str], None] | None = None,
    ) -> CommandResult:
        command = [str(item) for item in argv]
        label = " ".join(command)
        if cancellation_token is not None and cancellation_token.cancelled:
            return CommandResult(command=label, returncode=130, stderr="command cancelled before start")
        arguments = self.argv_arguments(command, cwd=cwd)
        return self._run_in_container(
            command_label=label,
            arguments=arguments,
            cancellation_token=cancellation_token,
            on_output=on_output,
        )

    def _execute(
        self,
        arguments: list[str],
        *,
        command_label: str,
        cancellation_token: CancellationToken | None,
        on_output: Callable[[str, str], None] | None,
        admission_held: bool = False,
    ) -> CommandResult:
        release_admission = not admission_held
        if release_admission and not self._admission.acquire(blocking=False):
            return CommandResult(
                command=command_label,
                returncode=125,
                stderr="sandbox command capacity exhausted",
            )

        container_name = "persistentcoder-" + uuid.uuid4().hex[:12]
        arguments[arguments.index("run") + 1 : arguments.index("run") + 1] = [
            "--name",
            container_name,
        ]

        try:
            try:
                process = subprocess.Popen(
                    arguments,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=False,
                )
            except OSError as error:
                return CommandResult(
                    command=command_label,
                    returncode=125,
                    stderr=f"sandbox process failed to start: {error}",
                )

            # Compatibility for simple process doubles; real subprocess pipes
            # always take the bounded streaming path below.
            if not hasattr(process, "stdout") or not hasattr(process, "stderr"):
                return self._communicate_compat(
                    process, container_name, command_label, cancellation_token
                )

            retained = {"stdout": bytearray(), "stderr": bytearray()}
            total = 0
            retained_total = 0
            lock = threading.Lock()
            flooded = threading.Event()
            limit = max(1, int(self.limits.max_output_bytes))

            def drain(name: str, stream) -> None:
                nonlocal total, retained_total
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        return
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8", errors="replace")
                    with lock:
                        total += len(chunk)
                        room = max(0, limit - retained_total)
                        kept = chunk[:room]
                        retained[name].extend(kept)
                        retained_total += len(kept)
                        if kept and on_output is not None:
                            try:
                                on_output(
                                    name,
                                    kept.decode("utf-8", errors="replace"),
                                )
                            except Exception:
                                pass
                        if total > limit:
                            flooded.set()

            readers = [
                threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
                threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
            ]
            for reader in readers:
                reader.start()

            deadline = time.monotonic() + self.timeout
            forced: tuple[int, str] | None = None
            while process.poll() is None:
                if flooded.is_set():
                    forced = (122, "command output exceeded max_output_bytes")
                    break
                if cancellation_token is not None and cancellation_token.cancelled:
                    forced = (130, "command cancelled")
                    break
                if time.monotonic() >= deadline:
                    forced = (124, f"timeout after {self.timeout}s")
                    break
                time.sleep(0.02)

            if forced is not None:
                self._stop_container(container_name, process)
            for reader in readers:
                reader.join(timeout=2)

            if forced is None and flooded.is_set():
                forced = (122, "command output exceeded max_output_bytes")

            stdout = retained["stdout"].decode("utf-8", errors="replace")
            stderr = retained["stderr"].decode("utf-8", errors="replace")
            if forced is not None:
                code, reason = forced
                return CommandResult(
                    command=command_label,
                    returncode=code,
                    stdout=stdout,
                    stderr=(stderr + ("\n" if stderr else "") + reason),
                )
            return self._completed_result(
                command=command_label,
                returncode=int(process.returncode),
                stdout=stdout,
                stderr=stderr,
            )
        finally:
            if release_admission:
                self._admission.release()

    def _communicate_compat(self, process, container_name, command_label, cancellation_token):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                return self._completed_result(
                    command=command_label,
                    returncode=int(process.returncode or 0),
                    stdout=self._truncate(stdout or ""),
                    stderr=self._truncate(stderr or ""),
                )
            except subprocess.TimeoutExpired:
                if cancellation_token is not None and cancellation_token.cancelled:
                    self._stop_container(container_name, process)
                    return CommandResult(command=command_label, returncode=130, stderr="command cancelled")
                if time.monotonic() >= deadline:
                    self._stop_container(container_name, process)
                    return CommandResult(command=command_label, returncode=124, stderr=f"timeout after {self.timeout}s")

    @staticmethod
    def _completed_result(
        *,
        command: str,
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> CommandResult:
        combined = f"{stdout}\n{stderr}".casefold()
        markers = (
            "no space left on device",
            "cannot allocate memory",
            "out of memory",
            "memory limit exceeded",
        )
        if returncode == 137 or any(marker in combined for marker in markers):
            detail = (
                "sandbox memory limit exceeded"
                if returncode == 137
                else "sandbox disk or memory limit exceeded"
            )
            stderr = stderr + ("\n" if stderr else "") + detail
            returncode = 125
        return CommandResult(
            command=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
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



