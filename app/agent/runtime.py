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
from app.tasks.runtime_store import (
    DONE as RUN_DONE,
    FAILED as RUN_FAILED,
    INTERRUPTED as RUN_INTERRUPTED,
    RuntimeStore,
)
from app.agent.state import AgentPhase
from app.tools.file_tools import FileTools
from app.tools.project_tools import ProjectTools
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
        project_root: str | Path | None = None,
        database_path: str | Path | None = None,
        llm=None,
        llm_factory=None,
        system_prompt: str | None = None,
        load_policy: bool = True,
        max_step_attempts: int = 2,
        max_task_attempts: int = 3,
        use_llm_dependencies: bool = True,
        planner_repair_attempts: int = 1,
        planner_plan_repairs: int = 2,
    ) -> None:
        ensure_layout()

        if workspace_root is None:
            # Production: agents edit a sandbox snapshot,
            # never the host project directly.
            self.sandbox_workspace = (
                SandboxWorkspace.create(
                    project_root=(
                        project_root or PROJECT_ROOT
                    ),
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
        self.last_patch_path: str | None = None

        # Durable execution state + event log (project-local SQLite).
        self.runtime_store = RuntimeStore(
            self.database_path
        )

        # Fail closed on restart: any run left RUNNING by a crash is
        # marked INTERRUPTED and never silently treated as DONE.
        self.interrupted_runs = (
            self.runtime_store.recover_interrupted()
        )

        self.current_run_id: int | None = None
        self.last_run_id: int | None = None

        self.events.subscribe(self._persist_event)

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

        # NOTE: production has NO host TerminalTools / host subprocess
        # runner. Every model command goes through SandboxCommandRunner.

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
            max_plan_repairs=(
                planner_plan_repairs
            ),
            use_llm_dependencies=(
                use_llm_dependencies
            ),
        )

        self.workspace = Workspace(
            self.workspace_root,
            file_tools=self.file_tools,
            project_tools=self.project_tools,
        )

        self.command_runner = SandboxCommandRunner(
            sandbox_root=self.workspace_root,
        )

        # Session-wide set of files the agent itself created.
        self.known_files: set[str] = set()

        self.executor = CodeExecutor(
            workspace=self.workspace,
            llm=self.llm,
            context=self.context,
            system_prompt=self.system_prompt,
            command_runner=self.command_runner,
            known_files=self.known_files,
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
                workspace=self.workspace,
                command_runner=self.command_runner,
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
            system_prompt=self.system_prompt,
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
            sandbox_workspace=self.sandbox_workspace,
            attempt_store=self.attempt_store,
            max_step_attempts=max_step_attempts,
            max_task_attempts=max_task_attempts,
        )

    def run(self, request: str):
        run_id = self.runtime_store.start_run(request)
        self.current_run_id = run_id
        self.last_run_id = run_id

        if self.sandbox_workspace is not None:
            self.runtime_store.update_run(
                run_id,
                sandbox_session_id=(
                    self.sandbox_workspace.session_id
                ),
            )

        try:
            state = AgentLoop(self.controller).run(
                request
            )

        except Exception:
            # Crash/interrupt during the run: never DONE.
            self.runtime_store.finish_run(
                run_id,
                RUN_INTERRUPTED,
            )
            self.current_run_id = None
            raise

        self.last_patch_path = None

        if (
            state.phase is AgentPhase.DONE
            and self.sandbox_workspace is not None
        ):
            patch = (
                self.sandbox_workspace.write_patch()
            )

            if patch is not None:
                self.last_patch_path = str(patch)
                state.patch_path = str(patch)

        status = (
            RUN_DONE
            if state.phase is AgentPhase.DONE
            else RUN_FAILED
        )

        self.runtime_store.finish_run(run_id, status)
        self.current_run_id = None

        return state

    def _persist_event(self, event) -> None:
        """
        Persist a compact event record (no prompts / file contents).

        Durable logging must never break the agent loop.
        """

        run_id = self.current_run_id

        if run_id is None:
            return

        payload = event.payload or {}

        try:
            self.runtime_store.update_run(
                run_id,
                plan_id=payload.get("plan_id"),
                task_id=payload.get("task_id"),
                step_id=payload.get("step_id"),
                attempt_id=payload.get("attempt_id"),
                checkpoint_id=payload.get(
                    "checkpoint"
                ),
                phase=(
                    event.name.upper()
                    if event.name
                    in (
                        "plan",
                        "execute",
                        "verify",
                        "repair",
                    )
                    else None
                ),
            )

            self.runtime_store.log_event(
                run_id,
                event.name,
                plan_id=payload.get("plan_id"),
                task_id=payload.get("task_id"),
                step_id=payload.get("step_id"),
                attempt_id=payload.get("attempt_id"),
                payload=payload,
            )

        except Exception:
            pass

    def sandbox_status(self) -> str:
        return self.command_runner.status()


