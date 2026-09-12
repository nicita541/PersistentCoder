from __future__ import annotations

from app.agent.runtime import AgentRuntime
from app.tasks.models import (
    PlanDraft,
    PlanStatus,
    TaskDraft,
    TaskStatus,
)

from helpers import (
    FakeLLM,
    coder_envelope,
    dependencies_response,
    goal_response,
    make_stores,
    tasks_response,
)


def _draft(
    *,
    request: str = "Создай заметки",
    goal: str = "REST API заметок",
) -> PlanDraft:
    return PlanDraft(
        user_request=request,
        global_goal=goal,
        tasks=[
            TaskDraft(
                key="database",
                title="Подключить базу",
                description="Создать подключение к базе.",
                requires=[],
                produces=["database connection"],
                success_criteria=["artifact.txt exists"],
            ),
            TaskDraft(
                key="notes_api",
                title="Создать API",
                description="Endpoints заметок.",
                requires=["database connection"],
                produces=["notes API"],
                success_criteria=["artifact.txt exists"],
                depends_on=["database"],
            ),
        ],
    )


def test_replan_creates_a_new_version_and_preserves_history(
    tmp_path,
):
    stores = make_stores(tmp_path)

    plan_id = stores.plan_store.create_plan(_draft())

    tasks = stores.plan_store.get_tasks(plan_id)

    first = tasks[0]

    # Finish the first task properly.
    stores.plan_store.update_task_status(
        first.id,
        TaskStatus.DONE,
    )
    stores.plan_store.set_task_result(
        first.id,
        result_summary="connection works",
        result_artifacts=["artifact.txt"],
        verification_status="PASS",
        verification_evidence=["artifact.txt exists: PASS"],
    )

    new_plan_id = stores.plan_store.create_replan(
        plan_id,
        _draft(goal="REST API заметок с авторизацией"),
        reason="новая цель: нужна авторизация",
        invalidate_task_keys=("notes_api",),
    )

    assert new_plan_id != plan_id

    # History is intact.
    old = stores.plan_store.get_plan(plan_id)

    assert old is not None
    assert old.status is PlanStatus.SUPERSEDED
    assert len(stores.plan_store.get_tasks(plan_id)) == 2

    revision = stores.plan_store.get_plan_revision(
        new_plan_id
    )

    assert revision is not None
    assert revision["version"] == 2
    assert revision["replaces_plan_id"] == plan_id
    assert (
        revision["replan_reason"]
        == "новая цель: нужна авторизация"
    )
    assert revision["status"] == PlanStatus.ACTIVE.value

    # DONE task carried over, invalidated task explicitly marked.
    by_key = {
        task.key: task
        for task in stores.plan_store.get_tasks(
            new_plan_id
        )
    }

    assert by_key["database"].status is TaskStatus.DONE
    assert (
        by_key["database"].verification_status == "PASS"
    )
    assert (
        by_key["notes_api"].status
        is TaskStatus.SUPERSEDED
    )

    # Dependencies of the new revision exist too.
    assert stores.plan_store.get_task_dependencies(
        by_key["notes_api"].id
    )


class _RoutingLLM:
    """
    Deterministic test double that answers each planner stage with the
    right JSON shape and every coding call with a coder envelope.

    Routing by stage instead of by call order keeps the test robust to
    how many attempts a run needs.
    """

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    def chat(
        self,
        messages: list[dict[str, str]],
        max_new_tokens: int = 512,
    ) -> str:
        self.calls.append(list(messages))

        text = "\n".join(
            str(message.get("content", ""))
            for message in messages
        )

        if "Goal Analyzer" in text:
            return goal_response()

        if "Task Decomposer" in text:
            return tasks_response()

        if "Dependency Builder" in text:
            return dependencies_response()

        return coder_envelope()


def test_runtime_global_replan_creates_revision_two(
    tmp_path,
):
    llm = _RoutingLLM()

    runtime = AgentRuntime(
        workspace_root=tmp_path,
        database_path=tmp_path / "pc.db",
        llm=llm,
        system_prompt="GLOBAL SYSTEM POLICY",
    )

    state = runtime.run("Создай API заметок")

    assert state.phase.value == "DONE"

    first_plan_id = state.plan_id

    result = runtime.global_replan(
        reason=(
            "исходное предположение о хранилище "
            "оказалось неверным"
        ),
        invalidate_task_keys=("notes_api",),
    )

    assert result["replanned"] is True
    assert result["previous_plan_id"] == first_plan_id
    assert result["version"] == 2

    # The old plan is superseded, not deleted.
    old = runtime.plan_store.get_plan(first_plan_id)

    assert old is not None
    assert old.status is PlanStatus.SUPERSEDED

    # DONE tasks were not re-executed: they carry their old
    # verification result.
    new_tasks = runtime.plan_store.get_tasks(
        result["plan_id"]
    )

    assert new_tasks

    done = [
        task
        for task in new_tasks
        if task.status is TaskStatus.DONE
    ]

    assert done
    assert all(
        task.verification_status == "PASS"
        for task in done
    )

    # The WHY is durably stored with the new revision.
    revision = runtime.plan_store.get_plan_revision(
        result["plan_id"]
    )

    assert revision is not None
    assert "неверным" in str(revision["replan_reason"])

    # A replan event was logged for the run.
    events = runtime.runtime_store.get_events(
        runtime.last_run_id
    )

    assert any(
        event["event_type"] == "replan"
        for event in events
    )

    # The CLI plan view shows the new revision.
    described = runtime.describe_plan()

    assert f"PLAN #{result['plan_id']}" in described
