from __future__ import annotations

import subprocess

import pytest

from app.sandbox.paths import PROJECT_ROOT
from app.sandbox.runner import SandboxCommandRunner


def _docker_available() -> bool:
    try:
        completed = subprocess.run(
            [
                "docker",
                "info",
                "--format",
                "{{.ServerVersion}}",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )

    except (OSError, subprocess.SubprocessError):
        return False

    return completed.returncode == 0


DOCKER_AVAILABLE = _docker_available()


def _git_status() -> str:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )

    return completed.stdout


@pytest.fixture()
def runner(tmp_path):
    sandbox_root = tmp_path / "workspace"
    sandbox_root.mkdir(parents=True, exist_ok=True)

    runner = SandboxCommandRunner(
        sandbox_root=sandbox_root,
    )

    if not runner.ensure_image():
        pytest.skip(
            "sandbox image is not available "
            "(Docker daemon / build required)"
        )

    return runner


# ==========================================
# HARDENING (no Docker daemon required)
# ==========================================


def test_command_arguments_are_hardened(tmp_path):
    sandbox_root = tmp_path / "workspace"
    sandbox_root.mkdir(parents=True, exist_ok=True)

    runner = SandboxCommandRunner(
        sandbox_root=sandbox_root
    )

    args = runner.command_arguments("pytest -q")

    assert "--cap-drop" in args
    assert args[args.index("--cap-drop") + 1] == "ALL"

    assert "--security-opt" in args
    assert (
        args[args.index("--security-opt") + 1]
        == "no-new-privileges"
    )

    assert "--pids-limit" in args
    assert "--memory" in args
    assert "--cpus" in args

    assert "--user" in args
    assert args[args.index("--user") + 1] == "1000:1000"

    # never privileged, never docker.sock, never the host project root
    assert "--privileged" not in args
    assert not any(
        "docker.sock" in argument
        for argument in args
    )

    mount = args[args.index("--mount") + 1]

    assert mount == (
        f"type=bind,source={sandbox_root},target=/input,readonly"
    )
    assert "--tmpfs" in args
    tmpfs = args[args.index("--tmpfs") + 1]
    assert tmpfs.startswith("/workspace:rw,size=")
    assert str(sandbox_root) != str(PROJECT_ROOT)
    assert mount != (
        f"type=bind,source={PROJECT_ROOT},target=/input,readonly"
    )


    flags = runner.hardening_flags()

    assert flags["privileged"] is False
    assert flags["mount_docker_socket"] is False
    assert flags["mount_project_root"] is False
    assert flags["mount"].endswith("/input:ro")
    assert flags["workspace"] == "tmpfs"


def test_refuses_to_mount_host_project_root():
    with pytest.raises(ValueError):
        SandboxCommandRunner(
            sandbox_root=PROJECT_ROOT
        )


# ==========================================
# REAL DOCKER INTEGRATION
# ==========================================


@pytest.mark.skipif(
    not DOCKER_AVAILABLE,
    reason="Docker daemon is not running",
)
def test_docker_sandbox_runs_pytest_and_keeps_host_unchanged(
    runner,
):
    before = _git_status()

    # 1. real Python inside the isolated container
    version = runner.run("python --version")

    assert version.ok, version.stderr
    assert "Python 3.12" in (
        version.stdout + version.stderr
    )

    # 2. the container runs as a non-root user
    user = runner.run("id -u")

    assert user.ok, user.stderr
    assert user.stdout.strip() == "1000"

    # 3. all Linux capabilities are dropped
    caps = runner.run(
        "grep CapEff /proc/self/status"
    )

    assert caps.ok, caps.stderr
    assert "0000000000000000" in caps.stdout

    # 4. create a real test file and run pytest inside Docker
    sample = runner.sandbox_root / "test_sample.py"

    sample.write_text(
        "def test_ok():\n"
        "    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )

    result = runner.run("pytest -q")

    combined = result.stdout + result.stderr

    assert result.ok, combined
    assert "1 passed" in combined

    # 5. the host source repository is untouched
    after = _git_status()

    assert before == after


@pytest.mark.skipif(
    not DOCKER_AVAILABLE,
    reason="Docker daemon is not running",
)
def test_command_writes_are_ephemeral(runner):
    persistent = runner.sandbox_root / "value.txt"
    persistent.write_text("persistent", encoding="utf-8")

    result = runner.run(
        "printf changed > value.txt; "
        "printf temporary > generated.txt; "
        "test -f generated.txt"
    )

    assert result.ok, result.stderr
    assert persistent.read_text(encoding="utf-8") == "persistent"
    assert not (runner.sandbox_root / "generated.txt").exists()


@pytest.mark.skipif(
    not DOCKER_AVAILABLE,
    reason="Docker daemon is not running",
)
def test_framework_argv_is_literal_in_real_container(runner):
    argument = "value; touch PWNED"

    result = runner.run_argv(
        ["python", "-c", "import sys; print(sys.argv[1])", argument]
    )

    assert result.ok, result.stderr
    assert result.stdout.strip() == argument
    assert not (runner.sandbox_root / "PWNED").exists()

    root_mount = runner.run(
        "awk '$2 == \"/\" {print $4}' /proc/mounts"
    )
    assert root_mount.ok, root_mount.stderr
    assert "ro" in root_mount.stdout.strip().split(",")
