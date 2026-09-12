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
