from __future__ import annotations

from pathlib import Path

from app.agent.coder.workspace import Workspace
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
from app.sandbox.runner import SandboxCommandRunner

from helpers import (
    RecordingCommandRunner,
    make_stores,
    seed_plan,
)


def _agent(stores, command_runner=None):
    verifier = Verifier(
        plan_store=stores.plan_store,
        step_store=stores.step_store,
        verification_store=(
            stores.verification_store
        ),
    )

    # Real sandbox workspace: criteria are checked against real
    # files, not against the criterion string itself.
    root = Path(stores.database_path).parent

    (root / "a.txt").write_text(
        "artifact",
        encoding="utf-8",
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
        workspace=Workspace(root),
        command_runner=(
            command_runner
            or RecordingCommandRunner(stdout="1 passed")
        ),
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
    assert result.context is not None

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
    assert records[-1].context == result.context


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
        is StepStatus.BLOCKED
    )

    persisted = stores.verification_store.get_step_verifications(step_id)[-1]
    assert persisted.status.value == "BLOCKED"


def test_static_check_is_blocked_when_sandbox_environment_is_unavailable(
    tmp_path,
):
    stores, task_id, step_id = _prepare(tmp_path)
    runner = SandboxCommandRunner(
        sandbox_root=tmp_path,
        daemon_probe=lambda: False,
    )
    agent = _agent(stores, command_runner=runner)

    result = agent.verify_step(
        task=stores.plan_store.get_task(task_id),
        step=stores.step_store.get_step(step_id),
        execution=_ok_execution(),
    )

    assert result.status == "BLOCKED"
    assert "environment unavailable" in result.reason
    assert stores.step_store.get_step(step_id).status is StepStatus.BLOCKED


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
