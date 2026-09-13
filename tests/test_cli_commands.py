from __future__ import annotations

from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

import app.main as main_module
from app.agent.runtime import AgentRuntime
from app.agent.state import (
    AgentPhase,
    VerificationResult,
)

from helpers import (
    FakeLLM,
    coder_envelope,
    dependencies_response,
    goal_response,
    tasks_response,
)


def _capture(monkeypatch) -> StringIO:
    buffer = StringIO()

    monkeypatch.setattr(
        main_module,
        "console",
        Console(
            file=buffer,
            width=200,
            no_color=True,
        ),
    )

    return buffer


def _project(root: Path) -> Path:
    (root / "src").mkdir(parents=True)

    (root / "src" / "app.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )

    return root


def _runtime(tmp_path) -> AgentRuntime:
    project = _project(tmp_path / "project")

    return AgentRuntime(
        project_root=project,
        database_path=tmp_path / "pc.db",
        llm=FakeLLM(
            [
                goal_response(),
                tasks_response(),
                dependencies_response(),
            ],
            default=coder_envelope(),
        ),
        system_prompt="GLOBAL SYSTEM POLICY",
    )


def test_help_status_plan_memory_patch_are_available(
    tmp_path,
    monkeypatch,
):
    runtime = _runtime(tmp_path)

    buffer = _capture(monkeypatch)

    main_module.show_commands()
    main_module.render_status(runtime)
    main_module.render_plan(runtime)
    main_module.show_memory(runtime)
    main_module.render_patch(runtime)

    text = buffer.getvalue()

    for command in (
        "/status",
        "/plan",
        "/memory",
        "/patch",
        "/apply",
        "/exit",
    ):
        assert command in text

    assert "STATUS" in text
    assert "PLAN" in text
    assert "PATCH" in text


def test_status_and_plan_and_patch_after_a_run(
    tmp_path,
    monkeypatch,
):
    runtime = _runtime(tmp_path)

    state = runtime.run("Создай artifact.txt")

    assert state.phase is AgentPhase.DONE

    buffer = _capture(monkeypatch)

    main_module.render_status(runtime)
    main_module.render_plan(runtime)
    main_module.render_patch(runtime)

    text = buffer.getvalue()

    assert "DONE" in text
    assert "artifact.txt" in text
    assert "PASS" in text
    assert "NOT applied" in text


def test_timeline_reports_stage_durations(
    tmp_path,
    monkeypatch,
):
    runtime = _runtime(tmp_path)

    state = runtime.run("Создай artifact.txt")

    assert state.phase is AgentPhase.DONE

    timeline = runtime.timeline()

    assert timeline
    assert any(
        entry["event"] == "plan"
        for entry in timeline
    )

    # Durations are millisecond deltas from the durable event log.
    deltas = [
        entry["since_previous_ms"]
        for entry in timeline
        if entry["since_previous_ms"] is not None
    ]

    assert deltas
    assert all(
        isinstance(delta, int) and delta >= 0
        for delta in deltas
    )

    buffer = _capture(monkeypatch)

    main_module.render_timeline(runtime)

    text = buffer.getvalue()

    assert "TIMELINE" in text
    assert "plan" in text


def test_apply_is_refused_without_a_verified_done_run(
    tmp_path,
    monkeypatch,
):
    runtime = _runtime(tmp_path)

    buffer = _capture(monkeypatch)

    # No patch yet.
    main_module.apply_patch_with_confirmation(runtime)

    assert "нечего применять" in buffer.getvalue()

    # A patch exists, but the run did not verify: still refused.
    workspace = Path(runtime.workspace_root)

    (workspace / "src" / "app.py").write_text(
        "VALUE = 2\n",
        encoding="utf-8",
    )

    runtime.last_patch_path = str(
        runtime.sandbox_workspace.write_patch()
    )

    runtime.last_state = SimpleNamespace(
        phase=AgentPhase.FAILED,
        verification=VerificationResult(
            ok=False,
            status="FAIL",
            reason="criteria failed",
        ),
    )

    buffer = _capture(monkeypatch)

    main_module.apply_patch_with_confirmation(runtime)

    text = buffer.getvalue()

    assert "Apply запрещён" in text
    assert "не применены" in text
