from __future__ import annotations

import importlib.util
from pathlib import Path

from app.agent.events import AgentEvent
from app.agent.runtime import AgentRuntime
from app.sandbox.paths import PROJECT_ROOT
from app.sandbox.runner import SandboxCommandRunner

from helpers import (
    FakeLLM,
    coder_envelope,
    dependencies_response,
    goal_response,
    tasks_response,
)


def _runtime(tmp_path, llm):
    return AgentRuntime(
        workspace_root=tmp_path,
        database_path=tmp_path / "pc.db",
        llm=llm,
        load_policy=False,
    )


def test_llm_is_created_exactly_once(tmp_path):
    calls = {"count": 0}

    def factory():
        calls["count"] += 1
        return FakeLLM()

    runtime = AgentRuntime(
        workspace_root=tmp_path,
        database_path=tmp_path / "pc.db",
        llm_factory=factory,
        load_policy=False,
    )

    assert calls["count"] == 1
    assert runtime.llm is not None


def test_single_llm_instance_shared_between_agents(
    tmp_path,
):
    llm = FakeLLM()
    runtime = _runtime(tmp_path, llm)

    assert runtime.planner.llm is llm
    assert (
        runtime.planner.goal_analyzer.llm is llm
    )
    assert (
        runtime.planner.decomposer.llm is llm
    )
    assert (
        runtime.planner.dependency_builder.llm
        is llm
    )
    assert runtime.coder.executor.llm is llm
    assert runtime.executor.llm is llm
    assert runtime.repair_agent.llm is llm


def test_no_duplicate_subsystems_inside_agent_layer():
    for name in (
        "llm",
        "memory",
        "context",
        "tasks",
        "tools",
    ):
        assert (
            importlib.util.find_spec(
                f"app.agent.{name}"
            )
            is None
        ), name


def test_runtime_run_completes_plan(tmp_path):
    llm = FakeLLM(
        [
            goal_response(),
            tasks_response(),
            dependencies_response(),
        ],
        default=coder_envelope(),
    )

    runtime = _runtime(tmp_path, llm)

    state = runtime.run("Сделай API.")

    assert state.phase.value == "DONE"
    assert state.completion == "DONE"
    assert (tmp_path / "artifact.txt").exists()
    assert runtime.last_run_id is not None
    run = runtime.runtime_store.get_run(runtime.last_run_id)
    assert run is not None
    assert run["phase"] == "DONE"
    assert run["task_id"] is None
    assert run["step_id"] is None
    assert run["attempt_id"] is None
    assert run["checkpoint_id"] is None
    assert runtime.plan_store.get_plan(state.plan_id).status.value == "DONE"
    with runtime.state_authority._connect() as connection:
        committed = connection.execute(
            """
            SELECT COUNT(*) FROM file_operation_journal
            WHERE state = 'COMMITTED'
            """
        ).fetchone()[0]
    assert committed > 0
    assert runtime.state_authority.list_recovery_journals(
        runtime.last_run_id
    ) == []


def test_event_bus_cannot_change_existing_run_cursor(tmp_path):
    runtime = _runtime(tmp_path, FakeLLM())
    run_id = runtime.runtime_store.start_run("build")
    runtime.current_run_id = run_id
    runtime.runtime_store.update_run(
        run_id,
        plan_id=11,
        task_id=22,
        step_id=33,
        attempt_id=44,
        checkpoint_id="attempt-4",
    )

    runtime._persist_event(
        AgentEvent(
            "execute",
            {
                "path": "sample.py",
                "plan_id": 91,
                "task_id": 92,
                "step_id": 93,
                "attempt_id": 94,
                "checkpoint": "telemetry-checkpoint",
            },
        )
    )

    run = runtime.runtime_store.get_run(run_id)
    assert run is not None
    assert run["plan_id"] == 11
    assert run["task_id"] == 22
    assert run["step_id"] == 33
    assert run["attempt_id"] == 44
    assert run["checkpoint_id"] == "attempt-4"


def test_runtime_persists_each_phase_as_a_state_snapshot(tmp_path):
    llm = FakeLLM(
        [goal_response(), tasks_response(), dependencies_response()],
        default=coder_envelope(),
    )
    runtime = _runtime(tmp_path, llm)

    runtime.run("Сделай API.")

    with runtime.state_authority._connect() as connection:
        phases = [
            row[0]
            for row in connection.execute(
                """
                SELECT phase FROM agent_state_snapshots
                WHERE run_id = ? ORDER BY id
                """,
                (runtime.last_run_id,),
            ).fetchall()
        ]
    assert phases[0] == "PLANNING"
    assert phases[-1] == "DONE"
    assert {"READY", "EXECUTING", "VERIFYING", "DONE"} <= set(phases)


def test_production_workspace_has_no_host_command_runner(
    tmp_path,
):
    runtime = AgentRuntime(
        database_path=tmp_path / "pc.db",
        llm=FakeLLM(),
        load_policy=False,
    )

    # No host subprocess runner on the production workspace.
    assert not hasattr(runtime.workspace, "terminal")

    # Commands go exclusively through the Docker sandbox runner.
    assert isinstance(
        runtime.command_runner,
        SandboxCommandRunner,
    )
    assert (
        runtime.executor.command_runner
        is runtime.command_runner
    )


def test_done_writes_patch_into_sandbox_patches(
    tmp_path,
):
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

    assert patch.exists()
    assert patch.parent == runtime.project_storage.patches_root
    assert PROJECT_ROOT in patch.parents

    contents = patch.read_text(encoding="utf-8")

    assert "artifact.txt" in contents

    # The host project must NOT be touched automatically.
    assert not (
        PROJECT_ROOT / "artifact.txt"
    ).exists()

