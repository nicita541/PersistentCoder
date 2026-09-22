from __future__ import annotations

import sqlite3
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.session import SessionStatus
from app.apply.builder import PatchManifestBuilder
from app.apply.manifest import PatchManifest
from app.apply.service import ApplyResult, ApplyService, ApplyStatus
from app.apply.store import PatchManifestStore
from app.project_identity import ProjectIdentity
from app.sandbox.runner import CancellationToken
from app.tasks.session_store import SessionStore
from app.tasks.store_context import StoreContext
from app.tasks.unit_of_work import RuntimeUnitOfWork


def tree(root: Path):
    return {p.relative_to(root).as_posix(): ("dir" if p.is_dir() else p.read_bytes())
            for p in root.rglob("*")}


def fixture(tmp_path, *, name="project", nested=True, extra_additions=()):
    base = tmp_path / name
    source, baseline, workspace = (base / item for item in ("source", "baseline", "workspace"))
    for root in (source, baseline, workspace):
        root.mkdir(parents=True)
        (root / "b.py").write_bytes(b"before\n")
        (root / "z.py").write_bytes(b"delete\n")
        (root / "keep.py").write_bytes(b"unchanged\n")
    addition = "new/deep/a.py" if nested else "a.py"
    (workspace / addition).parent.mkdir(parents=True, exist_ok=True)
    (workspace / addition).write_bytes(b"added\n")
    (workspace / "b.py").write_bytes(b"after\n")
    (workspace / "z.py").unlink()
    for path in extra_additions:
        (workspace / path).parent.mkdir(parents=True, exist_ok=True)
        (workspace / path).write_bytes(b"additional\n")
    identity = ProjectIdentity.from_source_root(source)
    context = StoreContext(tmp_path / "state.db", identity.project_id, str(source.resolve()))
    sessions = SessionStore(context)
    session_id = sessions.create(sandbox_session_id=name + "-sandbox")
    manifest = PatchManifestBuilder().build(project_identity=identity, session_id=name + "-sandbox",
        baseline_root=baseline, workspace_root=workspace, verification_id="verified")
    store = PatchManifestStore(context)
    store.save(manifest, agent_session_id=session_id)
    session = sessions.get(session_id)
    session.status = SessionStatus.DIRTY_VERIFIED
    session.patch_manifest_id = manifest.manifest_id
    session = sessions.update(session)
    uow = RuntimeUnitOfWork(context)
    staging = base / "staging"
    service = ApplyService(context, store, uow, source, workspace, staging)
    return SimpleNamespace(**locals())


def apply(f, token=None):
    return f.service.apply(f.manifest.manifest_id, agent_session_id=f.session_id,
        expected_session_version=f.session.version, cancellation_token=token)


def test_add_modify_delete_commit_in_manifest_order(tmp_path):
    f = fixture(tmp_path)
    observed = []
    f.service._boundary = lambda event, path=None: observed.append((event, path))
    result = apply(f)
    assert result.status is ApplyStatus.COMMITTED
    assert result.applied_paths == tuple(e.path.value for e in f.manifest.entries)
    assert tree(f.source) == tree(f.workspace)
    assert [path for event, path in observed if event == "after_mutation"] == list(result.applied_paths)
    assert f.sessions.get(f.session_id).status is SessionStatus.APPLIED
    assert f.uow.get_apply_journal(result.journal_id)["state"] == "COMMITTED"
    assert f.uow.pending_apply_journals() == []
    assert not list(f.staging.iterdir())
    assert f.workspace.is_dir()  # Rebase/discard belongs to the runtime.


def test_result_is_frozen_bounded_and_canonical():
    result = ApplyResult(ApplyStatus.CONFLICT, (), "x" * 4000, "a" * 64, None)
    assert len(result.reason) <= 2000
    with pytest.raises(FrozenInstanceError):
        result.reason = "change"
    with pytest.raises(ValueError):
        ApplyResult(ApplyStatus.COMMITTED, ("../escape",), None, "a" * 64, 1)


@pytest.mark.parametrize("change", ["source", "workspace", "unchanged_workspace", "missing",
    "type", "add_exists", "delete_workspace", "session", "version", "status", "sandbox",
    "manifest", "unsafe", "unreviewable"])
def test_preflight_conflicts_have_no_artifacts_or_reservation(tmp_path, change):
    f = fixture(tmp_path)
    if change == "source":
        (f.source / "b.py").write_bytes(b"stale\n")
    elif change == "workspace":
        (f.workspace / "b.py").write_bytes(b"changed\n")
    elif change == "unchanged_workspace":
        (f.workspace / "keep.py").write_bytes(b"changed\n")
    elif change == "missing":
        (f.workspace / "b.py").unlink()
    elif change == "type":
        (f.source / "b.py").unlink()
        (f.source / "b.py").mkdir()
    elif change == "add_exists":
        (f.source / f.addition).parent.mkdir(parents=True)
        (f.source / f.addition).write_bytes(b"existing")
    elif change == "delete_workspace":
        (f.workspace / "z.py").write_bytes(b"unexpected")
    elif change == "session":
        f.session_id = "foreign"
    elif change == "version":
        f.session.version += 1
    elif change in {"status", "sandbox"}:
        session = f.sessions.get(f.session_id)
        if change == "status":
            session.status = SessionStatus.DIRTY_FAILED
        else:
            session.sandbox_session_id = "foreign"
        f.session = f.sessions.update(session)
    elif change == "manifest":
        f.manifest = replace(f.manifest)  # Store, not the caller, is authoritative.
        with sqlite3.connect(f.context.database_path) as connection:
            connection.execute("DROP TRIGGER patch_manifest_entries_immutable_update")
            connection.execute("UPDATE patch_manifest_entries SET after_size = after_size + 1 WHERE after_size IS NOT NULL")
    else:
        entries = list(f.manifest.entries)
        entries[0] = replace(entries[0], apply_safe=False, reviewable=change != "unreviewable")
        f.manifest = PatchManifest.create(**{key: getattr(f.manifest, key) for key in
            ("project_id", "canonical_source_root", "session_id", "baseline_sha256", "workspace_sha256", "verification_id")}, entries=entries)
        f.store.save(f.manifest, agent_session_id=f.session_id)
        session = f.sessions.get(f.session_id)
        session.patch_manifest_id = f.manifest.manifest_id
        f.session = f.sessions.update(session)
    before = tree(f.source)
    result = apply(f)
    assert result.status is ApplyStatus.CONFLICT
    assert result.applied_paths == () and result.journal_id is None
    assert tree(f.source) == before
    assert not f.staging.exists()
    assert f.uow.pending_apply_journals() == []


@pytest.mark.parametrize("index", range(3))
@pytest.mark.parametrize("event", ["before_mutation", "after_mutation"])
def test_every_mutation_failure_restores_complete_tree(tmp_path, event, index):
    f = fixture(tmp_path)
    before = tree(f.source)
    path = f.manifest.entries[index].path.value
    def fail(at, current=None):
        if (at, current) == (event, path):
            raise OSError("injected failure")
    f.service._boundary = fail
    result = apply(f)
    assert result.status is ApplyStatus.ROLLED_BACK
    assert result.applied_paths == ()
    assert tree(f.source) == before
    assert f.uow.get_apply_journal(result.journal_id)["state"] == "ROLLED_BACK"
    assert f.sessions.get(f.session_id).status is SessionStatus.DIRTY_VERIFIED


@pytest.mark.parametrize("event", ["before_reservation", "after_staging", "after_mutation"])
def test_cancellation_restores_source(tmp_path, event):
    f = fixture(tmp_path)
    before = tree(f.source)
    token = CancellationToken()
    f.service._boundary = lambda at, path=None: token.cancel() if at == event else None
    result = apply(f, token)
    assert result.status is (ApplyStatus.CONFLICT if event == "before_reservation" else ApplyStatus.ROLLED_BACK)
    assert tree(f.source) == before
    if event == "before_reservation":
        assert not f.staging.exists() and not f.uow.pending_apply_journals()


class Crash(BaseException):
    pass


@pytest.mark.parametrize("event", ["after_reservation", "after_stage_file", "after_staging", "after_applying",
    "before_mutation", "after_mutation", "before_commit"])
def test_restart_recovers_each_durable_state(tmp_path, event):
    f = fixture(tmp_path)
    before = tree(f.source)
    def crash(at, path=None):
        if at == event:
            raise Crash()
    f.service._boundary = crash
    with pytest.raises(Crash):
        apply(f)
    service = ApplyService(f.context, f.store, f.uow, f.source, f.workspace, f.staging)
    results = service.recover_pending()
    assert len(results) == 1 and results[0].status is ApplyStatus.ROLLED_BACK
    assert tree(f.source) == before
    assert not f.uow.pending_apply_journals()
    assert service.recover_pending() == ()


def test_unprovable_restore_preserves_backup_and_blocks_project(tmp_path):
    f = fixture(tmp_path)
    def fail(at, path=None):
        if at == "after_mutation":
            (f.source / path).write_bytes(b"third party content")
            raise OSError("failure")
    f.service._boundary = fail
    result = apply(f)
    assert result.status is ApplyStatus.RECOVERY_FAILED
    assert f.uow.get_apply_journal(result.journal_id)["state"] == "RECOVERY_FAILED"
    assert list(f.staging.rglob("*.before"))
    assert f.sessions.get(f.session_id).status is SessionStatus.DIRTY_VERIFIED


def test_concurrent_reservation_and_cross_project_isolation(tmp_path):
    f = fixture(tmp_path, name="one")
    other = fixture(tmp_path, name="two")
    f.uow.prepare_apply(session_id=f.session_id, manifest_id=f.manifest.manifest_id,
        session_version=f.session.version, staging_id="pending", backup_id="backup")
    f.session = f.sessions.get(f.session_id)
    before = tree(f.source)
    assert apply(f).status is ApplyStatus.CONFLICT
    assert tree(f.source) == before and not f.staging.exists()
    assert apply(other).status is ApplyStatus.COMMITTED
    assert len(f.uow.pending_apply_journals()) == 1
    assert not other.service.recover_pending()
