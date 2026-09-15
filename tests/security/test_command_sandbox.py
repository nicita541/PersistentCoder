from __future__ import annotations

import subprocess
import io

import pytest

from app.sandbox.policy import (
    CommandPolicy,
    PolicyViolation,
)
from app.sandbox.runner import SandboxCommandRunner
from app.sandbox.limits import SandboxLimits
from app.sandbox.workspace import SandboxWorkspace


# 6
def test_model_command_is_not_executed_on_host(
    tmp_path,
    monkeypatch,
):
    calls = []

    real_run = subprocess.run

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(
        "app.sandbox.runner.subprocess.run",
        spy,
    )

    runner = SandboxCommandRunner(
        sandbox_root=tmp_path,
        daemon_probe=lambda: False,
    )

    marker = "sandbox_marker.txt"

    result = runner.run(
        "python -c \"open("
        f"'{marker}','w'"
        ").write('x')\""
    )

    assert result.returncode == 127
    assert "sandbox unavailable" in result.stderr

    # Nothing ran on the host at all.
    assert calls == []
    assert not (tmp_path / marker).exists()


def test_runner_reports_docker_prerequisite(tmp_path):
    runner = SandboxCommandRunner(
        sandbox_root=tmp_path,
        daemon_probe=lambda: False,
    )

    assert runner.daemon_available() is False
    assert "Docker" in runner.status()


def test_command_policy_rejects_absolute_host_path():
    policy = CommandPolicy()

    for command in (
        r"type C:\secret.txt",
        "rm -rf D:/data",
        r"cat F:\x",
    ):
        with pytest.raises(PolicyViolation):
            policy.validate(command)


def test_command_policy_rejects_package_installation():
    policy = CommandPolicy()

    for command in (
        "pip install requests",
        "python -m pip install requests",
        "npm install left-pad",
        "winget install Git.Git",
        "choco install python",
        "apt-get install curl",
    ):
        with pytest.raises(PolicyViolation):
            policy.validate(command)


def test_command_policy_allows_relative_command():
    policy = CommandPolicy()

    assert policy.validate("pytest -q") == "pytest -q"
    assert (
        policy.validate(
            "https://example.com not-a-drive"
        )
        == "https://example.com not-a-drive"
    )


def test_framework_argv_keeps_metacharacters_literal(tmp_path):
    runner = SandboxCommandRunner(sandbox_root=tmp_path)
    argv = ["python", "-m", "py_compile", "x.py; touch PWNED"]

    arguments = runner.argv_arguments(argv)

    assert arguments[-4:] == argv
    assert "x.py; touch PWNED" not in arguments[arguments.index("-lc") + 1]
    assert "--read-only" in arguments
    assert arguments[arguments.index("--memory-swap") + 1] == runner.memory
    tmpfs_values = [
        arguments[index + 1]
        for index, value in enumerate(arguments)
        if value == "--tmpfs"
    ]
    assert any(value.startswith("/workspace:rw,size=") for value in tmpfs_values)
    assert any(value.startswith("/tmp:rw,size=") for value in tmpfs_values)


def test_admission_control_refuses_excess_command_before_start(tmp_path, monkeypatch):
    probes = {"daemon": 0, "image": 0}

    def daemon_probe():
        probes["daemon"] += 1
        return True

    runner = SandboxCommandRunner(
        sandbox_root=tmp_path,
        daemon_probe=daemon_probe,
        limits=SandboxLimits(max_concurrent_commands=1),
    )
    def ensure_image():
        probes["image"] += 1
        return True

    monkeypatch.setattr(runner, "ensure_image", ensure_image)
    assert runner._admission.acquire(blocking=False)
    try:
        result = runner.run_argv(["python", "--version"])
    finally:
        runner._admission.release()

    assert result.returncode == 125
    assert "capacity" in result.stderr
    assert probes == {"daemon": 0, "image": 0}


def test_verification_environment_uses_command_admission_slot(tmp_path):
    probes = {"daemon": 0}

    def daemon_probe():
        probes["daemon"] += 1
        return True

    runner = SandboxCommandRunner(
        sandbox_root=tmp_path,
        daemon_probe=daemon_probe,
        limits=SandboxLimits(max_concurrent_commands=1),
    )
    assert runner._admission.acquire(blocking=False)
    try:
        ready, reason, environment = runner.verification_environment()
    finally:
        runner._admission.release()

    assert not ready
    assert "capacity" in reason
    assert environment["image_id"] is None
    assert probes == {"daemon": 0}


def test_output_flood_is_killed_while_streaming(tmp_path, monkeypatch):
    class FakeProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"x" * 4096)
            self.stderr = io.BytesIO()
            self.returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = 122

        def communicate(self, timeout=None):
            return b"", b""

    process = FakeProcess()
    runner = SandboxCommandRunner(
        sandbox_root=tmp_path,
        daemon_probe=lambda: True,
        limits=SandboxLimits(max_output_bytes=32),
    )
    monkeypatch.setattr(runner, "ensure_image", lambda: True)
    monkeypatch.setattr("app.sandbox.runner.subprocess.Popen", lambda *a, **k: process)
    monkeypatch.setattr(
        "app.sandbox.runner.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""),
    )

    streamed = []
    result = runner.run_argv(
        ["python", "-c", "print('x' * 4096)"],
        on_output=lambda stream, chunk: streamed.append((stream, chunk)),
    )

    assert result.returncode == 122
    assert len(result.stdout.encode("utf-8")) <= 32
    assert sum(
        len(chunk.encode("utf-8")) for _stream, chunk in streamed
    ) <= 32
    assert "output exceeded" in result.stderr


@pytest.mark.parametrize(
    ("returncode", "stderr"),
    [(137, ""), (1, "write failed: No space left on device")],
)
def test_container_resource_exhaustion_is_reported_as_blocked_code(
    tmp_path, monkeypatch, returncode, stderr
):
    class FakeProcess:
        def __init__(self):
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO(stderr.encode())
            self.returncode = returncode

        def poll(self):
            return self.returncode

    runner = SandboxCommandRunner(
        sandbox_root=tmp_path,
        daemon_probe=lambda: True,
    )
    monkeypatch.setattr(runner, "ensure_image", lambda: True)
    monkeypatch.setattr("app.sandbox.runner.subprocess.Popen", lambda *a, **k: FakeProcess())

    result = runner.run_argv(["python", "check.py"])

    assert result.returncode == 125
    assert "limit exceeded" in result.stderr


def test_sandbox_workspace_snapshot_patch_apply(
    tmp_path,
):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "main.py").write_text(
        "print('a')\n",
        encoding="utf-8",
    )

    sandbox_root = tmp_path / "sbx"

    workspace = SandboxWorkspace.create(
        project_root=project,
        session_id="s1",
        sandbox_root=sandbox_root,
    )

    assert workspace.workspace_root != project
    assert (
        workspace.workspace_root
        / "src"
        / "main.py"
    ).exists()

    (
        workspace.workspace_root / "src" / "main.py"
    ).write_text(
        "print('b')\n",
        encoding="utf-8",
    )

    assert workspace.changed_files() == ["src/main.py"]

    patch = workspace.write_patch()

    assert patch is not None
    assert patch == (
        sandbox_root / "patches" / "s1.patch"
    )

    # Host project stays untouched until explicit apply.
    assert (
        project / "src" / "main.py"
    ).read_text(encoding="utf-8") == "print('a')\n"

    applied = workspace.apply_to_project(project)

    assert applied == ["src/main.py"]
    assert (
        project / "src" / "main.py"
    ).read_text(encoding="utf-8") == "print('b')\n"
