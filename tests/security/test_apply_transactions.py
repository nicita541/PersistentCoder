from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from app.apply.service import ApplyService, ApplyStatus, _Tree
from app.sandbox.limits import SandboxLimits
from app.tasks.unit_of_work import StateAuthorityError
from tests.apply.test_apply_service import Crash, apply, fixture, tree


def junction(link: Path, target: Path):
    if os.name == "nt":
        # Windows directory junctions do not require symbolic-link privilege.
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.mark.parametrize("where", ["source_root", "workspace_root", "source_parent", "workspace_parent", "staging"])
def test_root_and_parent_reparse_substitution_is_rejected(tmp_path, where):
    f = fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"never touch")
    if where.endswith("root"):
        root = f.source if where == "source_root" else f.workspace
        root.rename(root.with_name(root.name + "-moved"))
        junction(root, outside)
    elif where == "source_parent":
        junction(f.source / "new", outside)
    elif where == "workspace_parent":
        (f.workspace / "new").rename(f.base / "moved-parent")
        junction(f.workspace / "new", outside)
    else:
        junction(f.staging, outside)
    before = tree(outside)
    result = apply(f)
    assert result.status is ApplyStatus.CONFLICT
    assert result.journal_id is None and not f.uow.pending_apply_journals()
    assert tree(outside) == before


@pytest.mark.parametrize("root_name", ["source", "workspace"])
def test_leaf_symlink_is_never_followed(tmp_path, root_name):
    f = fixture(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"secret")
    leaf = getattr(f, root_name) / "b.py"
    leaf.unlink()
    try:
        leaf.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symbolic links unavailable: {error}")
    assert apply(f).status is ApplyStatus.CONFLICT
    assert outside.read_bytes() == b"secret"
    assert not f.staging.exists()


@pytest.mark.parametrize("root_name", ["source", "workspace"])
def test_plain_root_replacement_after_service_construction_conflicts(tmp_path, root_name):
    f = fixture(tmp_path)
    root = getattr(f, root_name)
    root.rename(root.with_name(root.name + "-original"))
    root.mkdir()
    assert apply(f).status is ApplyStatus.CONFLICT
    assert not f.staging.exists()


@pytest.mark.parametrize("root_name", ["source", "workspace"])
def test_mutation_during_preflight_has_no_durable_side_effect(tmp_path, root_name):
    f = fixture(tmp_path)
    def mutate(event, path=None):
        if event == "during_preflight":
            (getattr(f, root_name) / "b.py").write_bytes(b"racing mutation")
    f.service._boundary = mutate
    result = apply(f)
    assert result.status is ApplyStatus.CONFLICT
    assert not f.staging.exists() and not f.uow.pending_apply_journals()
    assert not (f.source / "new").exists()


def test_preflight_directory_handles_prevent_root_rename_on_windows(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows denies directory delete sharing")
    f = fixture(tmp_path)
    denied = []
    def mutate(event, path=None):
        if event == "during_preflight":
            with pytest.raises(PermissionError):
                f.source.rename(f.base / "stolen")
            denied.append(True)
    f.service._boundary = mutate
    assert apply(f).status is ApplyStatus.COMMITTED
    assert denied == [True]


def crash_at(f, event, *, path=None):
    def crash(at, current=None):
        if at == event and (path is None or path == current):
            raise Crash()
    f.service._boundary = crash
    with pytest.raises(Crash):
        apply(f)
    return f.uow.pending_apply_journals()[0]


@pytest.mark.parametrize("index", range(3))
@pytest.mark.parametrize("event", ["before_mutation", "after_mutation"])
def test_crash_before_and_after_every_operation_recovers(tmp_path, index, event):
    f = fixture(tmp_path)
    before = tree(f.source)
    crash_at(f, event, path=f.manifest.entries[index].path.value)
    restarted = ApplyService(f.context, f.store, f.uow, f.source, f.workspace, f.staging)
    assert restarted.recover_pending()[0].status is ApplyStatus.ROLLED_BACK
    assert tree(f.source) == before


@pytest.mark.parametrize("damage", ["plan", "backup", "intent", "root", "session", "manifest"])
def test_missing_or_corrupt_recovery_evidence_never_claims_rollback(tmp_path, damage):
    f = fixture(tmp_path)
    journal = crash_at(f, "after_mutation", path="b.py")
    backup = f.staging / journal["backup_id"]
    if damage in {"plan", "backup", "intent"}:
        name = {"plan": "plan.json", "backup": "0.before", "intent": "0.intent"}[damage]
        (backup / name).write_bytes(b"corrupted")
    elif damage == "root":
        import shutil
        f.source.rename(f.base / "original-source")
        shutil.copytree(f.base / "original-source", f.source)
    elif damage == "session":
        session = f.sessions.get(f.session_id)
        session.sandbox_session_id = "changed"
        f.sessions.update(session)
    else:
        with sqlite3.connect(f.context.database_path) as connection:
            connection.execute("DROP TRIGGER patch_manifest_entries_immutable_update")
            connection.execute("UPDATE patch_manifest_entries SET before_size = before_size + 1 WHERE before_size IS NOT NULL")
    restarted = ApplyService(f.context, f.store, f.uow, f.source, f.workspace, f.staging)
    assert restarted.recover_pending()[0].status is ApplyStatus.RECOVERY_FAILED
    assert f.uow.get_apply_journal(journal["id"])["state"] == "RECOVERY_FAILED"
    assert backup.exists()


def test_failed_rollback_is_resumable_without_consuming_backup(tmp_path):
    f = fixture(tmp_path)
    before = tree(f.source)
    failures = []
    def fail(event, path=None):
        if event == "after_mutation" and path == "z.py":
            raise OSError("promotion failed")
        if event == "after_rollback" and not failures:
            failures.append(True)
            raise OSError("rollback process failed")
    f.service._boundary = fail
    result = apply(f)
    assert result.status is ApplyStatus.RECOVERY_FAILED
    restarted = ApplyService(f.context, f.store, f.uow, f.source, f.workspace, f.staging)
    assert restarted.recover_pending()[0].status is ApplyStatus.ROLLED_BACK
    assert tree(f.source) == before


def test_unprovable_path_does_not_prevent_restoring_other_paths(tmp_path):
    f = fixture(tmp_path)
    def fail(event, path=None):
        if event == "after_mutation" and path == "z.py":
            (f.source / "z.py").write_bytes(b"external")
            raise OSError("failed")
    f.service._boundary = fail
    result = apply(f)
    assert result.status is ApplyStatus.RECOVERY_FAILED
    assert (f.source / "b.py").read_bytes() == b"before\n"
    assert not (f.source / f.addition).exists()
    assert (f.source / "z.py").read_bytes() == b"external"


def test_source_limits_are_checked_before_reservation(tmp_path):
    f = fixture(tmp_path)
    f.service.limits = replace(SandboxLimits(), max_snapshot_file_bytes=1)
    assert apply(f).status is ApplyStatus.CONFLICT
    assert not f.staging.exists() and not f.uow.pending_apply_journals()


def test_foreign_store_context_cannot_publish_or_recover(tmp_path):
    f = fixture(tmp_path, name="one")
    other = fixture(tmp_path, name="two")
    before = tree(f.source)
    f.service.store = other.store
    assert apply(f).status is ApplyStatus.CONFLICT
    assert tree(f.source) == before
    assert f.uow.get_apply_session(other.session_id, other.manifest.manifest_id) is None
    journal, _ = other.uow.prepare_apply(session_id=other.session_id, manifest_id=other.manifest.manifest_id,
        session_version=other.session.version, staging_id="staged", backup_id="backup")
    with pytest.raises(StateAuthorityError):
        f.uow.record_apply_recovery_failure(journal, "foreign")
    assert other.uow.get_apply_journal(journal)["state"] == "PREPARING"


def test_commit_exception_after_durable_commit_keeps_applied_source(tmp_path, monkeypatch):
    f = fixture(tmp_path)
    original = f.uow.transition_apply
    def raised_after_commit(*args, **kwargs):
        version = original(*args, **kwargs)
        if kwargs["target_state"] == "COMMITTED":
            raise OSError("lost acknowledgement")
        return version
    monkeypatch.setattr(f.uow, "transition_apply", raised_after_commit)
    assert apply(f).status is ApplyStatus.COMMITTED
    assert tree(f.source) == tree(f.workspace)


def test_reservation_exception_after_commit_is_not_reported_as_conflict(tmp_path, monkeypatch):
    f = fixture(tmp_path)
    original = f.uow.prepare_apply
    def raised_after_reservation(**kwargs):
        original(**kwargs)
        raise OSError("lost acknowledgement")
    monkeypatch.setattr(f.uow, "prepare_apply", raised_after_reservation)
    result = apply(f)
    assert result.status is ApplyStatus.ROLLED_BACK
    assert result.journal_id is not None
    assert not f.uow.pending_apply_journals()


def test_pending_apply_recovery_does_not_interrupt_live_apply(tmp_path):
    f = fixture(tmp_path)
    observed = []
    def recover(event, path=None):
        if event == "after_applying":
            competing = ApplyService(f.context, f.store, f.uow, f.source, f.workspace, f.staging)
            observed.extend(competing.recover_pending())
    f.service._boundary = recover
    assert apply(f).status is ApplyStatus.COMMITTED
    assert observed and all(item.status is ApplyStatus.CONFLICT for item in observed)


_CHILD = """
import os, sys
from pathlib import Path
from app.apply.service import ApplyService
from app.apply.store import PatchManifestStore
from app.tasks.store_context import StoreContext
from app.tasks.unit_of_work import RuntimeUnitOfWork
database, project, source, workspace, staging, manifest, session, version, event, path = sys.argv[1:]
context = StoreContext(Path(database), project, source)
service = ApplyService(context, PatchManifestStore(context), RuntimeUnitOfWork(context), source, workspace, staging)
def boundary(at, current=None):
    if event == 'rollback' and at == 'before_commit':
        raise OSError('rollback trigger')
    if (event == 'rollback' and at == 'after_rollback') or (at == event and (not path or current == path)):
        os._exit(77)
service._boundary = boundary
if event == 'recover':
    results = service.recover_pending()
    assert results and all(item.status.value == 'CONFLICT' for item in results)
else:
    service.apply(manifest, agent_session_id=session, expected_session_version=int(version))
"""


def child(f, event, path=""):
    return subprocess.run([sys.executable, "-c", _CHILD, str(f.context.database_path),
        f.context.project_id, str(f.source), str(f.workspace), str(f.staging),
        f.manifest.manifest_id, f.session_id, str(f.session.version), event, path],
        capture_output=True, timeout=30)


@pytest.mark.parametrize("event,path", [("after_reservation", ""), ("after_stage_file", ""),
    ("after_staging", ""), ("after_applying", ""), ("after_mutation", "b.py"),
    ("after_mutation", "new/deep/a.py"), ("after_mutation", "z.py"), ("before_commit", ""), ("rollback", "")])
def test_actual_process_exit_recovers_durable_files_without_exception_cleanup(tmp_path, event, path):
    f = fixture(tmp_path)
    before = tree(f.source)
    crashed = child(f, event, path)
    assert crashed.returncode == 77, crashed.stderr.decode(errors="replace")
    restarted = ApplyService(f.context, f.store, f.uow, f.source, f.workspace, f.staging)
    assert restarted.recover_pending()[0].status is ApplyStatus.ROLLED_BACK
    assert tree(f.source) == before


def test_live_apply_is_excluded_from_another_process_recovery(tmp_path):
    f = fixture(tmp_path)
    def compete(event, path=None):
        if event == "after_applying":
            result = child(f, "recover")
            assert result.returncode == 0, result.stderr.decode(errors="replace")
    f.service._boundary = compete
    assert apply(f).status is ApplyStatus.COMMITTED


def test_multiple_parent_trees_recover_using_stable_directory_ordinals(tmp_path):
    f = fixture(tmp_path, extra_additions=("a/one/two.py", "y/other.py", "unicode/тест😀.py"))
    before = tree(f.source)
    crash_at(f, "before_commit")
    restarted = ApplyService(f.context, f.store, f.uow, f.source, f.workspace, f.staging)
    assert restarted.recover_pending()[0].status is ApplyStatus.ROLLED_BACK
    assert tree(f.source) == before


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("method", ["replace_from", "unlink"])
def test_native_mutation_fault_does_not_leave_partial_source(tmp_path, monkeypatch, when, method):
    f = fixture(tmp_path)
    before = tree(f.source)
    original = getattr(_Tree, method)
    raised = []
    def fault(self, *args, **kwargs):
        if self.root == f.source and not raised:
            raised.append(True)
            if when == "before":
                raise OSError("native failure before mutation")
            original(self, *args, **kwargs)
            raise OSError("native failure after mutation")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(_Tree, method, fault)
    assert apply(f).status is ApplyStatus.ROLLED_BACK
    assert tree(f.source) == before


def test_changed_staged_content_is_rejected_inside_promotion(tmp_path, monkeypatch):
    f = fixture(tmp_path)
    before = tree(f.source)
    original = _Tree.replace_from
    attacked = []
    def attack(self, other, name, *args, **kwargs):
        if not attacked:
            attacked.append(True)
            (other.root / name).write_bytes(b"tampered")
        return original(self, other, name, *args, **kwargs)
    monkeypatch.setattr(_Tree, "replace_from", attack)
    assert apply(f).status is ApplyStatus.ROLLED_BACK
    assert tree(f.source) == before


def test_windows_verified_promotion_handle_blocks_leaf_substitution(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows handle-sharing invariant")
    import app.apply.service as module
    f = fixture(tmp_path)
    original = module._windows_set_information
    denied = []
    def attack(descriptor, kind, info):
        if kind == 3:
            journal = f.uow.pending_apply_journals()[0]
            staged = f.staging / journal["staging_id"]
            for path in staged.glob("*.after"):
                try:
                    with path.open("r+b"):
                        pass
                except PermissionError:
                    denied.append(True)
        return original(descriptor, kind, info)
    monkeypatch.setattr(module, "_windows_set_information", attack)
    assert apply(f).status is ApplyStatus.COMMITTED
    assert len(denied) == 2


def test_rollback_acknowledgement_loss_retains_verified_terminal_state(tmp_path, monkeypatch):
    f = fixture(tmp_path)
    before = tree(f.source)
    original = f.uow.transition_apply
    def fail(*args, **kwargs):
        version = original(*args, **kwargs)
        if kwargs["target_state"] == "ROLLED_BACK":
            raise OSError("lost rollback acknowledgement")
        return version
    monkeypatch.setattr(f.uow, "transition_apply", fail)
    f.service._boundary = lambda event, path=None: (_ for _ in ()).throw(OSError()) if event == "before_commit" else None
    assert apply(f).status is ApplyStatus.ROLLED_BACK
    assert tree(f.source) == before


@pytest.mark.parametrize("target", ["PREPARING", "APPLYING", "COMMITTED", "ROLLED_BACK"])
def test_durable_transition_failure_preserves_provable_outcome(tmp_path, monkeypatch, target):
    f = fixture(tmp_path)
    before = tree(f.source)
    original = f.uow.transition_apply
    def fail(*args, **kwargs):
        if kwargs["target_state"] == target:
            raise OSError("durability failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(f.uow, "transition_apply", fail)
    if target == "PREPARING":
        monkeypatch.setattr(f.uow, "prepare_apply", lambda **kw: (_ for _ in ()).throw(OSError()))
    if target == "ROLLED_BACK":
        f.service._boundary = lambda event, path=None: (_ for _ in ()).throw(OSError()) if event == "before_commit" else None
    result = apply(f)
    expected = {"PREPARING": ApplyStatus.CONFLICT, "APPLYING": ApplyStatus.ROLLED_BACK,
        "COMMITTED": ApplyStatus.ROLLED_BACK, "ROLLED_BACK": ApplyStatus.RECOVERY_FAILED}[target]
    assert result.status is expected
    assert tree(f.source) == before


def test_unreadable_journal_after_source_mutation_is_not_a_conflict(tmp_path, monkeypatch):
    f = fixture(tmp_path)
    def fail(event, path=None):
        if event == "after_mutation":
            monkeypatch.setattr(f.uow, "get_apply_journal", lambda *args: (_ for _ in ()).throw(OSError()))
            raise OSError("database disappeared")
    f.service._boundary = fail
    result = apply(f)
    assert result.status is ApplyStatus.RECOVERY_FAILED
    assert result.journal_id is not None and f.staging.exists()
    assert f.uow.pending_apply_journals()[0]["state"] == "APPLYING"


def test_corrupted_backup_is_detected_before_first_source_mutation(tmp_path):
    f = fixture(tmp_path)
    before = tree(f.source)
    events = []
    def corrupt(event, path=None):
        events.append(event)
        if event == "after_staging":
            journal = f.uow.pending_apply_journals()[0]
            (f.staging / journal["backup_id"] / "0.before").write_bytes(b"bad")
    f.service._boundary = corrupt
    assert apply(f).status is ApplyStatus.ROLLED_BACK
    assert "before_mutation" not in events
    assert tree(f.source) == before


def test_crash_after_directory_creation_without_receipt_is_unprovable(tmp_path, monkeypatch):
    f = fixture(tmp_path)
    original = _Tree.mkdir
    def crash(self, path):
        identity = original(self, path)
        if self.root == f.source:
            raise Crash()
        return identity
    monkeypatch.setattr(_Tree, "mkdir", crash)
    with pytest.raises(Crash):
        apply(f)
    restarted = ApplyService(f.context, f.store, f.uow, f.source, f.workspace, f.staging)
    assert restarted.recover_pending()[0].status is ApplyStatus.RECOVERY_FAILED
    assert (f.source / "new").is_dir()
    assert (f.source / "b.py").read_bytes() == b"before\n"


def test_readonly_preflight_session_lookup_does_not_change_versions(tmp_path):
    f = fixture(tmp_path)
    row = f.uow.get_apply_session(f.session_id, f.manifest.manifest_id)
    assert row["version"] == f.session.version
    assert f.sessions.get(f.session_id).version == f.session.version
    assert not f.uow.pending_apply_journals()


def test_recovery_uses_durable_backups_when_workspace_is_gone(tmp_path):
    f = fixture(tmp_path)
    before = tree(f.source)
    crash_at(f, "before_commit")
    f.workspace.rename(f.base / "unavailable-workspace")
    restarted = ApplyService(f.context, f.store, f.uow, f.source, f.workspace, f.staging)
    assert restarted.recover_pending()[0].status is ApplyStatus.ROLLED_BACK
    assert tree(f.source) == before


def test_partial_restore_copy_can_be_retried_without_growing_staging(tmp_path, monkeypatch):
    f = fixture(tmp_path)
    before = tree(f.source)
    crash_at(f, "before_commit")
    restarted = ApplyService(f.context, f.store, f.uow, f.source, f.workspace, f.staging)
    original = restarted._copy
    def partial(source, name, destination, target, *args, **kwargs):
        if target.endswith(".restore"):
            (destination.root / target).write_bytes(b"partial")
            raise OSError("restore copy failed")
        return original(source, name, destination, target, *args, **kwargs)
    monkeypatch.setattr(restarted, "_copy", partial)
    assert restarted.recover_pending()[0].status is ApplyStatus.RECOVERY_FAILED
    count = len(list(f.staging.rglob("*.restore")))
    assert restarted.recover_pending()[0].status is ApplyStatus.RECOVERY_FAILED
    assert len(list(f.staging.rglob("*.restore"))) == count
    monkeypatch.setattr(restarted, "_copy", original)
    assert restarted.recover_pending()[0].status is ApplyStatus.ROLLED_BACK
    assert tree(f.source) == before


def test_source_change_during_last_workspace_scan_cannot_reserve(tmp_path, monkeypatch):
    f = fixture(tmp_path)
    original = f.service._workspace
    calls = []
    def mutate_after_scan(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(True)
        if len(calls) == 2:
            (f.source / "b.py").write_bytes(b"external change")
        return result
    monkeypatch.setattr(f.service, "_workspace", mutate_after_scan)
    assert apply(f).status is ApplyStatus.CONFLICT
    assert not f.uow.pending_apply_journals() and not f.staging.exists()
