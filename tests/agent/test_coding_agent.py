from __future__ import annotations

from pathlib import Path

from app.agent.coder.agent import CodingAgent
from app.agent.coder.executor import CodeExecutor
from app.agent.coder.workspace import Workspace
from app.context.builder import ContextBuilder

from helpers import (
    FakeLLM,
    RecordingCommandRunner,
    coder_envelope,
    envelope,
)


class FakeTask:
    key = "build"
    title = "Build artifact"
    description = "Create a real artifact file."
    success_criteria = ["artifact exists"]


def _agent(tmp_path, llm, runner=None):
    workspace = Workspace(tmp_path)

    context = ContextBuilder(
        project=workspace.project,
    )

    executor = CodeExecutor(
        workspace=workspace,
        llm=llm,
        context=context,
        command_runner=runner,
    )

    return CodingAgent(executor), workspace



def test_applies_real_file_change(tmp_path):
    llm = FakeLLM(
        [
            coder_envelope(
                path="out.txt",
                content="hi",
            )
        ]
    )

    agent, _ = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    assert result.ok is True
    assert (
        tmp_path / "out.txt"
    ).read_text(encoding="utf-8") == "hi"
    assert "out.txt" in result.artifacts
    assert result.evidence


def test_legacy_command_is_refused_before_file_write(tmp_path):
    command = 'python -c "print(123)"'

    llm = FakeLLM(
        [
            envelope(
                files=[{"path": "artifact.txt", "content": "hello"}],
                commands=[command],
            )
        ]
    )

    agent, _ = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    # Legacy model-provided shell text is rejected before any write.
    assert result.ok is False
    assert "arbitrary commands" in result.failure_reason
    assert result.commands == []
    assert not (tmp_path / "artifact.txt").exists()


def test_typed_tool_runs_through_injected_sandbox_runner(
    tmp_path,
):
    tool = {"tool": "python_module", "module": "demo", "args": ["--check"]}

    runner = RecordingCommandRunner(
        stdout="1 passed"
    )

    llm = FakeLLM([coder_envelope(tool=tool)])

    agent, _ = _agent(tmp_path, llm, runner)

    result = agent.execute(FakeTask())

    assert runner.commands == []
    assert runner.argv_commands == [["python", "-m", "demo", "--check"]]
    assert result.ok is True
    assert result.commands[0].ok is True
    assert "1 passed" in result.commands[0].stdout



def test_executor_reports_stage_timings(tmp_path):
    events: list[tuple[str, dict]] = []

    workspace = Workspace(tmp_path)

    executor = CodeExecutor(
        workspace=workspace,
        llm=FakeLLM(
            [coder_envelope(path="out.txt", content="hi")]
        ),
        context=ContextBuilder(project=workspace.project),
        on_event=lambda name, payload: events.append(
            (name, payload)
        ),
    )

    result = executor.execute(FakeTask())

    assert result.ok is True

    names = [name for name, _payload in events]

    assert "context_selection" in names
    assert "llm_tool_iteration" in names

    for name, payload in events:
        assert isinstance(payload.get("duration_ms"), int)
        assert payload["duration_ms"] >= 0


def test_docker_command_timing_is_reported(tmp_path):
    events: list[tuple[str, dict]] = []

    workspace = Workspace(tmp_path)

    executor = CodeExecutor(
        workspace=workspace,
        llm=FakeLLM(
            [
                coder_envelope(
                    tool={"tool": "python_module", "module": "demo", "args": []}
                )
            ]
        ),
        context=ContextBuilder(project=workspace.project),
        command_runner=RecordingCommandRunner(stdout="1 passed"),
        on_event=lambda name, payload: events.append(
            (name, payload)
        ),
    )

    result = executor.execute(FakeTask())

    assert result.ok is True

    docker = [
        payload
        for name, payload in events
        if name == "docker_command"
    ]

    assert docker
    assert docker[0]["returncode"] == 0
    assert docker[0]["duration_ms"] >= 0


def test_invalid_json_is_not_fake_success(tmp_path):
    llm = FakeLLM(["this is not json"])

    agent, _ = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    assert result.ok is False
    assert result.failure_reason


def test_empty_envelope_is_not_fake_success(tmp_path):
    llm = FakeLLM([envelope()])

    agent, _ = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    assert result.ok is False
    assert "nothing" in result.summary


def test_failed_typed_tool_is_not_ok(tmp_path):
    tool = {"tool": "python_module", "module": "demo", "args": []}

    runner = RecordingCommandRunner(
        returncode=3,
        stderr="failed",
    )

    llm = FakeLLM([coder_envelope(tool=tool)])

    agent, _ = _agent(tmp_path, llm, runner)

    result = agent.execute(FakeTask())

    assert result.ok is False
    assert result.commands[0].returncode == 3


def test_absolute_path_in_envelope_is_blocked(tmp_path):
    llm = FakeLLM(
        [
            coder_envelope(
                path=r"C:\outside.txt",
                content="x",
            )
        ]
    )

    agent, _ = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    assert result.ok is False
    assert not Path(r"C:\outside.txt").exists()



def test_uses_workspace_file_tools(tmp_path):
    llm = FakeLLM(
        [
            coder_envelope(
                path="nested/deep.txt",
                content="x",
            )
        ]
    )

    agent, _ = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    assert result.ok is True
    assert (
        tmp_path / "nested" / "deep.txt"
    ).exists()
