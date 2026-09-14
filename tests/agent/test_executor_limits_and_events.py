from __future__ import annotations

import json
from dataclasses import replace

from app.agent.coder.executor import CodeExecutor
from app.agent.coder.workspace import Workspace
from app.sandbox.limits import DEFAULT_LIMITS
from app.sandbox.runner import CancellationToken
from app.tasks.change_scope import AllowedChangeSet

from helpers import FakeLLM


class FakeTask:
    key = "bounded"
    title = "Bounded"
    description = "Bound observations"
    success_criteria = ["out.txt exists"]


def test_cumulative_observation_budget_fails_closed(tmp_path):
    (tmp_path / "large.txt").write_text("1234567890", encoding="utf-8")
    executor = CodeExecutor(
        workspace=Workspace(tmp_path),
        llm=FakeLLM([json.dumps({"action": "read", "path": "large.txt"})]),
        limits=replace(DEFAULT_LIMITS, max_observe_bytes=8),
    )

    result = executor.execute(
        FakeTask(), allowed_changes=AllowedChangeSet(["out.txt"])
    )

    assert result.ok is False
    assert "observation budget exceeded" in result.failure_reason


def test_observation_and_edit_events_are_sanitized(tmp_path):
    (tmp_path / "source.txt").write_text("TOP SECRET CONTENT", encoding="utf-8")
    events = []
    executor = CodeExecutor(
        workspace=Workspace(tmp_path),
        llm=FakeLLM(
            [
                json.dumps({"action": "read", "path": "source.txt"}),
                json.dumps(
                    {
                        "action": "edit",
                        "files": [{"path": "out.txt", "content": "generated"}],
                    }
                ),
            ]
        ),
        on_event=lambda name, payload: events.append((name, payload)),
    )

    result = executor.execute(
        FakeTask(), allowed_changes=AllowedChangeSet(["out.txt"])
    )

    assert result.ok is True
    names = [name for name, _ in events]
    assert "file_read" in names
    assert "edit_applied" in names
    serialized = repr(events)
    assert "TOP SECRET CONTENT" not in serialized
    assert "generated" not in serialized


def test_pre_cancelled_executor_never_calls_model(tmp_path):
    token = CancellationToken()
    token.cancel()
    llm = FakeLLM(default="must not be called")
    executor = CodeExecutor(workspace=Workspace(tmp_path), llm=llm)

    result = executor.execute(FakeTask(), cancellation_token=token)

    assert result.ok is False
    assert "cancelled" in result.failure_reason
    assert llm.calls == []
