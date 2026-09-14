from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.agent.runtime import AgentRuntime
from app.agent.session import SessionStatus
from app.agent.state import (
    AgentPhase,
    VerificationResult,
)

from helpers import (
    FakeLLM,
    coder_envelope,
    dependencies_response,
    goal_response,
    tasks_response,
)


def _project(root: Path) -> Path:
    (root / "src").mkdir(parents=True)

    (root / "src" / "app.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )

    (root / ".env").write_text(
        "SECRET=host-only\n",
        encoding="utf-8",
    )

    return root


def _runtime(project: Path, database: Path) -> AgentRuntime:
    return AgentRuntime(
        project_root=project,
        database_path=database,
        llm=FakeLLM(
            [
                goal_response(),
                tasks_response(),
                dependencies_response(),
            ],
            default=coder_envelope(),
        ),
        system_prompt="GLOBAL SYSTEM POLICY",
    )


def _tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): path.read_text(
            encoding="utf-8"
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_a_successful_run_never_touches_the_project(tmp_path):
    project = _project(tmp_path / "project")

    before = _tree(project)

    runtime = _runtime(project, tmp_path / "pc.db")

    state = runtime.run("Создай artifact.txt")

    assert state.phase is AgentPhase.DONE
    assert state.patch_path

    # The patch lives inside the sandbox...
    assert str(state.patch_path).startswith(
        str(runtime.sandbox_workspace.patch_root)
    )

    # ... and the host project is byte-identical.
    assert _tree(project) == before

    # The sandbox did receive the work.
    assert (
        Path(runtime.workspace_root) / "artifact.txt"
    ).exists()


def test_apply_requires_explicit_confirmation(tmp_path):
    project = _project(tmp_path / "project")

    runtime = _runtime(project, tmp_path / "pc.db")

    (
        Path(runtime.workspace_root) / "src" / "app.py"
    ).write_text("VALUE = 2\n", encoding="utf-8")

    result = runtime.apply_patch()

    assert result["applied"] == []
    assert (
        result["reason"]
        == "explicit confirmation required"
    )

    assert (project / "src" / "app.py").read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"


def test_apply_refused_when_run_is_not_done(tmp_path):
    project = _project(tmp_path / "project")

    runtime = _runtime(project, tmp_path / "pc.db")

    runtime.last_state = SimpleNamespace(
        phase=AgentPhase.FAILED,
        verification=VerificationResult(
            ok=False,
            status="FAIL",
            reason="criterion failed",
        ),
    )

    result = runtime.apply_patch(confirmed=True)

    assert result["applied"] == []
    assert "not DONE" in str(result["reason"])


def test_apply_refused_when_verification_failed(tmp_path):
    project = _project(tmp_path / "project")

    runtime = _runtime(project, tmp_path / "pc.db")

    runtime.last_state = SimpleNamespace(
        phase=AgentPhase.DONE,
        verification=VerificationResult(
            ok=False,
            status="FAIL",
            reason="pytest failed",
        ),
    )

    result = runtime.apply_patch(confirmed=True)

    assert result["applied"] == []
    assert "verification" in str(result["reason"])


def test_apply_targets_source_project_not_module_project_root(
    tmp_path,
    monkeypatch,
):
    project = _project(
        tmp_path / "source-project"
    )

    wrong_project = _project(
        tmp_path / "wrong-project"
    )

    monkeypatch.setattr(
        "app.agent.runtime.PROJECT_ROOT",
        wrong_project,
    )

    runtime = _runtime(
        project,
        tmp_path / "pc.db",
    )

    workspace = Path(
        runtime.workspace_root
    )

    (
        workspace
        / "src"
        / "app.py"
    ).write_text(
        "VALUE = 2\n",
        encoding="utf-8",
    )

    # Protected file must never be applied.
    (
        workspace
        / ".env"
    ).write_text(
        "SECRET=stolen\n",
        encoding="utf-8",
    )

    patch = (
        runtime
        .sandbox_workspace
        .write_patch()
    )

    assert patch is not None

    runtime.last_patch_path = str(
        patch
    )

    runtime.last_state = SimpleNamespace(
        phase=AgentPhase.DONE,
        verification=VerificationResult(
            ok=True,
            status="PASS",
            reason="all criteria passed",
        ),
    )
    assert runtime.session is not None
    runtime.session.transition(SessionStatus.RUNNING)
    runtime.session.transition(SessionStatus.DIRTY_VERIFIED)
    runtime.session = runtime.session_store.update(runtime.session)

    preview = runtime.patch_preview()

    assert (
        "src/app.py"
        in preview["changed_files"]
    )

    assert (
        ".env"
        not in preview["changed_files"]
    )

    result = runtime.apply_patch(
        confirmed=True
    )

    assert result["applied"] == [
        "src/app.py",
    ]

    # MUST update the actual project
    # AgentRuntime was created for.
    assert (
        project
        / "src"
        / "app.py"
    ).read_text(
        encoding="utf-8"
    ) == "VALUE = 2\n"

    # MUST NOT use module-level PROJECT_ROOT.
    assert (
        wrong_project
        / "src"
        / "app.py"
    ).read_text(
        encoding="utf-8"
    ) == "VALUE = 1\n"

    # Protected host file remains untouched.
    assert (
        project
        / ".env"
    ).read_text(
        encoding="utf-8"
    ) == "SECRET=host-only\n"
