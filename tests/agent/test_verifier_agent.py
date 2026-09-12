from __future__ import annotations

from app.agent.state import (
    CommandExecution,
    ExecutionResult,
)
from app.agent.verifier.agent import (
    VerificationAgent,
)
from app.agent.verifier.evidence import (
    EvidenceCollector,
)
from app.agent.verifier.quality_gate import QualityGate
from app.tasks.models import StepStatus, TaskStatus
from app.tasks.verifier import Verifier

from helpers import make_stores, seed_plan


def _agent(stores):
    verifier = Verifier(
        plan_store=stores.plan_store,
        step_store=stores.step_store,
        verification_store=(
            stores.verification_store
        ),
    )

    return VerificationAgent(
        quality_gate=QualityGate(),
        verifier=verifier,
        plan_store=stores.plan_store,
        step_store=stores.step_store,
        verification_store=(
            stores.verification_store
        ),
        evidence_collector=EvidenceCollector(),
    )


def _ok_execution() -> ExecutionResult:
    return ExecutionResult(
        ok=True,
        summary="applied change",
        artifacts=["a.txt"],
        evidence=["wrote a.txt"],
        commands=[
            CommandExecution(
                command="pytest",
                returncode=0,
                stdout="1 passed",
            )
        ],
    )


def _prepare(tmp_path):
    stores = make_stores(tmp_path)

    _, task, step = seed_plan(stores)

    stores.plan_store.update_task_status(
        task.id,
        TaskStatus.IN_PROGRESS,
    )

    stores.step_store.update_step_status(
        step.id,
        StepStatus.IN_PROGRESS,
    )

    return stores, task.id, step.id


def test_step_verification_passes_with_real_evidence(
    tmp_path,
):
    stores, task_id, step_id = _prepare(
        tmp_path
    )

    agent = _agent(stores)

    result = agent.verify_step(
        task=stores.plan_store.get_task(task_id),
        step=stores.step_store.get_step(step_id),
        execution=_ok_execution(),
    )

    assert result.ok is True
    assert result.status == "PASS"

    assert (
        stores.step_store.get_step(step_id).status
        is StepStatus.DONE
    )

    records = (
        stores.verification_store
        .get_step_verifications(step_id)
    )

    assert records
    assert records[-1].evidence


def test_rejects_fake_success_without_evidence(
    tmp_path,
):
    stores, task_id, step_id = _prepare(
        tmp_path
    )

    agent = _agent(stores)

    fake = ExecutionResult(
        ok=True,
        summary="done",
        artifacts=["a.txt"],
        evidence=[],
        commands=[],
    )

    result = agent.verify_step(
        task=stores.plan_store.get_task(task_id),
        step=stores.step_store.get_step(step_id),
        execution=fake,
    )

    assert result.ok is False

    assert (
        stores.step_store.get_step(step_id).status
        is StepStatus.IN_PROGRESS
    )


def test_rejects_failed_command(tmp_path):
    stores, task_id, step_id = _prepare(
        tmp_path
    )

    agent = _agent(stores)

    execution = ExecutionResult(
        ok=True,
        summary="ran command",
        artifacts=["a.txt"],
        evidence=["ran pytest"],
        commands=[
            CommandExecution(
                command="pytest",
                returncode=1,
                stderr="1 failed",
            )
        ],
    )

    result = agent.verify_step(
        task=stores.plan_store.get_task(task_id),
        step=stores.step_store.get_step(step_id),
        execution=execution,
    )

    assert result.ok is False
    assert "command failed" in result.reason


def test_task_verification_after_steps_done(
    tmp_path,
):
    stores, task_id, step_id = _prepare(
        tmp_path
    )

    agent = _agent(stores)

    step_result = agent.verify_step(
        task=stores.plan_store.get_task(task_id),
        step=stores.step_store.get_step(step_id),
        execution=_ok_execution(),
    )

    assert step_result.ok is True

    task_result = agent.verify_task(
        task=stores.plan_store.get_task(task_id),
        execution=_ok_execution(),
    )

    assert task_result.ok is True

    assert (
        stores.plan_store.get_task(task_id).status
        is TaskStatus.DONE
    )
