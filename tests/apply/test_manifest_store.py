from __future__ import annotations

import sqlite3

import pytest

from app.apply.manifest import ManifestValidationError, PatchEntry, PatchManifest, PatchOperation
from app.project_identity import ProjectIdentity
from app.tasks.session_store import SessionStore
from app.tasks.store_context import StoreContext
from app.tasks.unit_of_work import RuntimeUnitOfWork


def manifest_fixture(tmp_path, name="owner"):
    root = tmp_path / name
    root.mkdir()
    identity = ProjectIdentity.from_source_root(root)
    context = StoreContext(tmp_path / "store.db", identity.project_id, str(identity.canonical_source_root))
    sessions = SessionStore(context)
    session_id = sessions.create(sandbox_session_id="sandbox-1")
    manifest = PatchManifest.create(
        project_id=context.project_id, canonical_source_root=context.canonical_source_root,
        session_id="sandbox-1", baseline_sha256="a" * 64,
        workspace_sha256="b" * 64, verification_id="verified-1",
        entries=[
            PatchEntry("a.py", PatchOperation.ADD, None, "c" * 64, None, 7, True, True, ()),
            PatchEntry("b.bin", PatchOperation.MODIFY, "d" * 64, "e" * 64, 2, 3, False, False, ("binary",)),
            PatchEntry("z.py", PatchOperation.DELETE, "f" * 64, None, 8, None, True, True, ()),
        ],
    )
    return context, sessions, session_id, manifest


def store_for(context):
    from app.apply.store import PatchManifestStore
    return PatchManifestStore(context)


def test_manifest_store_round_trip_is_ordered_and_idempotent(tmp_path):
    context, _, session_id, manifest = manifest_fixture(tmp_path)
    store = store_for(context)
    store.save(manifest, agent_session_id=session_id)
    store.save(manifest, agent_session_id=session_id)
    assert store.get(manifest.manifest_id) == manifest
    with sqlite3.connect(context.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM patch_manifests").fetchone() == (1,)
        assert connection.execute("SELECT ordinal, path FROM patch_manifest_entries ORDER BY ordinal").fetchall() == [(0, "a.py"), (1, "b.bin"), (2, "z.py")]


def test_manifest_store_refuses_foreign_context_and_session(tmp_path):
    context, _, session_id, manifest = manifest_fixture(tmp_path)
    other, _, other_id, _ = manifest_fixture(tmp_path, "foreign")
    store = store_for(context)
    store.save(manifest, agent_session_id=session_id)
    assert store_for(other).get(manifest.manifest_id) is None
    with pytest.raises(ValueError, match="project|scope"):
        store_for(other).save(manifest, agent_session_id=other_id)
    with pytest.raises(ValueError, match="session"):
        store.save(manifest, agent_session_id=other_id)


def test_manifest_store_requires_matching_sandbox(tmp_path):
    context, sessions, _, manifest = manifest_fixture(tmp_path)
    wrong_id = sessions.create(sandbox_session_id="wrong-sandbox")
    with pytest.raises(ValueError, match="session"):
        store_for(context).save(manifest, agent_session_id=wrong_id)


@pytest.mark.parametrize("table", ["patch_manifests", "patch_manifest_entries"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_manifest_rows_are_immutable(tmp_path, table, operation):
    context, _, session_id, manifest = manifest_fixture(tmp_path)
    store_for(context).save(manifest, agent_session_id=session_id)
    with sqlite3.connect(context.database_path) as connection:
        sql = f"DELETE FROM {table}" if operation == "DELETE" else f"UPDATE {table} SET project_id = project_id"
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(sql)


def test_entry_constraints_reject_duplicate_ordinals_paths_and_foreign_owner(tmp_path):
    context, _, session_id, manifest = manifest_fixture(tmp_path)
    store_for(context).save(manifest, agent_session_id=session_id)
    with sqlite3.connect(context.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        columns = [row[1] for row in connection.execute("PRAGMA table_info(patch_manifest_entries)")]
        original = dict(zip(columns, connection.execute("SELECT * FROM patch_manifest_entries LIMIT 1").fetchone()))
        for changes in ({}, {"ordinal": 1}, {"project_id": "0" * 64}, {"path": "A.py", "ordinal": 2}):
            row = original | changes
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"INSERT INTO patch_manifest_entries ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", list(row.values()))


def test_manifest_read_recomputes_identity_and_save_detects_collision(tmp_path):
    context, _, session_id, manifest = manifest_fixture(tmp_path)
    store = store_for(context)
    store.save(manifest, agent_session_id=session_id)
    with sqlite3.connect(context.database_path) as connection:
        connection.execute("DROP TRIGGER patch_manifest_entries_immutable_update")
        connection.execute("UPDATE patch_manifest_entries SET after_size = 999 WHERE ordinal = 0")
    with pytest.raises(ManifestValidationError, match="identity"):
        store.get(manifest.manifest_id)
    with pytest.raises(ValueError, match="collision|content|identity"):
        store.save(manifest, agent_session_id=session_id)


def test_migration_preserves_legacy_sessions(tmp_path):
    context, sessions, session_id, _ = manifest_fixture(tmp_path)
    session = sessions.get(session_id)
    session.patch_manifest_id = "legacy-id"
    sessions.update(session)
    RuntimeUnitOfWork(context)
    RuntimeUnitOfWork(context)
    assert sessions.get(session_id).patch_manifest_id == "legacy-id"
    with sqlite3.connect(context.database_path) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"patch_manifests", "patch_manifest_entries", "apply_journal"} <= tables
