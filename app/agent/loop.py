from __future__ import annotations

from app.agent.state import (
    AgentPhase,
    AgentState,
)


class AgentLoopError(RuntimeError):
    pass


class AgentLoop:
    """
    Реально переключает фазы:

        PLANNING
        READY
        EXECUTING
        VERIFYING
        REPAIRING
        DONE / FAILED
    """

    def __init__(
        self,
        controller,
        *,
        max_iterations: int = 1000,
    ) -> None:
        self.controller = controller
        self.max_iterations = max_iterations

    def run(
        self,
        request: str,
    ) -> AgentState:
        state = self.controller.observe(
            request
        )

        state.record_phase()

        iterations = 0

        while not state.is_terminal:
            iterations += 1

            if iterations > self.max_iterations:
                raise AgentLoopError(
                    "agent loop did not terminate "
                    f"after {self.max_iterations} "
                    "iterations"
                )

            phase = state.phase

            if phase is AgentPhase.PLANNING:
                self.controller.plan(state)

            elif phase is AgentPhase.READY:
                self.controller.advance(state)

            elif phase is AgentPhase.EXECUTING:
                self.controller.execute(state)

            elif phase is AgentPhase.VERIFYING:
                self.controller.verify(state)

            elif phase is AgentPhase.REPAIRING:
                self.controller.repair(state)

            else:
                raise AgentLoopError(
                    f"unknown phase: {phase}"
                )

            state.record_phase()

        if state.phase is AgentPhase.DONE:
            self.controller.remember(state)

        return state

