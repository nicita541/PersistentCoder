from __future__ import annotations

import importlib.util
from pathlib import Path

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

