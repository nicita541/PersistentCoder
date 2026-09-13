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


class _Workspace:
    def __init__(self, existing) -> None:
        self._existing = set(existing)

    def exists(self, path: str) -> bool:
        return path in self._existing


def test_generic_pytest_criterion_targets_changed_tests():
    runner = _Runner()

    evaluator = CriterionEvaluator(
        workspace=_Workspace({"test_calculator.py"}),
        command_runner=runner,
        test_targets=["test_calculator.py"],
    )

    result = evaluator.evaluate("all tests pass")

    assert result.status == "PASS"
    assert runner.commands == [
        'python -m pytest -q "test_calculator.py"'
    ]


def test_generic_criterion_ignores_non_test_targets():
    runner = _Runner()

    evaluator = CriterionEvaluator(
        workspace=_Workspace({"calculator.py"}),
        command_runner=runner,
        test_targets=["calculator.py"],
    )

    evaluator.evaluate("all tests pass")

    # No test file was changed: the suite is the only honest scope.
    assert runner.commands == ["python -m pytest -q"]


def test_explicit_criterion_target_wins_over_changed_tests():
    runner = _Runner()

    evaluator = CriterionEvaluator(
        workspace=_Workspace({"test_a.py"}),
        command_runner=runner,
        test_targets=["test_a.py"],
    )

    evaluator.evaluate("test_b.py passes")

    assert runner.commands == [
        'python -m pytest -q "test_b.py"'
    ]


def test_failing_pytest_criterion_is_fail_not_blocked():
    runner = _Runner(returncode=1)

    result = CriterionEvaluator(
        workspace=None,
        command_runner=runner,
    ).evaluate("test_calc.py passes")

    assert result.status == "FAIL"
    assert result.check == "pytest"
