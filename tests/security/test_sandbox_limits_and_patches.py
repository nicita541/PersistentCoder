from __future__ import annotations

import subprocess

import pytest

from app.sandbox.limits import (
    LimitExceeded,
    SandboxLimits,
)
from app.sandbox.runner import SandboxCommandRunner
from app.sandbox.workspace import SandboxWorkspace
from app.tools.file_tools import (
    FileTools,
    FileToolsError,
)


# ==========================================
# FILESYSTEM ESCAPE
# ==========================================


def test_unc_path_is_blocked(tmp_path):
    tools = FileTools(tmp_path)

    with pytest.raises(FileToolsError):
        tools.write_text(r"\\server\share\evil.txt", "x")

    with pytest.raises(FileToolsError):
        tools.read_text(r"\\server\share\secret.txt")


def test_absolute_path_is_blocked(tmp_path):
    tools = FileTools(tmp_path)

    for path in (
        r"C:\evil.txt",
        r"D:\evil.txt",
        r"F:\outside.txt",
        "/etc/passwd",
    ):
        with pytest.raises(FileToolsError):
            tools.write_text(path, "x")


def test_junction_escape_is_blocked(tmp_path):
    """
    A junction/reparse point inside the sandbox that points outside
    must not allow a write to escape.
    """

    outside = tmp_path / "outside"
    outside.mkdir()

    sandbox = tmp_path / "sbx"
    sandbox.mkdir()

    link = sandbox / "link"

    created = subprocess.run(
        [
            "cmd",
            "/c",
            "mklink",
            "/J",
            str(link),
            str(outside),
        ],
        capture_output=True,
        text=True,
    )

    if created.returncode != 0 or not link.exists():
        pytest.skip("junction could not be created")

    tools = FileTools(sandbox)

    with pytest.raises(FileToolsError):
        tools.write_text("link/evil.txt", "x")

    assert not (outside / "evil.txt").exists()


# ==========================================
# RESOURCE LIMITS
# ==========================================


def test_max_file_bytes_is_enforced(tmp_path):
    tools = FileTools(
        tmp_path,
        limits=SandboxLimits(max_file_bytes=16),
    )

    with pytest.raises(LimitExceeded):
        tools.write_text("big.txt", "x" * 100)

    assert not (tmp_path / "big.txt").exists()


def test_workspace_growth_limit_is_enforced(tmp_path):
    tools = FileTools(
        tmp_path,
        limits=SandboxLimits(
            max_workspace_bytes=32,
            max_file_bytes=32,
        ),
    )

    tools.write_text("a.txt", "x" * 20)

    with pytest.raises(LimitExceeded):
        tools.write_text("b.txt", "y" * 20)


def test_command_output_is_truncated(tmp_path):
    (tmp_path / "ws").mkdir()

    runner = SandboxCommandRunner(
        sandbox_root=tmp_path / "ws",
        limits=SandboxLimits(max_output_bytes=32),
    )

    text = runner._truncate("z" * 500)

    assert text.startswith("z" * 32)
    assert "truncated" in text


def test_patch_size_limit_is_enforced(tmp_path):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "main.py").write_text(
        "print('a')\n",
        encoding="utf-8",
    )

    workspace = SandboxWorkspace.create(
        project_root=project,
        session_id="s1",
        sandbox_root=tmp_path / "sbx",
        limits=SandboxLimits(max_patch_bytes=16),
    )

    (
        workspace.workspace_root / "src" / "main.py"
    ).write_text(
        "print('" + ("b" * 500) + "')\n",
        encoding="utf-8",
    )

    with pytest.raises(LimitExceeded):
        workspace.write_patch()


# ==========================================
# PATCH SAFETY
# ==========================================


def test_patch_safety_filters_sensitive_paths():
    assert SandboxWorkspace.is_patch_safe(
        "src/main.py"
    )
    assert SandboxWorkspace.is_patch_safe(
        "sandbox_agent_test/calculator.py"
    )

    for unsafe in (
        ".env",
        "app/.env.local",
        ".git/config",
        ".venv/pyvenv.cfg",
        "models/x.bin",
        "data/persistent_coder.db",
        ".sandbox/sessions/x/y",
        "id_rsa",
        "credentials.json",
        "../escape.py",
        "C:/evil.py",
        "server/secrets.txt",
    ):
        assert not SandboxWorkspace.is_patch_safe(
            unsafe
        ), unsafe


def test_patch_excludes_sensitive_changed_files(
    tmp_path,
):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "main.py").write_text(
        "a\n",
        encoding="utf-8",
    )

    workspace = SandboxWorkspace.create(
        project_root=project,
        session_id="s2",
        sandbox_root=tmp_path / "sbx",
    )

    (
        workspace.workspace_root / "src" / "main.py"
    ).write_text("b\n", encoding="utf-8")

    (
        workspace.workspace_root / ".env"
    ).write_text("SECRET=1\n", encoding="utf-8")

    patch = workspace.write_patch()

    assert patch is not None

    contents = patch.read_text(encoding="utf-8")

    assert "src/main.py" in contents
    assert ".env" not in contents
    assert "SECRET" not in contents


# ==========================================
# DOCKER HARDENING / NO ENV LEAKAGE
# ==========================================


def test_docker_args_have_no_env_or_privileged(
    tmp_path,
):
    (tmp_path / "ws").mkdir()

    runner = SandboxCommandRunner(
        sandbox_root=tmp_path / "ws"
    )

    args = runner.command_arguments("python --version")

    assert runner.forbidden_flags_present(args) == []

    for forbidden in (
        "-e",
        "--env",
        "--env-file",
        "--privileged",
        "docker.sock",
        "--pid=host",
        "--network=host",
    ):
        assert forbidden not in args

    assert "--rm" in args


def test_docker_run_does_not_forward_host_env(
    tmp_path,
):
    """
    Without -e/--env the container only sees image ENV, so host
    secrets (HF token, SSH, HOME) are never injected.
    """

    (tmp_path / "ws").mkdir()

    runner = SandboxCommandRunner(
        sandbox_root=tmp_path / "ws"
    )

    args = runner.command_arguments("env")

    assert "-e" not in args
    assert "--env" not in args
    assert "--env-file" not in args


def test_command_timeout_and_rm(tmp_path):
    (tmp_path / "ws").mkdir()

    runner = SandboxCommandRunner(
        sandbox_root=tmp_path / "ws",
        timeout=5,
    )

    args = runner.command_arguments("sleep 100")

    assert "--rm" in args
    assert runner.timeout == 5

