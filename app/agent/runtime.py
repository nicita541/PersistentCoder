from __future__ import annotations

from pathlib import Path

from app.agent.coder.agent import CodingAgent
from app.agent.coder.executor import CodeExecutor
from app.agent.coder.workspace import Workspace
from app.agent.controller import AgentController
from app.agent.events import EventBus
from app.agent.loop import AgentLoop
from app.agent.planner.agent import PlannerAgent
from app.agent.repair.agent import RepairAgent
from app.agent.repair.analyzer import FailureAnalyzer
from app.agent.repair.strategies import (
    RepairStrategySelector,
)
from app.agent.verifier.agent import (
    VerificationAgent,
)
from app.agent.verifier.evidence import (
    EvidenceCollector,
)
from app.agent.verifier.quality_gate import QualityGate
from app.context.builder import ContextBuilder
from app.memory.manager import MemoryManager
from app.memory.store import MemoryStore
from app.tasks.attempt_store import AttemptStore
from app.tasks.replan_store import ReplanStore
from app.tasks.replanner import Replanner
from app.tasks.scheduler import TaskScheduler
from app.tasks.step_store import StepStore
from app.tasks.store import PlanStore
from app.tasks.verification_store import (
    VerificationStore,
)
from app.tasks.verifier import Verifier
from app.tools.file_tools import FileTools
from app.tools.project_tools import ProjectTools
from app.tools.terminal_tools import TerminalTools
from app.sandbox.paths import ensure_layout
from app.sandbox.runner import SandboxCommandRunner
from app.sandbox.workspace import SandboxWorkspace


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def default_llm_factory():
    """
    Ленивая загрузка единственного LLM client.
    """

    from app.llm.client import QwenClient

    return QwenClient()


class AgentRuntime:
    """
    Единственная точка сборки dependency graph.

    Создаёт ровно один:
      - LLM client;
      - MemoryManager;
      - ContextBuilder;
      - Task OS stores/services;
      - Tools Layer.

    Затем передаёт общий LLM всем агентам.
    """

    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        database_path: str | Path | None = None,
        llm=None,
        llm_factory=None,
        system_prompt: str | None = None,
        load_policy: bool = True,
        max_step_attempts: int = 2,
        max_task_attempts: int = 3,
        use_llm_dependencies: bool = True,
        planner_repair_attempts: int = 1,
    ) -> None:
        ensure_layout()

        if workspace_root is None:
            # Production: agents edit a sandbox snapshot,
            # never the host project directly.
            self.sandbox_workspace = (
                SandboxWorkspace.create(
                    project_root=PROJECT_ROOT,
                )
            )
            self.workspace_root = (
                self.sandbox_workspace.workspace_root
            )

        else:
            # Explicit root (tests / framework tooling).
            self.sandbox_workspace = None
            self.workspace_root = Path(
                workspace_root
            ).resolve()

        self.database_path = (
            Path(database_path)
            if database_path is not None
            else None
        )

        self.events = EventBus()

        # ==================================
        # SINGLE LLM INSTANCE
        # ==================================

        if llm is not None:
            self.llm = llm

        else:
            factory = (
                llm_factory
                or default_llm_factory
            )
            self.llm = factory()

        # ==================================
        # POLICY
        # ==================================

        if system_prompt is not None:
            self.system_prompt = system_prompt

        elif load_policy:
            from app.policy.loader import (
                load_system_prompt,
            )

            self.system_prompt = (
                load_system_prompt()
            )

        else:
            self.system_prompt = ""

        # ==================================
        # TOOLS LAYER (single)
        # ==================================

        self.file_tools = FileTools(
            self.workspace_root
        )

        self.project_tools = ProjectTools(
            self.workspace_root,
            files=self.file_tools,
        )

        self.terminal = TerminalTools(
            cwd=self.workspace_root
        )

        # ==================================
        # TASK OS (single)
        # ==================================

        self.plan_store = PlanStore(
            self.database_path
        )
        self.step_store = StepStore(
            self.database_path
        )
        self.attempt_store = AttemptStore(
            self.database_path
        )
        self.verification_store = (
            VerificationStore(
                self.database_path
            )
        )
        self.replan_store = ReplanStore(
            self.database_path
        )

        self.scheduler = TaskScheduler(
            self.plan_store
        )

        self.task_verifier = Verifier(
            plan_store=self.plan_store,
            step_store=self.step_store,
            verification_store=(
                self.verification_store
            ),
        )

        self.replanner = Replanner(
            plan_store=self.plan_store,
            step_store=self.step_store,
            attempt_store=self.attempt_store,
            replan_store=self.replan_store,
            max_step_attempts=(
                max_step_attempts
            ),
        )

        # ==================================
        # MEMORY (single)
        # ==================================

        self.memory_store = MemoryStore(
            self.database_path
        )
        self.memory = MemoryManager(
            self.memory_store
        )

        # ==================================
        # CONTEXT (single)
        # ==================================

        self.context = ContextBuilder(
            memory=self.memory,
            project=self.project_tools,
            system_prompt=self.system_prompt,
        )

        # ==================================
        # AGENTS (all share one llm)
        # ==================================

        self.planner = PlannerAgent(
            self.llm,
            self.plan_store,
            step_store=self.step_store,
            max_repair_attempts=(
                planner_repair_attempts
            ),
            use_llm_dependencies=(
                use_llm_dependencies
            ),
        )

        self.workspace = Workspace(
            self.workspace_root,
            file_tools=self.file_tools,
            project_tools=self.project_tools,
            terminal=self.terminal,
        )

        self.command_runner = SandboxCommandRunner(
            sandbox_root=self.workspace_root,
        )

        self.executor = CodeExecutor(
            workspace=self.workspace,
            llm=self.llm,
            context=self.context,
            system_prompt=self.system_prompt,
            command_runner=self.command_runner,
        )

        self.coder = CodingAgent(self.executor)

        self.quality_gate = QualityGate()

        self.verification_agent = (
            VerificationAgent(
                quality_gate=self.quality_gate,
                verifier=self.task_verifier,
                plan_store=self.plan_store,
                step_store=self.step_store,
                verification_store=(
                    self.verification_store
                ),
                evidence_collector=(
                    EvidenceCollector()
                ),
            )
        )

        self.repair_agent = RepairAgent(
            analyzer=FailureAnalyzer(),
            strategies=RepairStrategySelector(
                max_step_attempts=(
                    max_step_attempts
                ),
                max_task_attempts=(
                    max_task_attempts
                ),
            ),
            replanner=self.replanner,
            attempt_store=self.attempt_store,
            replan_store=self.replan_store,
            llm=self.llm,
        )

        # ==================================
        # CONTROLLER
        # ==================================

        self.controller = AgentController(
            planner=self.planner,
            coder=self.coder,
            verifier=self.verification_agent,
            repair=self.repair_agent,
            scheduler=self.scheduler,
            plan_store=self.plan_store,
            step_store=self.step_store,
            memory=self.memory,
            events=self.events,
            max_step_attempts=max_step_attempts,
            max_task_attempts=max_task_attempts,
        )

    def run(self, request: str):
        return AgentLoop(self.controller).run(
            request
        )

    def sandbox_status(self) -> str:
        return self.command_runner.status()


