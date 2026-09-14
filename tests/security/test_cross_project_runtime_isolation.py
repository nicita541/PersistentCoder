from __future__ import annotations

from pathlib import Path

from app.agent.runtime import AgentRuntime
from tests.helpers.legacy_sqlite import (
    seed_legacy_memory,
    seed_legacy_run,
)

from helpers import FakeLLM


def _project(path: Path) -> Path:
    path.mkdir()
    (path / "project.txt").write_text(path.name, encoding="utf-8")
    return path


def _runtime(project: Path, database: Path) -> AgentRuntime:
    return AgentRuntime(
        project_root=project,
        database_path=database,
        llm=FakeLLM(),
        load_policy=False,
    )


def test_project_b_never_recovers_project_a(tmp_path: Path) -> None:
    project_a = _project(tmp_path / "project-a")
    project_b = _project(tmp_path / "project-b")
    shared_database = tmp_path / "shared.db"
    crashed = _runtime(project_a, shared_database)
    assert crashed.session is not None
    run_id = crashed.runtime_store.start_run(
        "A",
        sandbox_session_id=crashed.session.sandbox_session_id,
    )

    restarted_b = _runtime(project_b, shared_database)

    assert restarted_b.interrupted_runs == []
    assert restarted_b.resumed_run_id is None
    assert restarted_b.session is not None
    assert restarted_b.session.project_id != crashed.session.project_id
    assert restarted_b.runtime_store.get_run(run_id) is None
    assert crashed.runtime_store.get_run(run_id) is not None


def test_legacy_unscoped_rows_are_not_adopted(tmp_path: Path) -> None:
    external_project = _project(tmp_path / "external")
    legacy_database = tmp_path / "legacy.db"
    seed_legacy_run(legacy_database, "legacy", "legacy-session")
    seed_legacy_memory(
        legacy_database,
        "FACT",
        "Legacy project fact",
    )

    runtime = _runtime(external_project, legacy_database)

    assert runtime.interrupted_runs == []
    assert runtime.runtime_store.get_run(1) is None
    assert runtime.memory_store.get_active_memories() == []
