from __future__ import annotations

import pytest

from app.agent.runtime import AgentRuntime

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


def test_observe_rejects_empty_request(tmp_path):
    runtime = _runtime(tmp_path, FakeLLM())

    with pytest.raises(ValueError):
        runtime.controller.observe("   ")


def test_run_creates_plan_and_steps(tmp_path):
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

    assert state.plan_id is not None

    assert (
        runtime.plan_store.get_plan(
            state.plan_id
        )
        is not None
    )

    tasks = runtime.plan_store.get_tasks(
        state.plan_id
    )

    assert len(tasks) == 4

    assert all(
        task.status.value == "DONE"
        for task in tasks
    )

    for task in tasks:
        assert runtime.step_store.get_steps(
            task.id
        )


def test_run_records_memory_experience(tmp_path):
    llm = FakeLLM(
        [
            goal_response(),
            tasks_response(),
            dependencies_response(),
        ],
        default=coder_envelope(),
    )

    runtime = _runtime(tmp_path, llm)

    runtime.run("Сделай API.")

    memories = (
        runtime.memory.get_active_memories()
    )

    assert any(
        memory["type"] == "EXPERIENCE"
        and "завершён" in str(memory["content"])
        for memory in memories
    )


def test_controller_uses_injected_dependencies(
    tmp_path,
):
    runtime = _runtime(tmp_path, FakeLLM())

    assert (
        runtime.controller.memory is runtime.memory
    )
    assert (
        runtime.controller.plan_store
        is runtime.plan_store
    )
    assert (
        runtime.controller.verifier
        is runtime.verification_agent
    )


def test_blocked_dependency_gates_task_without_calling_coder(tmp_path):
    import json
    from types import SimpleNamespace

    task_payload = json.loads(tasks_response())
    task_payload["tasks"][0]["external_dependencies"] = ["requests"]
    llm = FakeLLM(
        [goal_response(), json.dumps(task_payload), dependencies_response()],
        default=coder_envelope(),
    )
    runtime = _runtime(tmp_path, llm)
    state = runtime.controller.observe("Build")
    runtime.controller.plan(state)
    runtime.controller.advance(state)
    calls_before = len(llm.calls)
    runtime.controller.dependency_plan = SimpleNamespace(
        status="BLOCKED", reason="build disabled"
    )

    runtime.controller.execute(state)

    task = runtime.plan_store.get_task(state.active_task_id)
    assert state.phase.value == "FAILED"
    assert task.status.value == "BLOCKED"
    assert "BLOCKED_DEPENDENCY" in state.execution.failure_reason
    assert len(llm.calls) == calls_before
