from __future__ import annotations

from app.agent.loop import AgentLoop
from app.agent.runtime import AgentRuntime
from app.agent.state import (
    AgentPhase,
    AgentState,
)

from helpers import (
    FakeLLM,
    coder_envelope,
    dependencies_response,
    goal_response,
    tasks_response,
)


class StubController:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def observe(self, request):
        return AgentState(
            request=request,
            phase=AgentPhase.PLANNING,
        )

    def plan(self, state):
        self.calls.append("plan")
        state.phase = AgentPhase.READY
        return state

    def advance(self, state):
        self.calls.append("advance")
        state.phase = AgentPhase.EXECUTING
        return state

    def execute(self, state):
        self.calls.append("execute")
        state.phase = AgentPhase.VERIFYING
        return state

    def verify(self, state):
        self.calls.append("verify")
        state.phase = AgentPhase.DONE
        return state

    def remember(self, state):
        self.calls.append("remember")
        return state


def test_loop_switches_phases_in_order():
    controller = StubController()

    state = AgentLoop(controller).run("build")

    assert controller.calls == [
        "plan",
        "advance",
        "execute",
        "verify",
        "remember",
    ]

    assert state.history == [
        "PLANNING",
        "READY",
        "EXECUTING",
        "VERIFYING",
        "DONE",
    ]


def test_loop_reaches_failed_after_repair_gives_up(
    tmp_path,
):
    command = (
        'python -c "import sys; sys.exit(2)"'
    )

    llm = FakeLLM(
        [
            goal_response(),
            tasks_response(),
            dependencies_response(),
        ],
        default=coder_envelope(command=command),
    )

    runtime = AgentRuntime(
        workspace_root=tmp_path,
        database_path=tmp_path / "pc.db",
        llm=llm,
        load_policy=False,
    )

    state = runtime.run("Сделай API.")

    assert state.phase is AgentPhase.FAILED
    assert state.completion == "FAILED"
    assert "REPAIRING" in state.history
    assert state.history[-1] == "FAILED"
    assert runtime.plan_store.get_plan(state.plan_id).status.value == "FAILED"
    assert runtime.last_run_id is not None
    run = runtime.runtime_store.get_run(runtime.last_run_id)
    assert run is not None
    assert run["status"] == "FAILED"
    assert run["task_id"] is None
    assert run["step_id"] is None
    assert run["attempt_id"] is None
    assert run["checkpoint_id"] is None
