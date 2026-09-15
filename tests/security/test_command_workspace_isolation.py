from __future__ import annotations

import subprocess

from app.sandbox.limits import SandboxLimits
from app.sandbox.runner import CancellationToken, SandboxCommandRunner


def test_persistent_workspace_is_read_only_input(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = SandboxCommandRunner(
        sandbox_root=workspace,
        limits=SandboxLimits(max_command_workspace_bytes=123456),
    )

    arguments = runner.command_arguments("python -m pytest -q")

    mount = arguments[arguments.index("--mount") + 1]
    assert mount == f"type=bind,source={workspace},target=/input,readonly"
    tmpfs = arguments[arguments.index("--tmpfs") + 1]
    assert tmpfs == "/workspace:rw,size=123456,uid=1000,gid=1000,mode=1770"
    assert ":/workspace:rw" not in " ".join(arguments)


def test_fixed_wrapper_copies_input_before_separate_model_command(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = SandboxCommandRunner(sandbox_root=workspace)
    model_command = "python -c \"print('ok')\""

    arguments = runner.command_arguments(model_command, cwd="tests")

    script_index = arguments.index("-lc") + 1
    wrapper = arguments[script_index]
    assert "cp -a /input/. /workspace/" in wrapper
    assert model_command not in wrapper
    assert arguments[-1] == model_command
    assert arguments[-2] == "/workspace/tests"


def test_pre_cancelled_command_never_probes_or_starts_docker(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    probes = {"count": 0}

    def probe() -> bool:
        probes["count"] += 1
        return True

    token = CancellationToken()
    token.cancel()
    runner = SandboxCommandRunner(sandbox_root=workspace, daemon_probe=probe)

    result = runner.run("pytest -q", cancellation_token=token)

    assert result.returncode == 130
    assert "cancelled" in result.stderr
    assert probes["count"] == 0


def test_active_cancellation_kills_named_container(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    token = CancellationToken()
    killed = {"process": False, "container": False}

    class FakeProcess:
        returncode = None

        def communicate(self, timeout=None):
            if not killed["process"]:
                token.cancel()
                raise subprocess.TimeoutExpired("docker", timeout)
            return "", ""

        def poll(self):
            return None if not killed["process"] else 130

        def kill(self):
            killed["process"] = True

    runner = SandboxCommandRunner(
        sandbox_root=workspace,
        daemon_probe=lambda: True,
    )
    monkeypatch.setattr(runner, "ensure_image", lambda: True)
    monkeypatch.setattr(
        "app.sandbox.runner.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )

    def fake_run(arguments, **kwargs):
        assert arguments[:2] == ["docker", "kill"]
        killed["container"] = True
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr("app.sandbox.runner.subprocess.run", fake_run)

    result = runner.run("pytest -q", cancellation_token=token)

    assert result.returncode == 130
    assert killed == {"process": True, "container": True}
