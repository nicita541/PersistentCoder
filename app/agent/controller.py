from app.agent.state import AgentState, AgentPhase

class AgentController:
    def __init__(self, planner, coder, verifier, repair, memory=None):
        self.planner=planner
        self.coder=coder
        self.verifier=verifier
        self.repair=repair
        self.memory=memory

    def state(self, request):
        if not request.strip():
            raise ValueError('request required')
        return AgentState(request=request)

    def step(self, state):
        if state.phase is AgentPhase.OBSERVE:
            state.facts['request']=state.request
            state.phase=AgentPhase.PLAN
        elif state.phase is AgentPhase.PLAN:
            state.facts['plan']=self.planner.plan(state.request)
            state.phase=AgentPhase.EXECUTE
        elif state.phase is AgentPhase.EXECUTE:
            state.facts['execution']=self.coder.execute(state.facts['plan']['draft'])
            state.phase=AgentPhase.VERIFY
        elif state.phase is AgentPhase.VERIFY:
            state.facts['verification']=self.verifier.verify(state.facts['execution'])
            state.phase=AgentPhase.REMEMBER if state.facts['verification'].get('ok') else AgentPhase.REPAIR
        elif state.phase is AgentPhase.REPAIR:
            state.facts['repair']=self.repair.repair(state.facts)
            state.phase=AgentPhase.EXECUTE
        elif state.phase is AgentPhase.REMEMBER:
            if self.memory:
                self.memory.remember(state.facts)
            state.phase=AgentPhase.DONE
        state.history.append(state.phase.value)
        return state
