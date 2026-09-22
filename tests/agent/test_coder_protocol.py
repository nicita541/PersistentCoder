from __future__ import annotations

import json
from pathlib import Path

from app.agent.coder.agent import CodingAgent
from app.agent.coder.executor import (
    MAX_ENVELOPE_RETRIES,
    CodeExecutor,
)
from app.agent.coder.workspace import Workspace
from app.context.builder import ContextBuilder

from helpers import FakeLLM, coder_envelope


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

    return CodingAgent(executor)


def test_single_file_shape_is_accepted(tmp_path):
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "path": "out.txt",
                    "content": "hi",
                }
            )
        ]
    )

    agent = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    assert result.ok is True
    assert (tmp_path / "out.txt").read_text(
        encoding="utf-8"
    ) == "hi"


def test_action_synonyms_are_normalized(tmp_path):
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "action": "create",
                    "files": [
                        {
                            "path": "src/x.py",
                            "content": "X = 1\n",
                        }
                    ],
                }
            )
        ]
    )

    agent = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    assert result.ok is True
    assert (tmp_path / "src" / "x.py").exists()


def test_invalid_reply_is_re_prompted_then_succeeds(tmp_path):
    llm = FakeLLM(
        [
            "sorry, I cannot do that",
            coder_envelope(
                path="out.txt",
                content="hi",
            ),
        ]
    )

    agent = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    assert result.ok is True
    assert len(llm.calls) == 2

    second_prompt = "\n".join(
        str(message.get("content", ""))
        for message in llm.calls[1]
    )

    assert "SYSTEM FEEDBACK" in second_prompt
    assert "ACTION PROTOCOL" in second_prompt


def test_invalid_reply_gives_up_after_bounded_retries(
    tmp_path,
):
    llm = FakeLLM(default="still not json")

    agent = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    assert result.ok is False
    assert result.failure_reason == "invalid model response"
    assert (
        len(llm.calls)
        == MAX_ENVELOPE_RETRIES + 1
    )


def test_json_without_any_action_is_rejected(tmp_path):
    llm = FakeLLM(default=json.dumps({"foo": "bar"}))

    agent = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    assert result.ok is False
    assert "unknown action" in result.failure_reason


def test_describe_only_envelope_is_reprompted(tmp_path):
    llm = FakeLLM(
        [
            "```json\n"
            '{"action": "create", "file": "calculator.py"}'
            "\n```",
            coder_envelope(
                path="calculator.py",
                content="def add(a, b):\n    return a + b\n",
            ),
        ]
    )

    agent = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    assert result.ok is True
    assert (tmp_path / "calculator.py").exists()

    second_prompt = "\n".join(
        str(message.get("content", ""))
        for message in llm.calls[1]
    )

    assert "NO file content" in second_prompt
    assert "ACTION PROTOCOL" in second_prompt


def test_create_action_with_file_and_content_is_accepted(tmp_path):
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "action": "create",
                    "file": "calc.py",
                    "content": "VALUE = 1\n",
                }
            )
        ]
    )

    agent = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    assert result.ok is True
    assert (tmp_path / "calc.py").exists()


def test_describe_only_envelope_fails_after_bounded_retries(
    tmp_path,
):
    llm = FakeLLM(
        default=json.dumps(
            {"action": "create", "file": "calculator.py"}
        )
    )

    agent = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    assert result.ok is False
    assert "no files or tools" in result.summary
    assert not (tmp_path / "calculator.py").exists()


def test_markdown_fenced_envelope_is_accepted(tmp_path):
    llm = FakeLLM(
        [
            "```json\n"
            + json.dumps(
                {
                    "action": "edit",
                    "files": [
                        {
                            "path": "calc.py",
                            "content": (
                                "def add(a, b):\n"
                                "    return a + b\n"
                            ),
                        }
                    ],
                    "tools": [],
                }
            )
            + "\n```"
        ]
    )

    agent = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    # A fenced but valid envelope is normalized, not rejected.
    assert result.ok is True
    assert (tmp_path / "calc.py").exists()


def test_fenced_garbage_is_still_rejected(tmp_path):
    llm = FakeLLM(
        default="```json\nnot json at all\n```"
    )

    agent = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    assert result.ok is False
    assert result.failure_reason == "invalid model response"


def test_content_key_synonyms_are_accepted(tmp_path):
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "path": "calc.py",
                    "code": "def add(a, b):\n    return a + b\n",
                }
            )
        ]
    )

    agent = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    assert result.ok is True
    assert "def add" in (
        tmp_path / "calc.py"
    ).read_text(encoding="utf-8")


def test_path_to_content_mapping_is_accepted(tmp_path):
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "files": {
                        "pkg/mod.py": "VALUE = 1\n",
                    }
                }
            )
        ]
    )

    agent = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    assert result.ok is True
    assert (tmp_path / "pkg" / "mod.py").exists()


def test_unknown_action_is_not_guessed(tmp_path):
    llm = FakeLLM(
        default=json.dumps(
            {"summary": "I would add the function"}
        )
    )

    agent = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    # No files, no recognizable action: nothing is written.
    assert result.ok is False
    assert "unknown action" in result.failure_reason
    assert list(tmp_path.glob("*.py")) == []


def test_read_before_edit_is_still_enforced(tmp_path):
    (tmp_path / "out.txt").write_text(
        "old",
        encoding="utf-8",
    )

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "path": "out.txt",
                    "content": "new",
                }
            )
        ]
    )

    agent = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    assert result.ok is False
    assert "observe-before-edit" in result.failure_reason
    assert (tmp_path / "out.txt").read_text(
        encoding="utf-8"
    ) == "old"


def test_absolute_path_is_still_blocked(tmp_path):
    llm = FakeLLM(
        [
            json.dumps(
                {
                    "path": r"C:\outside.txt",
                    "content": "x",
                }
            )
        ]
    )

    agent = _agent(tmp_path, llm)

    result = agent.execute(FakeTask())

    assert result.ok is False
    assert not Path(r"C:\outside.txt").exists()


def test_delete_entry_must_not_include_content(tmp_path):
    (tmp_path / "out.txt").write_text("keep", encoding="utf-8")
    llm = FakeLLM(
        [
            json.dumps({"action": "read", "path": "out.txt"}),
            json.dumps(
                {
                    "action": "edit",
                    "files": [
                        {
                            "path": "out.txt",
                            "operation": "delete",
                            "content": "unexpected",
                        }
                    ],
                }
            ),
        ]
    )

    result = _agent(tmp_path, llm).execute(FakeTask())

    assert result.ok is False
    assert "delete entry must not include content" in result.failure_reason
    assert (tmp_path / "out.txt").exists()
