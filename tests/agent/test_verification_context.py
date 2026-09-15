from __future__ import annotations

from app.tasks.verification_context import VerificationContext
from app.tasks.verification_spec import VerificationKind, VerificationSpec
from app.tasks.models import PlanDraft, TaskDraft, VerificationStatus
from helpers import make_stores


def test_context_is_bound_to_workspace_specs_and_environment(tmp_path):
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    specs = [VerificationSpec(VerificationKind.PY_COMPILE, "app.py")]
    context = VerificationContext.capture(
        tmp_path,
        specs=specs,
        environment={"image": "sandbox:v1", "python": "3.12"},
    )

    assert context.matches(
        tmp_path,
        specs=specs,
        environment={"image": "sandbox:v1", "python": "3.12"},
    )

    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    assert not context.matches(
        tmp_path,
        specs=specs,
        environment={"image": "sandbox:v1", "python": "3.12"},
    )


def test_context_rejects_changed_spec_or_environment(tmp_path):
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    original = [VerificationSpec(VerificationKind.PY_COMPILE, "app.py")]
    context = VerificationContext.capture(
        tmp_path, specs=original, environment={"image": "sandbox:v1"}
    )

    changed = [VerificationSpec(VerificationKind.FILE_EXISTS, "app.py")]
    assert not context.matches(
        tmp_path, specs=changed, environment={"image": "sandbox:v1"}
    )
    assert not context.matches(
        tmp_path, specs=original, environment={"image": "sandbox:v2"}
    )


def test_context_rejects_changed_legacy_success_criteria(tmp_path):
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    context = VerificationContext.capture(
        tmp_path,
        specs=[],
        criteria=["app.py exists"],
        environment={"image": "sandbox:v1"},
    )

    assert not context.matches(
        tmp_path,
        specs=[],
        criteria=["app.py compiles"],
        environment={"image": "sandbox:v1"},
    )


def test_gitignored_test_configuration_still_invalidates_evidence(tmp_path):
    (tmp_path / ".gitignore").write_text("pytest.ini\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    context = VerificationContext.capture(tmp_path, specs=[], environment={})

    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

    assert not context.matches(tmp_path, specs=[], environment={})


def test_context_round_trips_as_canonical_json(tmp_path):
    (tmp_path / "app.py").write_text("x\n", encoding="utf-8")
    context = VerificationContext.capture(tmp_path, specs=[], environment={})

    assert VerificationContext.from_dict(context.to_dict()) == context


def test_persisted_pass_is_trusted_only_while_context_is_current(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("x = 1\n", encoding="utf-8")
    stores = make_stores(tmp_path)
    spec = VerificationSpec(VerificationKind.PY_COMPILE, "app.py")
    plan_id = stores.plan_store.create_plan(PlanDraft(
        user_request="verify",
        global_goal="verify",
        tasks=[TaskDraft(title="app", description="app", verification_specs=[spec])],
    ))
    task = stores.plan_store.get_tasks(plan_id)[0]
    context = VerificationContext.capture(
        project, specs=[spec], environment={"image": "sandbox:v1"}
    )
    record = stores.verification_store.record_task(
        task.id,
        status=VerificationStatus.PASS,
        evidence=["compiled"],
        context=context,
    )

    assert stores.verification_store.is_current(
        record,
        project,
        specs=[spec],
        environment={"image": "sandbox:v1"},
    )
    (project / "app.py").write_text("x = 2\n", encoding="utf-8")
    assert not stores.verification_store.is_current(
        record,
        project,
        specs=[spec],
        environment={"image": "sandbox:v1"},
    )
