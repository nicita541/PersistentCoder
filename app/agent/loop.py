
from app.agent.state import AgentPhase

class AgentLoop:
    def __init__(self, controller):
        self.controller=controller

    def run(self, request: str):
        state=self.controller.state(request)
        while state.phase != AgentPhase.DONE:
            state=self.controller.step(state)
        return state
