from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

from app.agent.runtime import AgentRuntime
from app.agent.session import SessionStatus
from app.agent.state import AgentPhase, AgentState, VerificationResult
from app.tasks.verification_context import VerificationContext
from app.vscode_backend import BackendSession, serve

from helpers import FakeLLM


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _protocol_messages(
    backend: BackendSession,
    *messages: dict[str, object],
) -> list[dict[str, object]]:
    stdin = io.StringIO(
        "".join(json.dumps(message) + "\n" for message in messages)
    )
    stdout = io.StringIO()
    serve(stdin=stdin, stdout=stdout, session=backend)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def test_public_backend_protocol_runs_previews_applies_and_tests_project(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "Unicode project Пример"
    project.mkdir()
    (project / "app.py").write_text(
        "def greeting():\n    return 'before'\n",
        encoding="utf-8",
    )
    (project / "test_app.py").write_text(
        "import unittest\n\n"
        "from app import greeting\n\n"
        "class AppTest(unittest.TestCase):\n"
        "    def test_greeting(self):\n"
        "        self.assertEqual(greeting(), 'after')\n",
        encoding="utf-8",
    )
    (project / "obsolete.py").write_text("OLD = True\n", encoding="utf-8")
    source_before = _files(project)
    runtime = AgentRuntime(
        project_root=project,
        database_path=tmp_path / "runtime.db",
        llm=FakeLLM(default="unused"),
        load_policy=False,
    )

    def complete_verified_run(_loop, request: str) -> AgentState:
        workspace = Path(runtime.workspace_root)
        (workspace / "app.py").write_text(
            "from helper import suffix\n\n"
            "def greeting():\n    return 'after' + suffix()\n",
            encoding="utf-8",
        )
        (workspace / "helper.py").write_text(
            "def suffix():\n    return ''\n",
            encoding="utf-8",
        )
        (workspace / "obsolete.py").unlink()
        context = VerificationContext.capture(
            workspace,
            specs=[],
            environment={"runner": "backend-acceptance-fixture"},
        )
        return AgentState(
            request=request,
            phase=AgentPhase.DONE,
            completion="DONE",
            verification=VerificationResult(
                ok=True,
                status="PASS",
                reason="acceptance fixture verified",
                context=context,
            ),
        )

    monkeypatch.setattr("app.agent.runtime.AgentLoop.run", complete_verified_run)
    backend = BackendSession()
    monkeypatch.setattr(backend, "_get_runtime", lambda _project_root: runtime)

    run_messages = _protocol_messages(
        backend,
        {
            "type": "run",
            "request_id": "run-1",
            "request": "update the app and its tests",
            "project_root": str(project),
            "work_mode": "sandbox",
        },
    )
    completed = next(
        message for message in run_messages if message["type"] == "run_completed"
    )
    result = completed["result"]

    assert _files(project) == source_before
    assert result["workflow_status"] == "verified"
    assert result["can_apply"] is True
    assert {
        (entry["path"], entry["operation"])
        for entry in result["change_entries"]
    } == {
        ("app.py", "MODIFY"),
        ("helper.py", "ADD"),
        ("obsolete.py", "DELETE"),
    }

    apply_messages = _protocol_messages(
        backend,
        {
            "type": "apply",
            "request_id": "apply-1",
            "project_root": str(project),
        },
        {"type": "shutdown"},
    )
    applied = next(
        message
        for message in apply_messages
        if message["type"] == "action_completed"
    )["result"]

    assert applied["workflow_status"] == "applied"
    assert applied["ok"] is True
    assert _files(project) == _files(Path(runtime.workspace_root))
    assert runtime.session.status is SessionStatus.CLEAN
    project_tests = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-q"],
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )
    assert project_tests.returncode == 0, project_tests.stderr


def test_verified_no_change_request_returns_to_clean_without_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "no change project"
    project.mkdir()
    (project / "keep.txt").write_text("same\n", encoding="utf-8")
    source_before = _files(project)
    runtime = AgentRuntime(
        project_root=project,
        database_path=tmp_path / "runtime.db",
        llm=FakeLLM(default="unused"),
        load_policy=False,
    )

    def complete_verified_run(_loop, request: str) -> AgentState:
        context = VerificationContext.capture(
            Path(runtime.workspace_root),
            specs=[],
            environment={"runner": "no-change-acceptance-fixture"},
        )
        return AgentState(
            request=request,
            phase=AgentPhase.DONE,
            completion="DONE",
            verification=VerificationResult(
                ok=True,
                status="PASS",
                reason="nothing needed",
                context=context,
            ),
        )

    monkeypatch.setattr("app.agent.runtime.AgentLoop.run", complete_verified_run)

    state = runtime.run("inspect the project; do not change correct files")

    assert state.phase is AgentPhase.DONE
    assert runtime.session.status is SessionStatus.CLEAN
    assert runtime.session.patch_manifest_id is None
    assert runtime.patch_preview()["manifest_id"] is None
    assert _files(project) == source_before


def test_verified_add_modify_delete_flow_uses_one_durable_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "user project"
    project.mkdir()
    (project / "modify.txt").write_text("before\n", encoding="utf-8")
    (project / "delete.txt").write_text("obsolete\n", encoding="utf-8")
    (project / "keep.txt").write_text("same\n", encoding="utf-8")
    source_before = _files(project)

    runtime = AgentRuntime(
        project_root=project,
        database_path=tmp_path / "runtime.db",
        llm=FakeLLM(default="unused"),
        load_policy=False,
    )

    def complete_verified_run(_loop, request: str) -> AgentState:
        workspace = Path(runtime.workspace_root)
        (workspace / "modify.txt").write_text("after\n", encoding="utf-8")
        (workspace / "delete.txt").unlink()
        (workspace / "nested").mkdir()
        (workspace / "nested" / "added.txt").write_text(
            "created\n",
            encoding="utf-8",
        )
        context = VerificationContext.capture(
            workspace,
            specs=[],
            environment={"runner": "acceptance-fixture"},
        )
        return AgentState(
            request=request,
            phase=AgentPhase.DONE,
            completion="DONE",
            verification=VerificationResult(
                ok=True,
                status="PASS",
                reason="acceptance fixture verified",
                context=context,
            ),
        )

    monkeypatch.setattr(
        "app.agent.runtime.AgentLoop.run",
        complete_verified_run,
    )

    state = runtime.run("update this project")

    assert state.phase is AgentPhase.DONE
    assert _files(project) == source_before
    assert runtime.session is not None
    assert runtime.session.status is SessionStatus.DIRTY_VERIFIED
    manifest_id = runtime.session.patch_manifest_id
    assert manifest_id is not None

    preview = runtime.patch_preview()
    assert preview["manifest_id"] == manifest_id
    assert preview["manifest_current"] is True
    assert preview["can_apply"] is True
    assert {
        (entry["path"], entry["operation"])
        for entry in preview["entries"]
    } == {
        ("delete.txt", "DELETE"),
        ("modify.txt", "MODIFY"),
        ("nested/added.txt", "ADD"),
    }

    result = runtime.apply_patch(confirmed=True)

    assert result["status"] == "COMMITTED"
    assert set(result["applied"]) == {
        "delete.txt",
        "modify.txt",
        "nested/added.txt",
    }
    assert _files(project) == _files(Path(runtime.workspace_root))
    assert runtime.session.status is SessionStatus.CLEAN
    assert runtime.session.patch_manifest_id is None


def test_workspace_change_after_verified_preview_blocks_every_source_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "value.txt").write_text("source\n", encoding="utf-8")

    runtime = AgentRuntime(
        project_root=project,
        database_path=tmp_path / "runtime.db",
        llm=FakeLLM(default="unused"),
        load_policy=False,
    )

    def complete_verified_run(_loop, request: str) -> AgentState:
        workspace = Path(runtime.workspace_root)
        (workspace / "value.txt").write_text("verified\n", encoding="utf-8")
        context = VerificationContext.capture(
            workspace,
            specs=[],
            environment={"runner": "acceptance-fixture"},
        )
        return AgentState(
            request=request,
            phase=AgentPhase.DONE,
            completion="DONE",
            verification=VerificationResult(
                ok=True,
                status="PASS",
                reason="acceptance fixture verified",
                context=context,
            ),
        )

    monkeypatch.setattr(
        "app.agent.runtime.AgentLoop.run",
        complete_verified_run,
    )
    runtime.run("change value")
    assert runtime.patch_preview()["can_apply"] is True

    (Path(runtime.workspace_root) / "value.txt").write_text(
        "tampered\n",
        encoding="utf-8",
    )
    result = runtime.apply_patch(confirmed=True)

    assert result["applied"] == []
    assert "workspace changed" in str(result["reason"])
    assert (project / "value.txt").read_text(encoding="utf-8") == "source\n"


def test_verified_result_can_be_previewed_and_applied_after_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "restart project"
    project.mkdir()
    (project / "value.txt").write_text("source\n", encoding="utf-8")
    database = tmp_path / "runtime.db"
    first = AgentRuntime(
        project_root=project,
        database_path=database,
        llm=FakeLLM(default="unused"),
        load_policy=False,
    )

    def complete_verified_run(_loop, request: str) -> AgentState:
        workspace = Path(first.workspace_root)
        (workspace / "value.txt").write_text("verified\n", encoding="utf-8")
        context = VerificationContext.capture(
            workspace,
            specs=[],
            environment={"runner": "acceptance-fixture"},
        )
        return AgentState(
            request=request,
            phase=AgentPhase.DONE,
            completion="DONE",
            verification=VerificationResult(
                ok=True,
                status="PASS",
                reason="acceptance fixture verified",
                context=context,
            ),
        )

    monkeypatch.setattr(
        "app.agent.runtime.AgentLoop.run",
        complete_verified_run,
    )
    first.run("change value")
    manifest_id = first.session.patch_manifest_id
    sandbox_session_id = first.session_id

    restarted = AgentRuntime(
        project_root=project,
        database_path=database,
        llm=FakeLLM(default="unused"),
        load_policy=False,
    )

    assert restarted.session_id == sandbox_session_id
    assert restarted.session.patch_manifest_id == manifest_id
    assert restarted.last_state is None
    assert restarted.patch_preview()["can_apply"] is True

    backend = BackendSession()
    monkeypatch.setattr(backend, "_get_runtime", lambda _project_root: restarted)
    recovered = backend.inspect(str(project))
    assert recovered["workflow_status"] == "verified"
    assert recovered["next_actions"] == ["apply", "discard"]
    assert recovered["manifest_id"] == manifest_id

    result = restarted.apply_patch(confirmed=True)

    assert result["status"] == "COMMITTED"
    assert (project / "value.txt").read_text(encoding="utf-8") == "verified\n"
    assert restarted.session.status is SessionStatus.CLEAN


def test_protected_change_is_visible_but_blocks_the_whole_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("SECRET=source\n", encoding="utf-8")
    runtime = AgentRuntime(
        project_root=project,
        database_path=tmp_path / "runtime.db",
        llm=FakeLLM(default="unused"),
        load_policy=False,
    )

    def complete_verified_run(_loop, request: str) -> AgentState:
        workspace = Path(runtime.workspace_root)
        (workspace / ".env").write_text("SECRET=changed\n", encoding="utf-8")
        context = VerificationContext.capture(
            workspace,
            specs=[],
            environment={"runner": "acceptance-fixture"},
        )
        return AgentState(
            request=request,
            phase=AgentPhase.DONE,
            completion="DONE",
            verification=VerificationResult(
                ok=True,
                status="PASS",
                reason="acceptance fixture verified",
                context=context,
            ),
        )

    monkeypatch.setattr(
        "app.agent.runtime.AgentLoop.run",
        complete_verified_run,
    )
    runtime.run("change protected file")

    preview = runtime.patch_preview()
    assert preview["changed_files"] == [".env"]
    assert preview["entries"][0]["apply_safe"] is False
    assert preview["entries"][0]["reasons"]
    assert preview["can_apply"] is False

    result = runtime.apply_patch(confirmed=True)

    assert result["applied"] == []
    assert "blocked" in str(result["reason"])
    assert (project / ".env").read_text(encoding="utf-8") == "SECRET=source\n"


def test_source_change_after_preview_returns_conflict_without_overwrite(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "value.txt").write_text("source\n", encoding="utf-8")
    runtime = AgentRuntime(
        project_root=project,
        database_path=tmp_path / "runtime.db",
        llm=FakeLLM(default="unused"),
        load_policy=False,
    )

    def complete_verified_run(_loop, request: str) -> AgentState:
        workspace = Path(runtime.workspace_root)
        (workspace / "value.txt").write_text("verified\n", encoding="utf-8")
        context = VerificationContext.capture(
            workspace,
            specs=[],
            environment={"runner": "acceptance-fixture"},
        )
        return AgentState(
            request=request,
            phase=AgentPhase.DONE,
            completion="DONE",
            verification=VerificationResult(
                ok=True,
                status="PASS",
                reason="acceptance fixture verified",
                context=context,
            ),
        )

    monkeypatch.setattr(
        "app.agent.runtime.AgentLoop.run",
        complete_verified_run,
    )
    runtime.run("change value")
    assert runtime.patch_preview()["can_apply"] is True

    (project / "value.txt").write_text("external edit\n", encoding="utf-8")
    result = runtime.apply_patch(confirmed=True)

    assert result["status"] == "CONFLICT"
    assert result["applied"] == []
    assert (project / "value.txt").read_text(encoding="utf-8") == "external edit\n"
    assert runtime.session.status is SessionStatus.DIRTY_VERIFIED


def test_restart_settles_durable_commit_that_crashed_before_rebase(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "value.txt").write_text("source\n", encoding="utf-8")
    database = tmp_path / "runtime.db"
    first = AgentRuntime(
        project_root=project,
        database_path=database,
        llm=FakeLLM(default="unused"),
        load_policy=False,
    )

    def complete_verified_run(_loop, request: str) -> AgentState:
        workspace = Path(first.workspace_root)
        (workspace / "value.txt").write_text("verified\n", encoding="utf-8")
        context = VerificationContext.capture(
            workspace,
            specs=[],
            environment={"runner": "acceptance-fixture"},
        )
        return AgentState(
            request=request,
            phase=AgentPhase.DONE,
            completion="DONE",
            verification=VerificationResult(
                ok=True,
                status="PASS",
                reason="acceptance fixture verified",
                context=context,
            ),
        )

    monkeypatch.setattr(
        "app.agent.runtime.AgentLoop.run",
        complete_verified_run,
    )
    first.run("change value")
    manifest_id = first.session.patch_manifest_id

    committed = first._new_apply_service().apply(
        manifest_id,
        agent_session_id=first.session.id,
        expected_session_version=first.session.version,
    )
    assert committed.status.value == "COMMITTED"
    assert first.session_store.get(first.session.id).status is SessionStatus.APPLIED

    restarted = AgentRuntime(
        project_root=project,
        database_path=database,
        llm=FakeLLM(default="unused"),
        load_policy=False,
    )

    assert restarted.session.id == first.session.id
    assert restarted.session.status is SessionStatus.CLEAN
    assert restarted.session.patch_manifest_id is None
    assert _files(Path(restarted.workspace_root)) == _files(project)
    assert (project / "value.txt").read_text(encoding="utf-8") == "verified\n"
