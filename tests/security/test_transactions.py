from __future__ import annotations

import json
from pathlib import Path

from app.agent.coder.workspace import Workspace
from app.agent.coder.executor import CodeExecutor
from app.agent.runtime import AgentRuntime
from app.project_identity import ProjectIdentity
from app.context.builder import ContextBuilder
from app.sandbox.paths import PROJECT_ROOT
from app.sandbox.workspace import SandboxWorkspace
from app.tasks.store_context import StoreContext

from helpers import (
    FakeLLM,
    coder_envelope,
    dependencies_response,
    goal_response,
    tasks_response,
)


class _Task:
    key = "build"
    title = "Build"
    description = "Build."
    success_criteria = ["artifact.txt exists"]


# ==========================================
# PARTIAL ENVELOPE -> NOTHING WRITTEN
# ==========================================


def test_partial_envelope_writes_nothing(tmp_path):
    """
    write A -> discover invalid B must not leave A behind.
    """

    workspace = Workspace(tmp_path)

    llm = FakeLLM(
        [
            json.dumps(
                {
                    "files": [
                        {
                            "path": "a.txt",
                            "content": "A",
                        },
                        {
                            "path": r"C:\evil.txt",
                            "content": "B",
                        },
                    ],
                    "commands": [],
                }
            )
        ]
    )

    executor = CodeExecutor(
        workspace=workspace,
        llm=llm,
        context=ContextBuilder(
            project=workspace.project
        ),
    )

    result = executor.execute(_Task())

    assert result.ok is False
    assert not (tmp_path / "a.txt").exists()


# ==========================================
# CHECKPOINT / ROLLBACK
# ==========================================


def test_checkpoint_rollback_restores_workspace(
    tmp_path,
):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "main.py").write_text(
        "orig\n",
        encoding="utf-8",
    )

    workspace = SandboxWorkspace.create(
        project_root=project,
        session_id="s1",
        sandbox_root=tmp_path / "sbx",
    )

    workspace.checkpoint("attempt-1")

    (workspace.workspace_root / "src" / "main.py").write_text(
        "changed\n",
        encoding="utf-8",
    )
    (workspace.workspace_root / "src" / "new.py").write_text(
        "new\n",
        encoding="utf-8",
    )

    assert workspace.rollback("attempt-1") is True

    assert (
        workspace.workspace_root / "src" / "main.py"
    ).read_text(encoding="utf-8") == "orig\n"

    assert not (
        workspace.workspace_root / "src" / "new.py"
    ).exists()


def test_failed_attempt_rolls_back_sandbox(tmp_path):
    """
    A real failed attempt must not leave half-written files in the
    sandbox workspace.
    """

    command = 'python -c "import sys; sys.exit(2)"'

    llm = FakeLLM(
        [
            goal_response(),
            tasks_response(),
            dependencies_response(),
        ],
        default=coder_envelope(command=command),
    )

    runtime = AgentRuntime(
        database_path=tmp_path / "pc.db",
        llm=llm,
        load_policy=False,
    )

    state = runtime.run("Сделай API.")

    assert state.phase.value == "FAILED"

    # The coder wrote artifact.txt, then the command failed; the
    # attempt rollback must have removed it again.
    assert not (
        runtime.workspace_root / "artifact.txt"
    ).exists()


# ==========================================
# NO AUTO APPLY
# ==========================================


def test_done_never_auto_applies_patch(
    tmp_path,
    monkeypatch,
):
    def boom(self, *args, **kwargs):
        raise AssertionError(
            "apply_to_project must not be called "
            "by the agent loop"
        )

    monkeypatch.setattr(
        SandboxWorkspace,
        "apply_to_project",
        boom,
    )

    llm = FakeLLM(
        [
            goal_response(),
            tasks_response(),
            dependencies_response(),
        ],
        default=coder_envelope(),
    )

    runtime = AgentRuntime(
        database_path=tmp_path / "pc.db",
        llm=llm,
        load_policy=False,
    )

    state = runtime.run("Сделай API.")

    assert state.phase.value == "DONE"
    assert state.patch_path is not None

    patch = Path(state.patch_path)

    assert patch.parent == runtime.project_storage.patches_root
    assert patch.exists()

    # host project untouched
    assert not (PROJECT_ROOT / "artifact.txt").exists()


# ==========================================
# ATTEMPTSTORE LIFECYCLE
# ==========================================


def test_attempt_store_is_source_of_truth(tmp_path):
    llm = FakeLLM(
        [
            goal_response(),
            tasks_response(),
            dependencies_response(),
        ],
        default=coder_envelope(),
    )

    runtime = AgentRuntime(
        database_path=tmp_path / "pc.db",
        llm=llm,
        load_policy=False,
    )

    state = runtime.run("Сделай API.")

    assert state.phase.value == "DONE"

    statuses: list[str] = []

    for task in runtime.plan_store.get_tasks(
        state.plan_id
    ):
        for step in runtime.step_store.get_steps(
            task.id
        ):
            for attempt in (
                runtime.attempt_store
                .get_step_attempts(step.id)
            ):
                statuses.append(attempt.status.value)

    assert statuses, "attempt history must be persisted"

    assert all(
        status == "PASS"
        for status in statuses
    )


# ==========================================
# DURABLE EVENT LOG + CRASH RECOVERY
# ==========================================


def test_event_log_is_durable(tmp_path):
    llm = FakeLLM(
        [
            goal_response(),
            tasks_response(),
            dependencies_response(),
        ],
        default=coder_envelope(),
    )

    runtime = AgentRuntime(
        database_path=tmp_path / "pc.db",
        llm=llm,
        load_policy=False,
    )

    state = runtime.run("Сделай API.")

    assert state.phase.value == "DONE"

    run_id = runtime.last_run_id

    assert run_id is not None

    events = runtime.runtime_store.get_events(run_id)

    names = [event["event_type"] for event in events]

    assert "plan" in names
    assert "execute" in names
    assert "verify" in names

    run = runtime.runtime_store.get_run(run_id)

    assert run is not None
    assert run["status"] == "DONE"


def test_interrupted_run_is_detected_on_restart(tmp_path):
    from app.tasks.runtime_store import RuntimeStore

    database = tmp_path / "pc.db"

    identity = ProjectIdentity.from_source_root(PROJECT_ROOT)
    context = StoreContext(
        database_path=database,
        project_id=identity.project_id,
        canonical_source_root=str(identity.canonical_source_root),
    )
    store = RuntimeStore(context)

    run_id = store.start_run("half-done request")

    assert store.get_running_runs()

    # Simulate a restart: a fresh runtime must detect the run as
    # interrupted (never DONE) and refuse to auto-continue it.
    runtime = AgentRuntime(
        database_path=database,
        llm=FakeLLM(),
        load_policy=False,
    )

    assert runtime.interrupted_runs

    assert (
        runtime.runtime_store.get_run(run_id)["status"]
        == "INTERRUPTED"
    )

    assert runtime.runtime_store.get_running_runs() == []

    assert (
        runtime.runtime_store.get_run(run_id)[
            "recovery_note"
        ]
    )


