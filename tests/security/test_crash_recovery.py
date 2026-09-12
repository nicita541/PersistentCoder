from __future__ import annotations

from pathlib import Path

from app.agent.runtime import AgentRuntime
from app.tasks.models import AttemptStatus
from app.tasks.runtime_store import INTERRUPTED

from helpers import (
    FakeLLM,
    make_stores,
    seed_plan,
)


def _project(root: Path) -> Path:
    (root / "src").mkdir(parents=True)

    (root / "src" / "app.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )

    return root


def _runtime(project: Path, database: Path) -> AgentRuntime:
    return AgentRuntime(
        project_root=project,
        database_path=database,
        llm=FakeLLM(),
        system_prompt="GLOBAL SYSTEM POLICY",
    )


def test_crash_rolls_back_sandbox_and_marks_attempt_blocked(
    tmp_path,
):
    project = _project(tmp_path / "project")

    stores = make_stores(tmp_path)
    _plan_id, task, step = seed_plan(stores)

    # ---------------- runtime #1 (the crash) ----------------

    crashed = _runtime(project, stores.database_path)

    session_id = crashed.session_id
    workspace = Path(crashed.workspace_root)

    run_id = crashed.runtime_store.start_run("fix src/app.py")

    # attempt START: checkpoint + durable attempt record
    crashed.sandbox_workspace.checkpoint("attempt-1")

    attempt = crashed.attempt_store.start_step_attempt(
        step.id
    )

    crashed.runtime_store.update_run(
        run_id,
        sandbox_session_id=session_id,
        task_id=task.id,
        step_id=step.id,
        attempt_id=attempt.id,
        checkpoint_id="attempt-1",
    )

    # half-written attempt, then the process dies (no finish_run)
    (workspace / "src" / "app.py").write_text(
        "VALUE = 2\n",
        encoding="utf-8",
    )
    (workspace / "src" / "broken.py").write_text(
        "oops\n",
        encoding="utf-8",
    )

    assert crashed.runtime_store.get_run(run_id)[
        "status"
    ] == "RUNNING"

    # ---------------- runtime #2 (the restart) ----------------

    restarted = _runtime(
        project,
        stores.database_path,
    )

    run = restarted.runtime_store.get_run(run_id)

    assert run["status"] == INTERRUPTED
    assert run["recovery_note"]

    # The sandbox is back at the last committed checkpoint.
    assert (
        Path(restarted.workspace_root)
        / "src"
        / "app.py"
    ).read_text(encoding="utf-8") == "VALUE = 1\n"

    assert not (
        Path(restarted.workspace_root)
        / "src"
        / "broken.py"
    ).exists()

    # ... and the runtime resumed the restored session.
    assert restarted.session_id == session_id
    assert restarted.resumed_session_id == session_id
    assert restarted.resumed_run_id == run_id

    # The unfinished attempt is NOT pass/done.
    recovered_attempt = (
        restarted.attempt_store.get_attempt(
            attempt.id
        )
    )

    assert (
        recovered_attempt.status
        is AttemptStatus.BLOCKED
    )

    assert restarted.interrupted_attempts == [
        attempt.id
    ]

    # A recovery event is durably logged.
    events = restarted.runtime_store.get_events(run_id)

    assert any(
        event["event_type"] == "recovery"
        for event in events
    )

    # The host project was never touched.
    assert (project / "src" / "app.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"

    assert not (project / "src" / "broken.py").exists()


def test_crash_without_checkpoint_starts_a_fresh_sandbox(
    tmp_path,
):
    project = _project(tmp_path / "project")

    stores = make_stores(tmp_path)
    _plan_id, _task, _step = seed_plan(stores)

    crashed = _runtime(project, stores.database_path)

    run_id = crashed.runtime_store.start_run("no checkpoint")

    crashed.runtime_store.update_run(
        run_id,
        sandbox_session_id=crashed.session_id,
    )

    restarted = _runtime(project, stores.database_path)

    run = restarted.runtime_store.get_run(run_id)

    assert run["status"] == INTERRUPTED
    assert "committed state" in str(run["recovery_note"])

    # Nothing to restore, so the runtime must not hijack the old
    # session: it starts from a fresh, clean copy.
    assert restarted.resumed_run_id is None
    assert restarted.session_id != crashed.session_id


def test_resume_can_be_disabled_explicitly(
    tmp_path,
):
    project = _project(tmp_path / "project")

    stores = make_stores(tmp_path)
    _plan_id, task, step = seed_plan(stores)

    crashed = _runtime(project, stores.database_path)

    run_id = crashed.runtime_store.start_run("crash")

    crashed.sandbox_workspace.checkpoint("attempt-1")
    crashed.runtime_store.update_run(
        run_id,
        sandbox_session_id=crashed.session_id,
        checkpoint_id="attempt-1",
    )

    (Path(crashed.workspace_root) / "src" / "app.py").write_text(
        "VALUE = 9\n",
        encoding="utf-8",
    )

    restarted = AgentRuntime(
        project_root=project,
        database_path=stores.database_path,
        llm=FakeLLM(),
        system_prompt="GLOBAL SYSTEM POLICY",
        resume_interrupted=False,
    )

    # Recovery still happens...
    assert restarted.recovery
    assert restarted.recovery[0]["restored"] is True

    # ... but the runtime keeps a clean session of its own.
    assert restarted.resumed_run_id is None
    assert restarted.session_id != crashed.session_id

    # The old session itself was restored to its checkpoint.
    reopened = Path(crashed.workspace_root)
    assert (reopened / "src" / "app.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"

    _ = (task, step)
