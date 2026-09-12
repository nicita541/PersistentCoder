from __future__ import annotations

import subprocess

import pytest

from app.sandbox.policy import (
    CommandPolicy,
    PolicyViolation,
)
from app.sandbox.runner import SandboxCommandRunner
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
