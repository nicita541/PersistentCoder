from __future__ import annotations

import json

import pytest

from app.agent.coder.executor import CodeExecutor
from app.agent.coder.tool_protocol import ToolProtocolError, decode_tool_calls
from app.agent.coder.workspace import Workspace
from app.tasks.change_scope import AllowedChangeSet

from helpers import FakeLLM, RecordingCommandRunner


class FakeTask:
    key = "typed-tools"
    title = "Exercise typed tools"
    description = "Use only a declared typed tool."
    success_criteria = ["tool was run"]


def test_py_compile_maps_to_fixed_argv():
    calls = decode_tool_calls(
        [{"tool": "py_compile", "paths": ["src/app.py"]}],
        allowed_changes=AllowedChangeSet(["src/app.py"]),
    )

    assert calls[0].argv == ("python", "-m", "py_compile", "src/app.py")


def test_pytest_accepts_only_allowlisted_options():
    calls = decode_tool_calls(
        [
            {
                "tool": "pytest",
                "targets": ["tests/test_app.py"],
                "options": ["-q", "--maxfail=1"],
            }
        ]
    )

    assert calls[0].argv == (
        "python",
        "-m",
        "pytest",
        "-q",
        "--maxfail=1",
        "tests/test_app.py",
    )

    with pytest.raises(ToolProtocolError, match="not allowlisted"):
        decode_tool_calls(
            [
                {
                    "tool": "pytest",
                    "targets": ["tests/test_app.py"],
                    "options": ["--rootdir=C:/"],
                }
            ]
        )


@pytest.mark.parametrize(
    "tool",
    [
        {"tool": "shell", "command": "whoami"},
        {"tool": "python_module", "module": "pip", "args": ["install", "x"]},
        {"tool": "python_file", "path": "../outside.py", "args": []},
        {"tool": "python_file", "path": "script.py", "args": ["C:/secret.txt"]},
    ],
)
def test_unsafe_or_unknown_tool_is_rejected(tool):
    with pytest.raises(ToolProtocolError):
        decode_tool_calls([tool])


def test_tool_target_must_be_in_exact_change_scope():
    with pytest.raises(ToolProtocolError, match="outside the exact Step change scope"):
        decode_tool_calls(
            [{"tool": "py_compile", "paths": ["src/other.py"]}],
            allowed_changes=AllowedChangeSet(["src/app.py"]),
        )


def test_executor_uses_argv_runner_and_never_string_runner(tmp_path):
    runner = RecordingCommandRunner(stdout="ok")
    response = json.dumps(
        {
            "action": "edit",
            "files": [{"path": "src/app.py", "content": "value = 1\n"}],
            "tools": [{"tool": "py_compile", "paths": ["src/app.py"]}],
        }
    )
    executor = CodeExecutor(
        workspace=Workspace(tmp_path),
        llm=FakeLLM([response]),
        command_runner=runner,
    )

    result = executor.execute(
        FakeTask(),
        allowed_changes=AllowedChangeSet(["src/app.py"]),
    )

    assert result.ok is True
    assert runner.commands == []
    assert runner.argv_commands == [
        ["python", "-m", "py_compile", "src/app.py"]
    ]


def test_legacy_command_is_rejected_before_any_write(tmp_path):
    response = json.dumps(
        {
            "action": "edit",
            "files": [{"path": "src/app.py", "content": "value = 1\n"}],
            "commands": ["python -c 'print(1)'"],
        }
    )
    executor = CodeExecutor(
        workspace=Workspace(tmp_path),
        llm=FakeLLM([response]),
    )

    result = executor.execute(FakeTask())

    assert result.ok is False
    assert "arbitrary commands are forbidden" in (result.failure_reason or "")
    assert not (tmp_path / "src" / "app.py").exists()


def test_missing_read_gets_non_repetition_feedback_then_can_create(tmp_path):
    read = json.dumps({"action": "read", "path": "src/app.py"})
    edit = json.dumps(
        {
            "action": "edit",
            "files": [{"path": "src/app.py", "content": "value = 1\n"}],
            "tools": [],
        }
    )
    llm = FakeLLM([read, edit])
    executor = CodeExecutor(
        workspace=Workspace(tmp_path),
        llm=llm,
    )

    result = executor.execute(
        FakeTask(),
        allowed_changes=AllowedChangeSet(["src/app.py"]),
    )

    assert result.ok is True
    second_prompt = "\n".join(item["content"] for item in llm.calls[1])
    assert "Do not repeat the same observation" in second_prompt
    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "value = 1\n"
