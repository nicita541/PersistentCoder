from __future__ import annotations

from app.agent.repair.strategies import GIVE_UP, REPLAN_TASK
from app.agent.state import (
    AgentPhase,
    AgentState,
    ExecutionResult,
    RepairState,
)
from app.tasks.models import (
    AttemptStatus,
    StepDraft,
    StepStatus,
    TaskStatus,
)
from app.tasks.change_scope import AllowedChangeSet


class AgentControllerError(RuntimeError):
    pass


class AgentController:
    """
    Главный управляющий компонент Agent Layer.

    Поток:

        observe() -> plan() -> execute() -> verify()
        -> repair() -> remember()

    Контроллер НЕ содержит собственных LLM/Memory/Context/
    Task OS/Tools. Всё приходит через dependency injection.
    """

    def __init__(
        self,
        *,
        planner,
        coder,
        verifier,
        repair,
        scheduler,
        plan_store,
        step_store=None,
        memory=None,
        events=None,
        sandbox_workspace=None,
        attempt_store=None,
        attempt_coordinator=None,
        budget_consumer=None,
        max_step_attempts: int = 2,
        max_task_attempts: int = 3,
        dependency_plan=None,
    ) -> None:
        self.planner = planner
        self.coder = coder
        self.verifier = verifier
        self.repair_agent = repair
        self.scheduler = scheduler
        self.plan_store = plan_store
        self.step_store = step_store
        self.memory = memory
        self.events = events
        self.sandbox_workspace = sandbox_workspace
        self.attempt_store = attempt_store
        self.attempt_coordinator = attempt_coordinator
        self.budget_consumer = budget_consumer
        self.dependency_plan = dependency_plan
        self._attempt_seq = 0
        self.max_step_attempts = (
            max_step_attempts
        )
        self.max_task_attempts = (
            max_task_attempts
        )

    # ======================================
    # EVENTS
    # ======================================

    def _emit(
        self,
        name: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        if self.events is not None:
            self.events.emit(
                name,
                payload or {},
            )

    # ======================================
    # OBSERVE
    # ======================================

    def observe(
        self,
        request: str,
    ) -> AgentState:
        request = (request or "").strip()

        if not request:
            raise ValueError("request required")

        state = AgentState(
            request=request,
            global_goal=request,
            phase=AgentPhase.PLANNING,
        )

        self._emit(
            "observe",
            {"request": request},
        )

        return state

    # ======================================
    # PLAN
    # ======================================

    def plan(
        self,
        state: AgentState,
    ) -> AgentState:
        result = self.planner.plan(
            state.request
        )

        state.plan_id = result.plan_id
        state.global_goal = (
            result.draft.global_goal
        )
        state.phase = AgentPhase.READY

        self._emit(
            "plan",
            {
                "plan_id": result.plan_id,
                "tasks": len(result.draft.tasks),
            },
        )

        return state

    # ======================================
    # READY -> next Task
    # ======================================

    def advance(
        self,
        state: AgentState,
    ) -> AgentState:
        plan_id = self._require_plan(state)

        task = self.scheduler.start_next(
            plan_id
        )

        if task is None:
            self._settle(state)
            return state

        state.active_task_id = task.id

        state.active_step_id = (
            self._activate_first_step(task.id)
        )

        state.phase = AgentPhase.EXECUTING

        self._emit(
            "task_selected",
            {
                "task_id": task.id,
                "step_id": state.active_step_id,
            },
        )

        return state

    def _settle(
        self,
        state: AgentState,
    ) -> None:
        if state.plan_id is None:
            state.phase = AgentPhase.FAILED
            state.completion = "FAILED"
            return

        tasks = self.plan_store.get_tasks(
            state.plan_id
        )

        if tasks and all(
            task.status is TaskStatus.DONE
            for task in tasks
        ):
            state.phase = AgentPhase.DONE
            state.completion = "DONE"
            return

        state.phase = AgentPhase.FAILED
        state.completion = "FAILED"

        if state.repair.reason is None:
            state.repair.reason = (
                "no executable task remains"
            )

    # ======================================
    # EXECUTE
    # ======================================

    @staticmethod
    def _repair_feedback(repair: RepairState) -> str:
        context = repair.context or {}
        lines = [
            "STRUCTURED REPAIR CONTEXT",
            f"scope: {context.get('scope', repair.scope or '')}",
            f"failure_class: {context.get('failure_class', '')}",
            f"root_cause: {context.get('root_cause', repair.reason or '')}",
            f"previous_approach: {context.get('previous_approach', repair.approach or '')}",
            "required_different_approach: "
            + str(context.get("required_different_approach", repair.strategy or "")),
        ]
        files = context.get("files_to_inspect", [])
        checks = context.get("verification_plan", [])
        lines.append("files_to_inspect: " + ", ".join(str(item) for item in files))
        lines.append("verification_plan: " + "; ".join(str(item) for item in checks))
        return "\n".join(lines)[:4000]

    def execute(
        self,
        state: AgentState,
    ) -> AgentState:
        task = self._require_task(state)
        step = self._active_step(state)

        if (
            task.external_dependencies
            and self.dependency_plan is not None
            and self.dependency_plan.status == "BLOCKED"
        ):
            reason = "BLOCKED_DEPENDENCY: " + str(self.dependency_plan.reason)
            self.plan_store.update_task_status(task.id, TaskStatus.BLOCKED)
            if step is not None:
                self.step_store.update_step_status(step.id, StepStatus.BLOCKED)
            state.execution = ExecutionResult(
                ok=False,
                summary="dependency environment unavailable",
                failure_reason=reason,
                evidence=["blocked_dependency"],
            )
            state.repair = RepairState(
                required=False,
                action="BLOCKED_DEPENDENCY",
                scope="TASK",
                reason=reason,
            )
            state.phase = AgentPhase.FAILED
            state.completion = "FAILED"
            self._emit(
                "dependency_blocked",
                {"task_id": task.id, "reason": reason[:400]},
            )
            return state

        self._begin_attempt(state, task, step)

        feedback = (
            self._repair_feedback(state.repair)
            if state.repair.required
            else None
        )

        try:
            result = self.coder.execute(
                task,
                step=step,
                plan_id=state.plan_id,
                feedback=feedback,
                allowed_changes=AllowedChangeSet(
                    task.change_paths,
                    step.change_paths if step is not None else None,
                ),
            )

        except Exception as error:
            result = ExecutionResult(
                ok=False,
                summary="executor error",
                failure_reason=str(error),
                evidence=[
                    f"executor_error:{type(error).__name__}"
                ],
            )

        state.execution = result
        state.phase = AgentPhase.VERIFYING

        self._emit(
            "execute",
            {
                "task_id": task.id,
                "step_id": state.active_step_id,
                "ok": result.ok,
            },
        )

        return state

    # ======================================
    # VERIFY
    # ======================================

    def verify(
        self,
        state: AgentState,
    ) -> AgentState:
        task = self._require_task(state)
        step = self._active_step(state)

        if step is not None:
            result = self.verifier.verify_step(
                task=task,
                step=step,
                execution=state.execution,
            )

        else:
            result = self.verifier.verify_task(
                task=task,
                execution=state.execution,
            )

        state.verification = result

        self._finalize_attempt(
            state,
            ok=result.ok,
            status=result.status,
            reason=result.reason,
        )

        if not result.ok:
            state.phase = AgentPhase.REPAIRING

            self._emit(
                "verify",
                {
                    "task_id": task.id,
                    "ok": False,
                    "reason": result.reason,
                },
            )

            return state

        state.repair = RepairState()

        if step is not None:
            next_step = self._next_pending_step(
                task.id
            )

            if next_step is not None:
                if (
                    next_step.status
                    is not StepStatus.IN_PROGRESS
                ):
                    self.step_store.update_step_status(
                        next_step.id,
                        StepStatus.IN_PROGRESS,
                    )

                self.step_store.set_current_step(
                    task_id=task.id,
                    step_id=next_step.id,
                )

                state.active_step_id = next_step.id
                state.phase = AgentPhase.EXECUTING

                self._emit(
                    "step_done",
                    {
                        "task_id": task.id,
                        "step_id": step.id,
                        "next_step_id": (
                            next_step.id
                        ),
                    },
                )

                return state

            task_result = self.verifier.verify_task(
                task=task,
                execution=state.execution,
            )

            state.verification = task_result

            if not task_result.ok:
                state.phase = AgentPhase.REPAIRING
                return state

        state.active_step_id = None
        state.phase = AgentPhase.READY

        self._emit(
            "verify",
            {"task_id": task.id, "ok": True},
        )

        return state

    # ======================================
    # REPAIR
    # ======================================

    def repair(
        self,
        state: AgentState,
    ) -> AgentState:
        task = self._require_task(state)
        step = self._active_step(state)

        task_key = f"task:{task.id}"
        step_key = (
            f"step:{step.id}"
            if step is not None
            else None
        )

        if self.budget_consumer is not None:
            task_cache = self.budget_consumer(
                "TASK", task.id, self.max_task_attempts
            )
            state.attempts[task_key] = task_cache
        else:
            state.attempts[task_key] = (
                state.attempts.get(task_key, 0) + 1
            )
            task_cache = state.attempts[task_key]

        step_cache = 0

        if step_key is not None:
            if self.budget_consumer is not None:
                step_cache = self.budget_consumer(
                    "STEP", step.id, self.max_step_attempts
                )
                state.attempts[step_key] = step_cache
            else:
                state.attempts[step_key] = (
                    state.attempts.get(step_key, 0) + 1
                )
                step_cache = state.attempts[step_key]

        # AttemptStore is the authoritative source of attempt history.
        task_attempts = task_cache
        step_attempts = step_cache

        if self.attempt_store is not None:
            stored_task = sum(
                attempt.status in {AttemptStatus.FAILED, AttemptStatus.BLOCKED}
                for attempt in self.attempt_store.get_task_attempts(task.id)
            )

            if self.step_store is not None:
                for sibling in (
                    self.step_store.get_steps(task.id)
                ):
                    stored_task += sum(
                        attempt.status
                        in {AttemptStatus.FAILED, AttemptStatus.BLOCKED}
                        for attempt in self.attempt_store.get_step_attempts(
                            sibling.id
                        )
                    )

            stored_step = (
                sum(
                    attempt.status in {AttemptStatus.FAILED, AttemptStatus.BLOCKED}
                    for attempt in self.attempt_store.get_step_attempts(step.id)
                )
                if step is not None
                else 0
            )

            task_attempts = max(
                task_attempts,
                stored_task,
            )
            step_attempts = max(
                step_attempts,
                stored_step,
            )

        outcome = self.repair_agent.repair(
            task=task,
            verification=state.verification,
            step=step,
            step_attempt_number=max(step_attempts, 1),
            task_attempt_number=max(task_attempts, 1),
        )

        state.repair = RepairState(
            required=True,
            action=outcome.action,
            scope=outcome.scope,
            strategy=outcome.strategy,
            reason=outcome.reason,
            approach=(
                outcome.approach.fingerprint
                if outcome.approach is not None
                else outcome.strategy
            ),
            context=(
                {
                    "scope": outcome.approach.scope,
                    "failure_class": outcome.approach.failure_class,
                    "root_cause": outcome.approach.root_cause,
                    "previous_approach": outcome.approach.fingerprint,
                    "required_different_approach": outcome.approach.new_approach,
                    "files_to_inspect": outcome.approach.files_to_inspect,
                    "verification_plan": outcome.approach.verification_plan,
                }
                if outcome.approach is not None
                else {}
            ),
        )

        self._emit(
            "repair",
            {
                "task_id": task.id,
                "action": outcome.action,
                "scope": outcome.scope,
                "reason": outcome.reason,
            },
        )

        if outcome.action == GIVE_UP:
            self.plan_store.update_task_status(
                task.id,
                TaskStatus.FAILED,
            )

            self._settle(state)
            return state

        if outcome.action == REPLAN_TASK and self.step_store is not None:
            accepted = {
                path.casefold()
                for existing in self.step_store.get_steps(task.id)
                if existing.status is StepStatus.DONE
                for path in existing.change_paths
            }
            replacements = []
            for path in task.change_paths:
                if path.casefold() in accepted:
                    continue
                specs = [
                    spec
                    for spec in task.verification_specs
                    if spec.target == path
                ]
                replacements.append(
                    StepDraft(
                        title=f"Repair {path}",
                        description=(
                            f"Use a materially different approach for {path}: "
                            f"{state.repair.context.get('required_different_approach', '')}"
                        ),
                        requires=list(task.requires),
                        produces=[path],
                        success_criteria=list(task.success_criteria),
                        change_paths=[path],
                        verification_specs=specs,
                    )
                )
            if replacements:
                replacement_ids = (
                    self.step_store.supersede_unfinished_and_create(
                        task.id, replacements
                    )
                )
                state.active_step_id = replacement_ids[0]
                self.step_store.update_step_status(
                    state.active_step_id, StepStatus.IN_PROGRESS
                )
                self.step_store.set_current_step(
                    task_id=task.id,
                    step_id=state.active_step_id,
                )
                state.phase = AgentPhase.EXECUTING
                self._emit(
                    "task_replanned",
                    {
                        "task_id": task.id,
                        "replacement_steps": replacement_ids,
                        "reason": outcome.reason[:400],
                    },
                )
                return state

        if step is not None:
            current = self.step_store.get_step(
                step.id
            )

            if (
                current is not None
                and current.status
                is not StepStatus.IN_PROGRESS
            ):
                self.step_store.update_step_status(
                    step.id,
                    StepStatus.IN_PROGRESS,
                )

            state.active_step_id = step.id

        state.phase = AgentPhase.EXECUTING

        return state

    # ======================================
    # REMEMBER
    # ======================================

    def remember(
        self,
        state: AgentState,
    ) -> AgentState:
        if self.memory is not None:
            self.memory.record_experience(
                request=state.request,
                plan_id=state.plan_id,
                outcome=(
                    state.completion
                    or state.phase.value
                ),
            )

        self._emit(
            "remember",
            {
                "plan_id": state.plan_id,
                "completion": state.completion,
            },
        )

        return state

    # ======================================
    # TRANSACTIONAL ATTEMPTS
    # ======================================

    def _begin_attempt(
        self,
        state: AgentState,
        task,
        step,
    ) -> None:
        """
        Checkpoint the sandbox and open a durable attempt record.

        The AttemptStore is the source of truth for attempt history.
        """

        if self.attempt_coordinator is not None:
            self.attempt_coordinator.begin(state, task, step)
            self._emit(
                "attempt_start",
                {
                    "attempt_id": state.attempt_id,
                    "checkpoint": state.checkpoint_id,
                },
            )
            return

        self._attempt_seq += 1

        label = f"attempt-{self._attempt_seq}"

        state.attempt_id = None
        state.checkpoint_id = None

        if self.sandbox_workspace is not None:
            try:
                self.sandbox_workspace.checkpoint(label)
                state.checkpoint_id = label

            except Exception as error:
                self._emit(
                    "checkpoint_failed",
                    {"error": str(error)},
                )
                raise AgentControllerError(
                    f"checkpoint creation failed: {error}"
                ) from error

        if self.attempt_store is not None:
            approach = (
                state.repair.approach
                or state.repair.strategy
                or None
            )

            try:
                if step is not None:
                    record = (
                        self.attempt_store
                        .start_step_attempt(
                            step.id,
                            approach=approach,
                        )
                    )

                else:
                    record = (
                        self.attempt_store
                        .start_task_attempt(
                            task.id,
                            approach=approach,
                        )
                    )

                state.attempt_id = record.id

            except Exception as error:
                self._emit(
                    "attempt_start_failed",
                    {"error": str(error)},
                )
                if self.sandbox_workspace is not None and state.checkpoint_id:
                    restored = self.sandbox_workspace.rollback(
                        state.checkpoint_id
                    )
                    if not restored:
                        raise AgentControllerError(
                            "attempt persistence failed and checkpoint vanished"
                        ) from error
                    self.sandbox_workspace.commit(state.checkpoint_id)
                    state.checkpoint_id = None
                raise AgentControllerError(
                    f"attempt persistence failed: {error}"
                ) from error

        self._emit(
            "attempt_start",
            {
                "attempt_id": state.attempt_id,
                "checkpoint": state.checkpoint_id,
            },
        )

    def _finalize_attempt(
        self,
        state: AgentState,
        *,
        ok: bool,
        status: str,
        reason: str = "",
    ) -> None:
        """
        Commit (PASS) or roll back (FAIL/BLOCKED) the attempt.

        A failed attempt must never leave half-written files.
        """

        if self.attempt_coordinator is not None:
            attempt_id = state.attempt_id
            attempt_status = (
                AttemptStatus.PASS
                if ok
                else (
                    AttemptStatus.BLOCKED
                    if status == "BLOCKED"
                    else AttemptStatus.FAILED
                )
            )
            self.attempt_coordinator.finish(
                state, ok=ok, status=status, reason=reason
            )
            self._emit(
                "attempt_finish",
                {
                    "attempt_id": attempt_id,
                    "status": attempt_status.value,
                    "reason": reason,
                },
            )
            return

        label = state.checkpoint_id
        attempt_id = state.attempt_id

        if ok:
            if (
                self.sandbox_workspace is not None
                and label
            ):
                try:
                    self.sandbox_workspace.commit(label)

                except Exception as error:
                    raise AgentControllerError(
                        f"checkpoint commit failed: {error}"
                    ) from error

            attempt_status = AttemptStatus.PASS

        else:
            if (
                self.sandbox_workspace is not None
                and label
            ):
                try:
                    restored = self.sandbox_workspace.rollback(label)
                    if not restored:
                        raise AgentControllerError(
                            f"checkpoint is missing: {label}"
                        )
                    self.sandbox_workspace.commit(
                        label
                    )

                except Exception as error:
                    raise AgentControllerError(
                        f"checkpoint rollback failed: {error}"
                    ) from error

            attempt_status = (
                AttemptStatus.BLOCKED
                if status == "BLOCKED"
                else AttemptStatus.FAILED
            )

        if (
            self.attempt_store is not None
            and attempt_id is not None
        ):
            self.attempt_store.finish_attempt(
                attempt_id,
                status=attempt_status,
                failure_reason=(
                    None if ok else (reason or "attempt failed")
                ),
            )

        self._emit(
            "attempt_finish",
            {
                "attempt_id": attempt_id,
                "status": attempt_status.value,
                "reason": reason,
            },
        )

        state.attempt_id = None
        state.checkpoint_id = None

    # ======================================
    # HELPERS
    # ======================================

    def _require_plan(
        self,
        state: AgentState,
    ) -> int:
        if state.plan_id is None:
            raise AgentControllerError(
                "plan is required"
            )

        return state.plan_id

    def _require_task(
        self,
        state: AgentState,
    ):
        if state.active_task_id is None:
            raise AgentControllerError(
                "active task is required"
            )

        task = self.plan_store.get_task(
            state.active_task_id
        )

        if task is None:
            raise AgentControllerError(
                "unknown task: "
                f"{state.active_task_id}"
            )

        return task

    def _active_step(
        self,
        state: AgentState,
    ):
        if (
            self.step_store is None
            or state.active_step_id is None
        ):
            return None

        return self.step_store.get_step(
            state.active_step_id
        )

    def _next_pending_step(
        self,
        task_id: int,
    ):
        if self.step_store is None:
            return None

        executable = {
            StepStatus.PENDING,
            StepStatus.READY,
            StepStatus.IN_PROGRESS,
        }

        for step in self.step_store.get_steps(
            task_id
        ):
            if step.status in executable:
                return step

        return None

    def _activate_first_step(
        self,
        task_id: int,
    ) -> int | None:
        if self.step_store is None:
            return None

        executable = {
            StepStatus.PENDING,
            StepStatus.READY,
            StepStatus.IN_PROGRESS,
        }

        for step in self.step_store.get_steps(
            task_id
        ):
            if step.status not in executable:
                continue

            if (
                step.status
                is not StepStatus.IN_PROGRESS
            ):
                self.step_store.update_step_status(
                    step.id,
                    StepStatus.IN_PROGRESS,
                )

            self.step_store.set_current_step(
                task_id=task_id,
                step_id=step.id,
            )

            return step.id

        return None



