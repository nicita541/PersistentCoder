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


def test_command_is_refused_without_sandbox(tmp_path):
    command = 'python -c "print(123)"'

    llm = FakeLLM([coder_envelope(command=command)])

    agent, _ = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    # No sandbox runner -> the command must NOT run on the host.
    assert result.ok is False
    assert "host" in result.failure_reason
    assert result.commands == []


def test_command_runs_through_injected_sandbox_runner(
    tmp_path,
):
    command = "pytest -q"

    runner = RecordingCommandRunner(
        stdout="1 passed"
    )

    llm = FakeLLM([coder_envelope(command=command)])

    agent, _ = _agent(tmp_path, llm, runner)

    result = agent.execute(FakeTask())

    assert runner.commands == [command]
    assert result.ok is True
    assert result.commands[0].ok is True
    assert "1 passed" in result.commands[0].stdout



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


def test_failed_command_is_not_ok(tmp_path):
    command = "pytest -q"

    runner = RecordingCommandRunner(
        returncode=3,
        stderr="failed",
    )

    llm = FakeLLM([coder_envelope(command=command)])

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
