from __future__ import annotations

from app.agent.state import (
    CriterionResult,
    ExecutionResult,
    VerificationResult,
)
from app.agent.verifier.criterion import (
    CriterionEvaluator,
    is_test_file,
)
from app.agent.verifier.evidence import (
    EvidenceCollector,
)
from app.agent.verifier.structured import StructuredVerifier
from app.tasks.models import TaskStatus
from app.tasks.verification_context import VerificationContext


class VerificationAgent:
    """
    Управляющий агент верификации.

    Использует:
      - низкоуровневый Verifier (app.tasks.verifier);
      - VerificationStore;
      - StepStore;
      - success criteria Task;
      - реальные результаты/evidence.

    Никакого фиктивного verified=True.
    """

    def __init__(
        self,
        *,
        quality_gate,
        verifier,
        plan_store,
        step_store,
        verification_store,
        evidence_collector=None,
        workspace=None,
        command_runner=None,
        criterion_evaluator=None,
    ) -> None:
        self.quality_gate = quality_gate
        self.verifier = verifier
        self.plan_store = plan_store
        self.step_store = step_store
        self.verification_store = (
            verification_store
        )
        self.evidence = (
            evidence_collector
            or EvidenceCollector()
        )
        self.criteria = (
            criterion_evaluator
            or CriterionEvaluator(
                workspace=workspace,
                command_runner=command_runner,
            )
        )
        self.structured = StructuredVerifier(
            workspace=workspace,
            command_runner=command_runner,
        )

    def _collect_evidence(
        self,
        execution: ExecutionResult,
        result: VerificationResult,
    ) -> list[str]:
        evidence = self.evidence.collect(
            execution
        )

        if evidence:
            return evidence

        if result.evidence:
            return list(result.evidence)

        return ["execution_failed"]

    @staticmethod
    def _criteria_for(
        task,
        step,
    ) -> list[str]:
        criteria: list[str] = []

        if step is not None:
            criteria = list(
                getattr(
                    step,
                    "success_criteria",
                    [],
                )
                or []
            )

        if not criteria:
            criteria = list(
                getattr(
                    task,
                    "success_criteria",
                    [],
                )
                or []
            )

        return criteria

    @staticmethod
    def _specs_for(task, step):
        specs = list(getattr(step, "verification_specs", []) or [])
        if not specs:
            specs = list(getattr(task, "verification_specs", []) or [])
        return specs

    def _verify(
        self,
        *,
        task,
        step,
        execution: ExecutionResult,
    ) -> VerificationResult:
        specs = self._specs_for(task, step)
        criteria = self._criteria_for(task, step)
        workspace_root = getattr(self.structured.workspace, "root", None)
        environment_check = getattr(
            self.structured.command_runner, "verification_environment", None
        )
        if callable(environment_check):
            environment_ready, environment_reason, environment = (
                environment_check()
            )
        else:
            environment_ready = self.structured.command_runner is not None
            environment_reason = (
                "sandbox command runner is unavailable"
                if not environment_ready
                else "test runner environment"
            )
            environment = {
                "sandbox": "unavailable" if not environment_ready else "unspecified"
            }
        environment = {
            **environment,
            "python": self.structured.python,
        }
        try:
            context = VerificationContext.capture(
                workspace_root,
                specs=specs,
                criteria=criteria,
                environment=environment,
            )
        except Exception as error:
            return VerificationResult(
                ok=False,
                status="BLOCKED",
                reason=f"verification context unavailable: {error}",
                evidence=[f"verification_context_blocked: {error}"],
            )

        if not environment_ready:
            return VerificationResult(
                ok=False,
                status="BLOCKED",
                reason=f"verification environment unavailable: {environment_reason}",
                evidence=[f"verification_environment_blocked: {environment_reason}"],
                context=context,
            )

        # Each verification must observe the CURRENT workspace: drop
        # any cached command results from a previous attempt.
        cache = getattr(
            self.criteria,
            "_command_cache",
            None,
        )

        if isinstance(cache, dict):
            cache.clear()

        base = self.quality_gate.check(
            task=task,
            execution=execution,
        )

        evidence = self._collect_evidence(
            execution,
            base,
        )

        if not base.ok:
            return VerificationResult(
                ok=False,
                status=base.status,
                reason=base.reason,
                evidence=evidence,
                context=context,
            )

        # Authoritative pytest scope: the test files this attempt
        # actually changed (never the whole repository by accident).
        self.criteria.test_targets = [
            artifact
            for artifact in (
                execution.artifacts or []
            )
            if is_test_file(artifact)
        ]

        if not specs and not criteria:
            return VerificationResult(
                ok=False,
                status="FAIL",
                reason=(
                    "no success criteria declared"
                ),
                evidence=evidence,
                context=context,
            )

        if specs:
            results: list[CriterionResult] = self.structured.verify_all(specs)
        else:
            # Direct/legacy callers keep fail-closed compatibility; all new
            # persisted runtime plans carry structured specs.
            results = [
                self.criteria.evaluate(criterion)
                for criterion in criteria
            ]

        failures = [
            item
            for item in results
            if item.status == "FAIL"
        ]

        blocked = [
            item
            for item in results
            if item.status == "BLOCKED"
        ]

        for item in results:
            evidence.extend(item.evidence)

        if failures:
            ok = False
            status = "FAIL"
            reason = (
                "criterion failed: "
                + "; ".join(
                    f"{item.criterion} -> "
                    f"{item.reason or item.status}"
                    for item in failures
                )
            )

        elif blocked:
            ok = False
            status = "BLOCKED"
            reason = (
                "criterion not provable: "
                + "; ".join(
                    f"{item.criterion} -> "
                    f"{item.reason or item.status}"
                    for item in blocked
                )
            )

        else:
            ok = True
            status = "PASS"
            reason = "all success criteria verified"

        seen: set[str] = set()
        deduped: list[str] = []

        for item in evidence:
            if item and item not in seen:
                seen.add(item)
                deduped.append(item)

        current_environment = environment
        current_environment_ready = environment_ready
        if callable(environment_check):
            (
                current_environment_ready,
                _current_environment_reason,
                current_environment,
            ) = environment_check()
            current_environment = {
                **current_environment,
                "python": self.structured.python,
            }

        if ok and (
            not current_environment_ready
            or not context.matches(
                workspace_root,
                specs=specs,
                criteria=criteria,
                environment=current_environment,
            )
        ):
            return VerificationResult(
                ok=False,
                status="BLOCKED",
                reason="workspace, verification spec, or environment changed during verification",
                evidence=[*deduped, "verification_context_changed"],
                criterion_results=results,
                context=context,
            )

        return VerificationResult(
            ok=ok,
            status=status,
            reason=reason,
            evidence=deduped,
            criterion_results=results,
            context=context,
        )

    def verify_step(
        self,
        *,
        task,
        step,
        execution: ExecutionResult,
    ) -> VerificationResult:
        result = self._verify(
            task=task,
            step=step,
            execution=execution,
        )

        self.verifier.begin_step_verification(
            step.id
        )

        if result.ok:
            self.verifier.pass_step(
                step.id,
                evidence=(
                    result.evidence
                    or ["verified"]
                ),
                context=result.context,
            )

        elif result.status == "BLOCKED":
            self.verifier.block_step(
                step.id,
                reason=result.reason or "verification blocked",
                evidence=result.evidence or ["verification_blocked"],
                context=result.context,
            )
        else:
            self.verifier.fail_step(
                step.id,
                reason=(
                    result.reason
                    or "verification failed"
                ),
                evidence=(
                    result.evidence
                    or ["verification_failed"]
                ),
                context=result.context,
            )

        return result

    def verify_task(
        self,
        *,
        task,
        execution: ExecutionResult,
    ) -> VerificationResult:
        result = self._verify(
            task=task,
            step=None,
            execution=execution,
        )

        self._begin_task_verification(task)

        if result.ok:
            self.verifier.pass_task(
                task.id,
                evidence=(
                    result.evidence
                    or ["verified"]
                ),
                context=result.context,
            )

        elif result.status == "BLOCKED":
            self.verifier.block_task(
                task.id,
                reason=result.reason or "verification blocked",
                evidence=result.evidence or ["verification_blocked"],
                context=result.context,
            )
        else:
            self.verifier.fail_task(
                task.id,
                reason=(
                    result.reason
                    or "verification failed"
                ),
                evidence=(
                    result.evidence
                    or ["verification_failed"]
                ),
                context=result.context,
            )

        return result

    def _begin_task_verification(
        self,
        task,
    ) -> None:
        steps = self.step_store.get_steps(
            task.id
        )

        if steps:
            self.verifier.begin_task_verification(
                task.id
            )

        else:
            # Task без steps: переводим состояние
            # вручную, парную запись делает
            # низкоуровневый verifier ниже.
            self.plan_store.update_task_status(
                task.id,
                TaskStatus.VERIFYING,
            )

