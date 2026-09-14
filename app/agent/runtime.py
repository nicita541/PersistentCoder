from __future__ import annotations

import time
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
from app.context.selector import RepoContextSelector
from app.memory.manager import MemoryManager
from app.memory.retrieval import MemoryRetriever
from app.memory.store import MemoryScope, MemoryStore
from app.policy.injection import (
    enforce_system_policy,
)
from app.tasks.attempt_store import AttemptStore
from app.tasks.models import AttemptStatus
from app.tasks.replan_store import ReplanStore
from app.tasks.replanner import Replanner
from app.tasks.scheduler import TaskScheduler
from app.tasks.step_store import StepStore
from app.tasks.store import PlanStore
from app.tasks.session_store import SessionStore
from app.tasks.store_context import StoreContext
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
from app.agent.session import SessionStatus
from app.project_identity import ProjectIdentity
from app.storage import ProjectStorage
from app.tools.file_tools import FileTools
from app.tools.project_tools import ProjectTools
from app.sandbox.paths import DATA_ROOT, ensure_layout
from app.sandbox.runner import SandboxCommandRunner
from app.sandbox.dependencies import (
    DependencyResolver,
)
from app.sandbox.workspace import SandboxWorkspace


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class DirtySessionError(RuntimeError):
    pass


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
        resume_interrupted: bool = True,
        allow_dependency_build: bool = False,
    ) -> None:
        ensure_layout()

        if project_root is not None:
            source_project_root = Path(project_root)
        elif workspace_root is not None:
            source_project_root = Path(workspace_root)
        else:
            source_project_root = PROJECT_ROOT

        self.project_identity = ProjectIdentity.from_source_root(
            source_project_root
        )
        self.source_project_root = (
            self.project_identity.canonical_source_root
        )
        self.project_storage = ProjectStorage.for_identity(
            self.project_identity
        )
        self.project_storage.ensure_layout()
        self.database_path = (
            Path(database_path)
            if database_path is not None
            else self.project_storage.database_path
        )
        self.store_context = StoreContext(
            database_path=self.database_path,
            project_id=self.project_identity.project_id,
            canonical_source_root=str(self.source_project_root),
        )

        self.events = EventBus()
        self.last_patch_path: str | None = None
        self.runtime_store = RuntimeStore(self.store_context)
        self.session_store = SessionStore(self.store_context)
        self.interrupted_runs = (
            self.runtime_store.recover_interrupted()
        )

        self.current_run_id: int | None = None
        self.last_run_id: int | None = None

        self.resumed_run_id: int | None = None
        self.resumed_session_id: str | None = None
        self.recovery: list[dict[str, object]] = []

        if workspace_root is None:
            # Production: agents edit a sandbox snapshot,
            # never the host project directly.
            self.sandbox_workspace = None
            self.workspace_root = (
                self.source_project_root
            )

            self.recovery = self.recover_sandboxes(
                resume=resume_interrupted
            )

            if self.sandbox_workspace is None:
                self.sandbox_workspace = (
                    SandboxWorkspace.create(
                        project_root=self.workspace_root,
                        identity=self.project_identity,
                        storage=self.project_storage,
                    )
                )

                self.workspace_root = (
                    self.sandbox_workspace
                    .workspace_root
                )

        else:
            # Explicit root (tests / framework tooling).
            self.sandbox_workspace = None
            self.workspace_root = Path(
                workspace_root
            ).resolve()

        if self.sandbox_workspace is not None:
            self.session_id: str | None = (
                self.sandbox_workspace.session_id
            )
            self.session = (
                self.session_store.get_by_sandbox_session_id(
                    self.session_id
                )
            )
            if self.session is None:
                agent_session_id = self.session_store.create(
                    sandbox_session_id=self.session_id
                )
                self.session = self.session_store.get(agent_session_id)
                if self.session is None:
                    raise RuntimeError("agent session was not created")

        else:
            self.session_id = None
            self.session = None

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
        # GLOBAL SYSTEM POLICY (every call)
        # ==================================

        # One LLM client is shared by every agent in this runtime.
        # Stage instructions never replace the System Policy: this
        # wrapper guarantees the policy is present in EVERY semantic
        # call, so no stage can silently forget it.
        self.llm = enforce_system_policy(
            self.llm,
            self.system_prompt,
        )

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
            self.store_context
        )
        self.step_store = StepStore(
            self.store_context
        )
        self.attempt_store = AttemptStore(
            self.store_context
        )
        self.verification_store = (
            VerificationStore(
                self.store_context
            )
        )
        self.replan_store = ReplanStore(
            self.store_context
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
            self.database_path,
            scope=MemoryScope.PROJECT,
            project_id=self.project_identity.project_id,
        )
        self.global_memory_store = MemoryStore(
            DATA_ROOT / "global" / "persistent_coder.db",
            scope=MemoryScope.GLOBAL,
        )
        self.memory = MemoryManager(
            project_store=self.memory_store,
            global_store=self.global_memory_store,
        )

        # ==================================
        # CONTEXT (single)
        # ==================================

        self.memory_retriever = MemoryRetriever()

        self.repo_selector = RepoContextSelector(
            self.project_tools,
        )

        self.context = ContextBuilder(
            memory=self.memory,
            project=self.project_tools,
            system_prompt=self.system_prompt,
            retriever=self.memory_retriever,
            selector=self.repo_selector,
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
            repo_selector=self.repo_selector,
        )

        self.workspace = Workspace(
            self.workspace_root,
            file_tools=self.file_tools,
            project_tools=self.project_tools,
        )

        self.command_runner = SandboxCommandRunner(
            sandbox_root=self.workspace_root,
        )

        # ==================================
        # DEPENDENCIES (framework-controlled, offline-first)
        # ==================================

        # The model can never install anything at runtime. When the
        # framework is explicitly allowed to, it prepares a derived
        # Docker image with the project's OWN manifests at build
        # time; otherwise the base image is used and the status is
        # reported as BLOCKED (never silently ignored).
        self.dependencies = DependencyResolver(
            allow_build=allow_dependency_build,
        )

        self.dependency_plan = None

        if self.sandbox_workspace is not None:
            self.dependency_plan = (
                self.dependencies.plan(
                    self.workspace_root
                )
            )

            if self.dependency_plan.ready:
                self.command_runner.image = (
                    self.dependency_plan.image
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
            on_event=self.events.emit,
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
            dependency_plan=self.dependency_plan,
        )

        # Last finished AgentState (CLI: /status, /patch, /apply).
        self.last_state = None

        # Finish the crash-recovery bookkeeping now that the
        # AttemptStore exists: an attempt that was IN_PROGRESS when
        # the process died becomes BLOCKED (never PASS, never DONE).
        self.interrupted_attempts = (
            self._mark_interrupted_attempts()
        )

    # ==================================
    # CRASH RECOVERY
    # ==================================

    def recover_sandboxes(
        self,
        *,
        resume: bool = True,
    ) -> list[dict[str, object]]:
        """
        Roll every crash-interrupted sandbox session back to its last
        committed checkpoint.

        The interrupted attempt is never treated as DONE and is never
        auto-continued: files written by the half-finished attempt are
        discarded, a recovery event is recorded, and only then may a
        safe resume happen.
        """

        recovered: list[dict[str, object]] = []

        for run in self.interrupted_runs:
            run_id = int(run["id"])
            session_id = run.get("sandbox_session_id")

            record: dict[str, object] = {
                "run_id": run_id,
                "session_id": session_id,
                "checkpoint": None,
                "restored": False,
                "resumed": False,
                "note": "",
            }

            if not session_id:
                record["note"] = (
                    "no sandbox session recorded; "
                    "nothing to restore"
                )

            else:
                try:
                    workspace = (
                        SandboxWorkspace.open_session(
                            str(session_id),
                            identity=self.project_identity,
                            storage=self.project_storage,
                        )
                    )

                except Exception as error:
                    workspace = None
                    record["note"] = (
                        "sandbox recovery error: "
                        f"{error}"
                    )

                if workspace is None:
                    if not record["note"]:
                        record["note"] = (
                            "sandbox session is gone; "
                            "nothing to restore"
                        )

                else:
                    label = (
                        run.get("checkpoint_id")
                        or workspace.latest_checkpoint()
                    )

                    restored = (
                        workspace.rollback(label)
                        if label
                        else False
                    )

                    record.update(
                        {
                            "checkpoint": label,
                            "restored": restored,
                            "note": (
                                "rolled back to checkpoint "
                                f"'{label}'"
                                if restored
                                else "no uncommitted checkpoint; "
                                "workspace already at last "
                                "committed state"
                            ),
                        }
                    )

                    self.runtime_store.log_event(
                        run_id,
                        "recovery",
                        payload={
                            "session_id": str(
                                session_id
                            ),
                            "checkpoint": label,
                            "restored": restored,
                        },
                    )

                    if (
                        resume
                        and restored
                        and self.resumed_run_id is None
                    ):
                        # Continue in the restored sandbox instead of
                        # silently starting from a fresh copy.
                        self.sandbox_workspace = (
                            workspace
                        )

                        self.workspace_root = (
                            workspace.workspace_root
                        )

                        self.resumed_run_id = run_id
                        self.resumed_session_id = str(
                            session_id
                        )

                        record["resumed"] = True

            try:
                self.runtime_store.set_recovery_note(
                    run_id,
                    str(record["note"]),
                )

            except Exception:
                pass

            recovered.append(record)

        return recovered

    def _mark_interrupted_attempts(
        self,
    ) -> list[int]:
        """
        Close attempts that were IN_PROGRESS when the process died.

        AttemptStore is the source of truth: an unfinished attempt
        becomes BLOCKED, so escalation limits are computed from
        persisted history, not from in-memory counters.
        """

        blocked: list[int] = []

        for run in self.interrupted_runs:
            attempt_id = run.get("attempt_id")

            if attempt_id is None:
                continue

            try:
                attempt = self.attempt_store.get_attempt(
                    int(attempt_id)
                )

                if attempt is None:
                    continue

                if (
                    attempt.status
                    is AttemptStatus.IN_PROGRESS
                ):
                    self.attempt_store.finish_attempt(
                        int(attempt_id),
                        status=AttemptStatus.BLOCKED,
                        failure_reason=(
                            "interrupted by process crash "
                            "(recovered on restart)"
                        ),
                    )

                    blocked.append(int(attempt_id))

            except Exception:
                continue

        return blocked

    def _save_session(self) -> None:
        if self.session is not None:
            self.session = self.session_store.update(self.session)

    def _settle_session_after_run(self, *, succeeded: bool) -> None:
        if self.session is None or self.sandbox_workspace is None:
            return

        changed = bool(self.sandbox_workspace.changed_files())
        self.session.active_run_id = None
        if changed:
            target = (
                SessionStatus.DIRTY_VERIFIED
                if succeeded
                else SessionStatus.DIRTY_FAILED
            )
        else:
            target = SessionStatus.CLEAN
        self.session.transition(target)
        self._save_session()

    def discard_session(self):
        if self.session is None or self.sandbox_workspace is None:
            raise DirtySessionError("no sandbox session")
        if self.session.status is SessionStatus.RUNNING:
            raise DirtySessionError("cannot discard a running session")

        self.sandbox_workspace.discard_and_recreate()
        if self.session.status is SessionStatus.APPLIED:
            self.session.transition(SessionStatus.CLEAN)
            self._save_session()
        if self.session.status is not SessionStatus.DISCARDED:
            self.session.transition(SessionStatus.DISCARDED)
            self._save_session()
        self.session.transition(SessionStatus.CLEAN)
        self.session.active_run_id = None
        self.session.patch_manifest_id = None
        self._save_session()
        self.last_patch_path = None
        self.last_state = None
        return self.session

    def new_session(self, *, discard_dirty: bool = False):
        if self.session is None or self.sandbox_workspace is None:
            raise DirtySessionError("no sandbox session")
        if self.session.status is SessionStatus.RUNNING:
            raise DirtySessionError("cannot replace a running session")
        if self.session.status in {
            SessionStatus.DIRTY_VERIFIED,
            SessionStatus.DIRTY_FAILED,
        } and not discard_dirty:
            raise DirtySessionError(
                "dirty session requires explicit discard before new chat"
            )

        self.sandbox_workspace.discard_and_recreate()
        if self.session.status is SessionStatus.APPLIED:
            self.session.transition(SessionStatus.CLEAN)
            self._save_session()
        if self.session.status is not SessionStatus.DISCARDED:
            self.session.transition(SessionStatus.DISCARDED)
            self._save_session()

        session_id = self.session_store.create(
            sandbox_session_id=self.sandbox_workspace.session_id
        )
        session = self.session_store.get(session_id)
        if session is None:
            raise RuntimeError("new agent session was not created")
        self.session = session
        self.session_id = self.sandbox_workspace.session_id
        self.last_patch_path = None
        self.last_state = None
        self.last_run_id = None
        return session

    def rebase_session_after_apply(self):
        if self.session is None or self.sandbox_workspace is None:
            raise DirtySessionError("no sandbox session")
        if self.session.status is not SessionStatus.DIRTY_VERIFIED:
            raise DirtySessionError(
                "only a verified dirty session can be rebased after apply"
            )

        self.session.transition(SessionStatus.APPLIED)
        self._save_session()
        self.sandbox_workspace.rebase_from_source()
        self.session.transition(SessionStatus.CLEAN)
        self.session.patch_manifest_id = None
        self._save_session()
        self.last_patch_path = None
        return self.session

    def run(self, request: str):
        if (
            self.session is not None
            and self.session.status is not SessionStatus.CLEAN
        ):
            raise DirtySessionError(
                f"session is not clean: {self.session.status.value}"
            )

        run_id = self.runtime_store.start_run(
            request,
            sandbox_session_id=self.session_id,
        )
        self.current_run_id = run_id
        self.last_run_id = run_id
        self.last_state = None

        try:
            if self.session is not None:
                self.session.transition(SessionStatus.RUNNING)
                self.session.active_run_id = run_id
                self._save_session()

            state = AgentLoop(self.controller).run(
                request
            )

        except Exception:
            # Crash/interrupt during the run: never DONE.
            self.runtime_store.finish_run(
                run_id,
                RUN_INTERRUPTED,
            )
            self._settle_session_after_run(succeeded=False)
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
        self._settle_session_after_run(
            succeeded=state.phase is AgentPhase.DONE
        )
        self.current_run_id = None
        self.last_state = state

        return state

    # ==================================
    # CLI / USER-FACING VIEW
    # ==================================

    def run_status(self) -> dict[str, object]:
        """
        Compact status of the current/last run + sandbox.
        """

        run = (
            self.runtime_store.get_run(
                self.last_run_id
            )
            if self.last_run_id is not None
            else None
        )

        state = getattr(self, "last_state", None)

        plan = self.plan_store.get_active_plan()

        return {
            "run_id": self.last_run_id,
            "run_status": (
                run.get("status") if run else None
            ),
            "phase": (
                state.phase.value
                if state is not None
                else (run.get("phase") if run else None)
            ),
            "plan_id": (
                state.plan_id
                if state is not None
                else (run.get("plan_id") if run else None)
            ),
            "task_id": run.get("task_id") if run else None,
            "step_id": run.get("step_id") if run else None,
            "attempt_id": (
                run.get("attempt_id") if run else None
            ),
            "sandbox": self.sandbox_status(),
            "sandbox_session_id": self.session_id,
            "session_status": (
                self.session.status.value
                if self.session is not None
                else None
            ),
            "workspace": str(self.workspace_root),
            "source_project_root": str(
                self.source_project_root
            ),
            "patch": self.last_patch_path,
            "interrupted_runs": len(
                self.interrupted_runs
            ),
            "recovery": list(self.recovery),
            "active_plan": (
                plan.id if plan else None
            ),
            "dependencies": (
                {
                    "status": self.dependency_plan.status,
                    "image": self.dependency_plan.image,
                    "reason": self.dependency_plan.reason,
                }
                if self.dependency_plan is not None
                else None
            ),
        }

    def describe_plan(self) -> str:
        """
        Human-readable Plan / Task / Step view.
        """

        state = getattr(self, "last_state", None)

        plan_id = (
            state.plan_id
            if state is not None
            else None
        )

        if plan_id is None:
            active = self.plan_store.get_active_plan()
            plan_id = active.id if active else None

        if plan_id is None:
            return "PLAN: план ещё не создан."

        plan = self.plan_store.get_plan(plan_id)

        if plan is None:
            return f"PLAN: план #{plan_id} не найден."

        tasks = self.plan_store.get_tasks(plan_id)

        lines: list[str] = [
            f"PLAN #{plan.id} [{plan.status.value}]",
            f"GOAL: {plan.global_goal}",
            "",
        ]

        for task in tasks:
            marker = {
                "DONE": "[x]",
                "IN_PROGRESS": "[>]",
                "FAILED": "[!]",
                "BLOCKED": "[#]",
            }.get(task.status.value, "[ ]")

            lines.append(
                f"{marker} #{task.id} {task.title} "
                f"({task.status.value})"
            )

            dependencies = (
                self.plan_store.get_task_dependencies(
                    task.id
                )
            )

            if dependencies:
                lines.append(
                    "      depends_on: "
                    + ", ".join(
                        str(dependency)
                        for dependency in dependencies
                    )
                )

            for step in self.step_store.get_steps(
                task.id
            ):
                lines.append(
                    f"      - step #{step.id} "
                    f"{step.title} ({step.status.value})"
                )

        warnings = list(
            getattr(
                self.planner,
                "last_step_warnings",
                [],
            )
            or []
        )

        if warnings:
            lines.append("")
            lines.append("WARNINGS (definition of done):")

            lines.extend(
                f"  ! {warning}"
                for warning in warnings
            )

        return "\n".join(lines)

    def global_replan(
        self,
        *,
        reason: str,
        request: str | None = None,
        invalidate_task_keys: tuple[str, ...] = (),
    ) -> dict[str, object]:
        """
        LEVEL 3 — GLOBAL PLAN REPLAN.

        Creates a new plan revision:

          - the previous plan stays in SQLite (SUPERSEDED);
          - DONE tasks are carried over, never re-executed;
          - explicitly invalidated task keys are marked SUPERSEDED;
          - the WHY is stored with the new revision.

        Only the framework / the user may trigger this. The model can
        merely *propose* it through a RepairAgent decision; the
        controller applies the state mutation.
        """

        previous = (
            self.last_state.plan_id
            if getattr(self, "last_state", None) is not None
            else None
        )

        if previous is None:
            active = self.plan_store.get_active_plan()
            previous = active.id if active else None

        if previous is None:
            return {
                "replanned": False,
                "reason": "there is no previous plan",
            }

        if request is None:
            request = (
                self.last_state.request
                if getattr(self, "last_state", None)
                is not None
                else None
            )

        if not request:
            plan = self.plan_store.get_plan(previous)
            request = (
                plan.user_request if plan else None
            )

        if not request:
            return {
                "replanned": False,
                "reason": "no request available for replanning",
            }

        result = self.planner.replan(
            request,
            previous_plan_id=previous,
            reason=reason,
            invalidate_task_keys=invalidate_task_keys,
        )

        revision = self.plan_store.get_plan_revision(
            result.plan_id
        )

        # The framework applies the state mutation: the runtime now
        # works on the new revision, so /status and /plan show it.
        state = getattr(self, "last_state", None)

        if state is not None:
            state.plan_id = result.plan_id

        if self.last_run_id is not None:
            try:
                self.runtime_store.log_event(
                    self.last_run_id,
                    "replan",
                    plan_id=result.plan_id,
                    payload={
                        "previous_plan_id": previous,
                        "reason": reason,
                        "invalidated": list(
                            invalidate_task_keys
                        ),
                    },
                )

            except Exception:
                pass

        return {
            "replanned": True,
            "plan_id": result.plan_id,
            "previous_plan_id": previous,
            "version": (
                revision.get("version")
                if revision
                else None
            ),
            "reason": reason,
            "invalidated": list(
                invalidate_task_keys
            ),
        }

    def memory_overview(self) -> dict[str, object]:
        """
        Active long-term memory, grouped (no giant dump).
        """

        memories = self.memory.get_active_memories()

        counts: dict[str, int] = {}

        for memory in memories:
            memory_type = str(
                memory.get("type", "?")
            )

            counts[memory_type] = (
                counts.get(memory_type, 0) + 1
            )

        return {
            "total": len(memories),
            "counts": counts,
            "memories": memories,
        }

    def patch_preview(self) -> dict[str, object]:
        """
        Preview of the verified patch. Never applies anything.
        """

        state = getattr(self, "last_state", None)

        patch_path = self.last_patch_path

        changed: list[str] = []

        if self.sandbox_workspace is not None:
            changed = [
                relative
                for relative in (
                    self.sandbox_workspace
                    .changed_files()
                )
                if self.sandbox_workspace
                .is_patch_safe(relative)
            ]

        verification = None

        if (
            state is not None
            and state.verification is not None
        ):
            verification = {
                "status": state.verification.status,
                "ok": state.verification.ok,
                "reason": state.verification.reason,
            }

        return {
            "patch": patch_path,
            "patch_exists": bool(
                patch_path
                and Path(patch_path).exists()
            ),
            "changed_files": changed,
            "verification": verification,
            "phase": (
                state.phase.value
                if state is not None
                else None
            ),
            "applied": False,
        }

    def apply_patch(
        self,
        *,
        confirmed: bool = False,
    ) -> dict[str, object]:
        """
        Apply the verified patch to the host project.

        Hard prerequisites (never bypassed):
          - the user explicitly confirmed;
          - the last run finished DONE;
          - verification PASS;
          - the patch exists and every changed path is patch-safe.

        There is no auto-apply anywhere in the agent loop.
        """

        preview = self.patch_preview()

        if not confirmed:
            return {
                "applied": [],
                "reason": "explicit confirmation required",
                "preview": preview,
            }

        if self.sandbox_workspace is None:
            return {
                "applied": [],
                "reason": "no sandbox workspace",
                "preview": preview,
            }

        state = getattr(self, "last_state", None)

        if state is None or state.phase is not AgentPhase.DONE:
            return {
                "applied": [],
                "reason": (
                    "run is not DONE; nothing may be applied"
                ),
                "preview": preview,
            }

        verification = state.verification

        if (
            verification is None
            or not getattr(
                verification,
                "ok",
                False,
            )
        ):
            return {
                "applied": [],
                "reason": (
                    "verification did not PASS; "
                    "nothing may be applied"
                ),
                "preview": preview,
            }

        if not preview["patch_exists"]:
            return {
                "applied": [],
                "reason": "no patch file was produced",
                "preview": preview,
            }

        applied = (
            self.sandbox_workspace.apply_to_project(
                self.source_project_root,
                paths=list(
                    preview["changed_files"]
                ),
            )
        )

        # A successful direct apply establishes the source tree as the
        # new clean baseline.  Keep the durable session transition in
        # the same framework-owned path as manual rebases.
        self.rebase_session_after_apply()

        return {
            "applied": applied,
            "reason": "applied",
            "preview": preview,
        }

    def timeline(
        self,
        run_id: int | None = None,
    ) -> list[dict[str, object]]:
        """
        Stage durations of a run (Шаг 6: где именно тратится время).
        """

        target = (
            run_id if run_id is not None else self.last_run_id
        )

        if target is None:
            return []

        return self.runtime_store.event_timeline(
            int(target)
        )

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
            # Millisecond stamp: stage durations are read back from
            # the durable event log (no separate timing subsystem).
            payload = dict(payload)

            payload.setdefault(
                "ts_ms",
                int(time.time() * 1000),
            )

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


