from __future__ import annotations

from app.agent.planner.step_validator import (
    StepValidator,
)
from app.agent.verifier.criterion import (
    classify_criterion,
)
from app.tasks.models import StepDraft, TaskDraft


def _task(
    *,
    key: str = "calc",
    requires=None,
    produces=None,
    criteria=None,
) -> TaskDraft:
    return TaskDraft(
        key=key,
        title="Calculator",
        description="Add calculator",
        requires=list(requires or []),
        produces=list(produces or ["calculator.py"]),
        success_criteria=list(
            criteria or ["calculator.py exists"]
        ),
    )


def _step(
    title: str = "Implement add",
    *,
    requires=None,
    produces=None,
    criteria=None,
    description: str = "Add the add() function",
) -> StepDraft:
    return StepDraft(
        title=title,
        description=description,
        requires=list(requires or []),
        produces=list(produces or []),
        success_criteria=list(
            criteria or ["py_compile calculator.py passes"]
        ),
    )


def test_valid_decomposition_passes():
    task = _task()

    errors = StepValidator().validate(
        task=task,
        steps=[
            _step(
                "Implement add",
                produces=["calculator.py"],
            ),
            _step(
                "Add tests",
                requires=["calculator.py"],
                produces=["test_calculator.py"],
                criteria=["pytest passes"],
            ),
        ],
    )

    assert errors == []


def test_step_count_is_bounded():
    task = _task()

    steps = [
        _step(f"Step {index}")
        for index in range(1, 12)
    ]

    errors = StepValidator(
        max_steps_per_task=4
    ).validate(task=task, steps=steps)

    assert any("max 4" in error for error in errors)


def test_duplicate_steps_are_rejected():
    task = _task()

    errors = StepValidator().validate(
        task=task,
        steps=[
            _step("Implement add"),
            _step("implement   add"),
        ],
    )

    assert any(
        "duplicate step" in error for error in errors
    )


def test_unknown_step_dependency_is_rejected():
    task = _task()

    errors = StepValidator().validate(
        task=task,
        steps=[
            _step(
                "Add tests",
                requires=["authentication service"],
            )
        ],
    )

    assert any(
        "requires unknown resource" in error
        for error in errors
    )


def test_dependency_satisfied_by_earlier_step_is_allowed():
    task = _task()

    errors = StepValidator().validate(
        task=task,
        steps=[
            _step(
                "Implement add",
                produces=["calculator.py"],
            ),
            _step(
                "Add tests",
                requires=["calculator.py"],
                produces=["test_calculator.py"],
            ),
        ],
    )

    assert errors == []


def test_one_file_cannot_have_two_owners():
    task = _task()

    errors = StepValidator().validate(
        task=task,
        steps=[
            _step(
                "Write calculator",
                produces=["calculator.py"],
            ),
            _step(
                "Also write calculator",
                produces=["calculator.py"],
            ),
        ],
    )

    assert any(
        "produced by two steps" in error
        for error in errors
    )


def test_step_outside_task_scope_is_rejected():
    task = _task(produces=["calculator.py"])

    errors = StepValidator().validate(
        task=task,
        steps=[
            _step(
                "Implement add",
                produces=["src/other.py"],
            )
        ],
    )

    assert any(
        "outside the task" in error for error in errors
    )


def test_unverifiable_criteria_are_reported_as_warnings():
    task = _task(criteria=["Make it nice"])

    steps = [
        _step(
            "Implement add",
            criteria=["Make it nice"],
        )
    ]

    validator = StepValidator()

    # Structural validation still passes...
    assert validator.validate(
        task=task,
        steps=steps,
    ) == []

    # ... but the unverifiable definition of done is reported, never
    # silently accepted (VerificationAgent would BLOCK it).
    warnings = validator.warnings(
        task=task,
        steps=steps,
    )

    assert any(
        "no verifiable success criteria" in warning
        for warning in warnings
    )


def test_verifiable_criteria_produce_no_warning():
    validator = StepValidator()

    assert (
        validator.warnings(
            task=_task(),
            steps=[_step()],
        )
        == []
    )


def test_classify_criterion_matches_real_checks():
    assert (
        classify_criterion("calculator.py exists")
        == "file_exists"
    )
    assert (
        classify_criterion(
            "py_compile calculator.py passes"
        )
        == "py_compile"
    )
    assert (
        classify_criterion("pytest passes")
        == "pytest"
    )
    assert (
        classify_criterion("import calculator works")
        == "import"
    )
    assert (
        classify_criterion("Make it nice")
        == "unknown"
    )
