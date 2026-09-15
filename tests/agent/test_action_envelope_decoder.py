from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent.coder.protocol import ActionEnvelopeDecoder, ProtocolError
from app.tasks.change_scope import AllowedChangeSet


FIXTURES = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "qwen_action_protocol.json")
    .read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", FIXTURES, ids=lambda case: case["name"])
def test_sanitized_qwen_regression_corpus(case):
    decoded = ActionEnvelopeDecoder().decode(
        case["raw"],
        allowed_changes=AllowedChangeSet(["src/app.py"]),
    )

    assert decoded["action"] == case["expected_action"]
    assert decoded["files"][0]["path"] == case["expected_path"]


def test_root_prefix_normalizes_only_to_unique_allowed_suffix():
    decoded = ActionEnvelopeDecoder().decode(
        json.dumps(
            {
                "action": "edit",
                "files": [{"path": "root/app.py", "content": "x"}],
            }
        ),
        allowed_changes=AllowedChangeSet(["src/app.py"]),
    )

    assert decoded["files"][0]["path"] == "src/app.py"


def test_ambiguous_root_prefix_is_rejected():
    with pytest.raises(ProtocolError, match="ambiguous"):
        ActionEnvelopeDecoder().decode(
            json.dumps(
                {
                    "action": "edit",
                    "files": [{"path": "root/app.py", "content": "x"}],
                }
            ),
            allowed_changes=AllowedChangeSet(
                ["src/app.py", "tests/app.py"]
            ),
        )


def test_unknown_alias_is_not_guessed():
    decoded = ActionEnvelopeDecoder().decode(
        '{"action":"execute_magic","path":"src/app.py"}'
    )

    assert decoded["action"] == "execute_magic"


def test_arbitrary_python_literal_is_rejected_without_eval():
    with pytest.raises(ProtocolError, match="valid JSON"):
        ActionEnvelopeDecoder().decode(
            "{'action': 'edit', 'files': [('src/app.py', 'x')]}"
        )


def test_delete_shape_is_canonicalized():
    decoded = ActionEnvelopeDecoder().decode(
        '{"action":"delete","path":"old.py"}',
        allowed_changes=AllowedChangeSet(["old.py"]),
    )

    assert decoded == {
        "action": "edit",
        "files": [{"path": "old.py", "operation": "delete"}],
        "commands": [],
    }


def test_nested_file_content_alias_is_canonicalized():
    decoded = ActionEnvelopeDecoder().decode(
        '{"action":"write","files":[{"path":"sample.py","code":"x = 1"}]}'
    )

    assert decoded["files"] == [
        {"path": "sample.py", "content": "x = 1"}
    ]
