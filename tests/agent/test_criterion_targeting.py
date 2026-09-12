from __future__ import annotations

from app.agent.verifier.criterion import (
    CriterionEvaluator,
)


class _Runner:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.commands: list[str] = []

    def run(self, command: str):
        from app.tools.terminal_tools import (
            CommandResult,
        )

        self.commands.append(command)

        return CommandResult(
            command=command,
            returncode=self.returncode,
            stdout="1 passed",
        )


def test_pytest_criterion_targets_the_named_test_file():
    runner = _Runner()

    evaluator = CriterionEvaluator(
        workspace=None,
        command_runner=runner,
    )

    result = evaluator.evaluate(
        "python -m pytest tests/test_calculator.py passes"
    )

    assert result.check == "pytest"
    assert result.status == "PASS"
    assert runner.commands == [
        'python -m pytest -q "tests/test_calculator.py"'
    ]


def test_pytest_criterion_without_a_file_runs_the_suite():
    runner = _Runner()

    evaluator = CriterionEvaluator(
        workspace=None,
        command_runner=runner,
    )

    evaluator.evaluate("all tests pass")

    assert runner.commands == ["python -m pytest -q"]


def test_failing_pytest_criterion_is_fail_not_blocked():
    runner = _Runner(returncode=1)

    result = CriterionEvaluator(
        workspace=None,
        command_runner=runner,
    ).evaluate("test_calc.py passes")

    assert result.status == "FAIL"
    assert result.check == "pytest"
