from __future__ import annotations

from app.tasks.models import PlanDraft, StepDraft, StepStatus, TaskDraft, TaskStatus
from app.tasks.verification_spec import VerificationKind, VerificationSpec
from app.agent.controller import AgentController

from helpers import make_stores


def test_replan_supersedes_unfinished_steps_and_keeps_accepted_work(tmp_path):
    stores = make_stores(tmp_path)
    specs = [
        VerificationSpec(VerificationKind.FILE_EXISTS, "accepted.py"),
        VerificationSpec(VerificationKind.FILE_EXISTS, "retry.py"),
    ]
    plan_id = stores.plan_store.create_plan(
        PlanDraft(
            user_request="repair",
            global_goal="repair",
            tasks=[
                TaskDraft(
                    key="feature",
                    title="Feature",
                    description="Feature",
                    success_criteria=["display"],
                    change_paths=["accepted.py", "retry.py"],
                    verification_specs=specs,
                )
            ],
        )
    )
    task = stores.plan_store.get_tasks(plan_id)[0]
    original_ids = stores.step_store.create_steps(
        task.id,
        [
            StepDraft(
                title="Accepted",
                description="Accepted",
                success_criteria=["display"],
                change_paths=["accepted.py"],
                verification_specs=[specs[0]],
            ),
            StepDraft(
                title="Old approach",
                description="Old approach",
                success_criteria=["display"],
                change_paths=["retry.py"],
                verification_specs=[specs[1]],
            ),
        ],
    )
    stores.step_store.update_step_status(original_ids[0], StepStatus.DONE)

    replacement_ids = stores.step_store.supersede_unfinished_and_create(
        task.id,
        [
            StepDraft(
                title="Different approach",
                description="Different approach because tests failed",
                success_criteria=["display"],
                change_paths=["retry.py"],
                verification_specs=[specs[1]],
            )
        ],
    )

    assert stores.step_store.get_step(original_ids[0]).status is StepStatus.DONE
    assert (
        stores.step_store.get_step(original_ids[1]).status
        is StepStatus.SUPERSEDED
    )
    assert stores.step_store.get_step(replacement_ids[0]).status is StepStatus.PENDING


def test_pass_attempts_do_not_count_as_failure_budget(tmp_path):
    stores = make_stores(tmp_path)
    from helpers import seed_plan
    from app.tasks.models import AttemptStatus

    _plan_id, task, step = seed_plan(stores)
    passed = stores.attempt_store.start_step_attempt(step.id, approach="old")
    stores.attempt_store.finish_attempt(passed.id, status=AttemptStatus.PASS)

    failed = [
        attempt
        for attempt in stores.attempt_store.get_step_attempts(step.id)
        if attempt.status in {AttemptStatus.FAILED, AttemptStatus.BLOCKED}
    ]

    assert failed == []


def test_superseded_steps_are_never_selected_for_execution(tmp_path):
    stores = make_stores(tmp_path)
    from helpers import seed_plan

    _plan_id, task, old_step = seed_plan(stores)
    replacement_id = stores.step_store.supersede_unfinished_and_create(
        task.id,
        [
            StepDraft(
                title="Replacement",
                description="Replacement",
                success_criteria=["display"],
            )
        ],
    )[0]
    controller = AgentController(
        planner=None,
        coder=None,
        verifier=None,
        repair=None,
        scheduler=None,
        plan_store=stores.plan_store,
        step_store=stores.step_store,
    )

    selected = controller._activate_first_step(task.id)

    assert stores.step_store.get_step(old_step.id).status is StepStatus.SUPERSEDED
    assert selected == replacement_id
    stores.step_store.update_step_status(replacement_id, StepStatus.DONE)
    assert controller._next_pending_step(task.id) is None
