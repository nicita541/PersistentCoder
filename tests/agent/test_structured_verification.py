from __future__ import annotations

from app.agent.coder.workspace import Workspace
from app.agent.verifier.structured import StructuredVerifier
from app.tasks.verification_spec import VerificationKind, VerificationSpec
from app.tasks.models import PlanDraft, StepDraft, TaskDraft

from helpers import RecordingCommandRunner, make_stores


def _verifier(tmp_path, runner=None):
    return StructuredVerifier(
        workspace=Workspace(tmp_path),
        command_runner=runner,
    )


def test_file_exists_and_absent_are_exact(tmp_path):
    (tmp_path / "present.txt").write_text("x", encoding="utf-8")
    verifier = _verifier(tmp_path)

    results = verifier.verify_all(
        [
            VerificationSpec(VerificationKind.FILE_EXISTS, "present.txt"),
            VerificationSpec(VerificationKind.FILE_ABSENT, "removed.txt"),
        ]
    )

    assert [result.status for result in results] == ["PASS", "PASS"]


def test_python_symbol_and_signature_use_ast(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "mod.py").write_text(
        "class Calculator:\n"
        "    def add(self, left, right=0):\n"
        "        return left + right\n",
        encoding="utf-8",
    )
    verifier = _verifier(tmp_path)

    results = verifier.verify_all(
        [
            VerificationSpec(
                VerificationKind.PY_SYMBOL,
                "pkg/mod.py",
                symbol="Calculator.add",
            ),
            VerificationSpec(
                VerificationKind.PY_SIGNATURE,
                "pkg/mod.py",
                symbol="Calculator.add",
                expected_signature=["self", "left", "right=0"],
            ),
        ]
    )

    assert all(result.status == "PASS" for result in results)


def test_signature_contract_includes_kinds_defaults_and_variadics(tmp_path):
    (tmp_path / "api.py").write_text(
        "def run(first, /, second=2, *args, flag=True, **kwargs):\n"
        "    return first\n",
        encoding="utf-8",
    )
    exact = VerificationSpec(
        VerificationKind.PY_SIGNATURE,
        "api.py",
        symbol="run",
        expected_signature=[
            "first", "/", "second=2", "*args", "flag=True", "**kwargs"
        ],
    )
    incomplete = VerificationSpec(
        VerificationKind.PY_SIGNATURE,
        "api.py",
        symbol="run",
        expected_signature=["first", "second"],
    )

    exact_result, incomplete_result = _verifier(tmp_path).verify_all(
        [exact, incomplete]
    )

    assert exact_result.status == "PASS"
    assert incomplete_result.status == "FAIL"


def test_missing_ast_symbol_fails_even_when_file_exists(tmp_path):
    (tmp_path / "calculator.py").write_text("VALUE = 1\n", encoding="utf-8")

    result = _verifier(tmp_path).verify_all(
        [
            VerificationSpec(
                VerificationKind.PY_SYMBOL,
                "calculator.py",
                symbol="add",
            )
        ]
    )[0]

    assert result.status == "FAIL"
    assert "missing" in result.reason


def test_nested_import_uses_full_module_name(tmp_path):
    runner = RecordingCommandRunner(stdout="ok")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "sub").mkdir()
    (tmp_path / "pkg" / "sub" / "mod.py").write_text("VALUE = 1\n")

    result = _verifier(tmp_path, runner).verify_all(
        [VerificationSpec(VerificationKind.PY_IMPORT, "pkg/sub/mod.py")]
    )[0]

    assert result.status == "PASS"
    assert runner.argv_commands == [
        ["python", "-c", "import pkg.sub.mod"]
    ]
    assert runner.commands == []


def test_all_explicit_pytest_targets_run_together(tmp_path):
    runner = RecordingCommandRunner(stdout="2 passed")
    (tmp_path / "test_a.py").write_text("def test_a(): assert True\n")
    (tmp_path / "test_b.py").write_text("def test_b(): assert True\n")

    results = _verifier(tmp_path, runner).verify_all(
        [
            VerificationSpec(VerificationKind.PYTEST, "test_a.py"),
            VerificationSpec(VerificationKind.PYTEST, "test_b.py"),
        ]
    )

    assert all(result.status == "PASS" for result in results)
    assert runner.argv_commands == [[
        "python", "-m", "pytest", "-q", "test_a.py", "test_b.py"
    ]]


def test_python_compile_passes_shell_metacharacters_as_one_argv_item(tmp_path):
    runner = RecordingCommandRunner(stdout="ok")

    result = _verifier(tmp_path, runner).verify_all(
        [
            VerificationSpec(
                VerificationKind.PY_COMPILE,
                "sample.py; touch PWNED",
            )
        ]
    )[0]

    assert result.status == "PASS"
    assert runner.argv_commands == [[
        "python", "-m", "py_compile", "sample.py; touch PWNED"
    ]]


def test_pytest_without_target_is_blocked_unless_full_suite_explicit(tmp_path):
    runner = RecordingCommandRunner(stdout="should not run")

    result = _verifier(tmp_path, runner).verify_all(
        [VerificationSpec(VerificationKind.PYTEST)]
    )[0]

    assert result.status == "BLOCKED"
    assert runner.argv_commands == []


def test_missing_sandbox_environment_is_blocked(tmp_path):
    runner = RecordingCommandRunner(
        returncode=127,
        stderr="sandbox unavailable: Docker daemon is a prerequisite",
    )
    (tmp_path / "app.py").write_text("VALUE = 1\n")

    result = _verifier(tmp_path, runner).verify_all([
        VerificationSpec(VerificationKind.PY_COMPILE, "app.py")
    ])[0]

    assert result.status == "BLOCKED"
    assert "sandbox unavailable" in result.reason


def test_pytest_no_tests_collected_is_not_a_pass(tmp_path):
    runner = RecordingCommandRunner(returncode=5, stdout="no tests ran")

    result = _verifier(tmp_path, runner).verify_all([
        VerificationSpec(VerificationKind.PYTEST, full_suite_allowed=True)
    ])[0]

    assert result.status == "FAIL"


def test_verification_specs_round_trip_through_task_and_step_stores(tmp_path):
    stores = make_stores(tmp_path)
    task_spec = VerificationSpec(VerificationKind.PY_SYMBOL, "app.py", symbol="run")
    plan_id = stores.plan_store.create_plan(
        PlanDraft(
            user_request="verify",
            global_goal="verify",
            tasks=[
                TaskDraft(
                    key="app",
                    title="App",
                    description="App",
                    success_criteria=["human display only"],
                    change_paths=["app.py"],
                    verification_specs=[task_spec],
                )
            ],
        )
    )
    task = stores.plan_store.get_tasks(plan_id)[0]
    step_spec = VerificationSpec(VerificationKind.PY_COMPILE, "app.py")
    step_id = stores.step_store.create_steps(
        task.id,
        [
            StepDraft(
                title="App",
                description="App",
                success_criteria=["display"],
                change_paths=["app.py"],
                verification_specs=[step_spec],
            )
        ],
    )[0]

    assert stores.plan_store.get_tasks(plan_id)[0].verification_specs == [task_spec]
    assert stores.step_store.get_step(step_id).verification_specs == [step_spec]
