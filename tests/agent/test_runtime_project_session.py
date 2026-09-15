from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.runtime import AgentRuntime, DirtySessionError
from app.agent.state import AgentPhase, VerificationResult
from app.agent.session import SessionStatus
from app.tasks.verification_context import VerificationContext
from app.tasks.models import (
    PlanDraft,
    TaskDraft,
    TaskStatus,
    VerificationStatus,
)

from helpers import (
    FakeLLM,
    coder_envelope,
    dependencies_response,
    goal_response,
    tasks_response,
)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "value.txt").write_text("source-v1", encoding="utf-8")
    return project


def _runtime(tmp_path: Path) -> AgentRuntime:
    project = _project(tmp_path)
    return AgentRuntime(
        project_root=project,
        database_path=tmp_path / "runtime.db",
        llm=FakeLLM(
            [
                goal_response(),
                tasks_response(),
                dependencies_response(),
            ],
            default=coder_envelope(),
        ),
        load_policy=False,
    )


def test_runtime_binds_storage_before_opening_stores(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)

    assert runtime.project_identity.canonical_source_root == (
        tmp_path / "project"
    ).resolve()
    assert runtime.runtime_store.database_path == tmp_path / "runtime.db"
    assert runtime.runtime_store.context == runtime.store_context
    assert runtime.plan_store.context == runtime.store_context
    assert runtime.session is not None
    assert runtime.session.project_id == runtime.project_identity.project_id
    assert runtime.sandbox_workspace is not None
    assert (
        runtime.sandbox_workspace.project_id
        == runtime.project_identity.project_id
    )


def test_new_session_requires_explicit_dirty_resolution(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    assert runtime.session is not None
    assert runtime.sandbox_workspace is not None
    old_session_id = runtime.session.id
    runtime.session.transition(SessionStatus.RUNNING)
    runtime.session.transition(SessionStatus.DIRTY_VERIFIED)
    runtime.session = runtime.session_store.update(runtime.session)
    (runtime.workspace_root / "dirty.txt").write_text(
        "dirty",
        encoding="utf-8",
    )

    with pytest.raises(DirtySessionError):
        runtime.new_session()

    session = runtime.new_session(discard_dirty=True)

    assert session.id != old_session_id
    assert session.status is SessionStatus.CLEAN
    assert not (runtime.workspace_root / "dirty.txt").exists()
    assert runtime.sandbox_workspace.changed_files() == []


def test_rebase_session_after_apply_uses_current_source(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    assert runtime.session is not None
    assert runtime.sandbox_workspace is not None
    runtime.session.transition(SessionStatus.RUNNING)
    runtime.session.transition(SessionStatus.DIRTY_VERIFIED)
    runtime.session = runtime.session_store.update(runtime.session)
    (runtime.source_project_root / "value.txt").write_text(
        "source-v2",
        encoding="utf-8",
    )
    (runtime.workspace_root / "value.txt").write_text(
        "source-v2",
        encoding="utf-8",
    )

    runtime.rebase_session_after_apply()

    assert runtime.session.status is SessionStatus.CLEAN
    assert runtime.sandbox_workspace.changed_files() == []
    assert (runtime.workspace_root / "value.txt").read_text() == "source-v2"


def test_verified_run_claims_and_leaves_dirty_session(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)

    state = runtime.run("create artifact")

    assert state.phase is AgentPhase.DONE
    assert runtime.session is not None
    assert runtime.session.status is SessionStatus.DIRTY_VERIFIED
    assert runtime.session.active_run_id is None
    run = runtime.runtime_store.get_run(runtime.last_run_id)
    assert run is not None
    assert run["sandbox_session_id"] == runtime.session_id


def test_apply_rebases_session_to_clean_source(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    assert runtime.session is not None
    assert runtime.sandbox_workspace is not None
    (runtime.workspace_root / "value.txt").write_text(
        "sandbox-v2",
        encoding="utf-8",
    )
    runtime.last_patch_path = str(runtime.sandbox_workspace.write_patch())
    environment = {
        **runtime.command_runner.environment_identity(),
        "image_id": "sha256:test-image",
    }
    runtime.command_runner.verification_environment = lambda: (
        True,
        "available",
        environment,
    )
    verification_context = VerificationContext.capture(
        runtime.workspace_root,
        specs=[],
        environment={
            **environment,
            "python": runtime.verification_agent.structured.python,
        },
    )
    plan_id = runtime.plan_store.create_plan(
        PlanDraft(
            user_request="apply",
            global_goal="apply",
            tasks=[TaskDraft(title="apply", description="apply")],
        )
    )
    task = runtime.plan_store.get_tasks(plan_id)[0]
    runtime.plan_store.update_task_status(task.id, TaskStatus.DONE)
    runtime.verification_store.record_task(
        task.id,
        status=VerificationStatus.PASS,
        evidence=["verified"],
        context=verification_context,
    )
    runtime.last_state = type(
        "VerifiedState",
        (),
        {
            "phase": AgentPhase.DONE,
            "active_task_id": task.id,
            "verification": VerificationResult(
                ok=True,
                status="PASS",
                reason="verified",
                context=verification_context,
            ),
        },
    )()
    runtime.session.transition(SessionStatus.RUNNING)
    runtime.session.transition(SessionStatus.DIRTY_VERIFIED)
    runtime.session = runtime.session_store.update(runtime.session)

    result = runtime.apply_patch(confirmed=True)

    assert result["applied"] == ["value.txt"]
    assert (runtime.source_project_root / "value.txt").read_text() == (
        "sandbox-v2"
    )
    assert runtime.session.status is SessionStatus.CLEAN
    assert runtime.sandbox_workspace.changed_files() == []
