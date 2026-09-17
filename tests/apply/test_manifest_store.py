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


def _row_dict(connection, table, where="", parameters=()):
    columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
    row = connection.execute(f"SELECT * FROM {table} {where}", parameters).fetchone()
    assert row is not None
    return dict(zip(columns, row))


def _insert_row(connection, table, row, *, command="INSERT"):
    placeholders = ", ".join("?" for _ in row)
    connection.execute(
        f"{command} INTO {table} ({', '.join(row)}) VALUES ({placeholders})",
        list(row.values()),
    )


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


@pytest.mark.parametrize(
    "constraint",
    ["duplicate_ordinal", "duplicate_path", "foreign_owner", "case_path"],
)
def test_entry_constraints_reject_one_invalid_relationship(tmp_path, constraint):
    context, _, session_id, manifest = manifest_fixture(tmp_path)
    store_for(context).save(manifest, agent_session_id=session_id)
    with sqlite3.connect(context.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        original_manifest = _row_dict(connection, "patch_manifests")
        scratch_manifest_id = "1" * 64
        _insert_row(
            connection,
            "patch_manifests",
            original_manifest | {
                "manifest_id": scratch_manifest_id,
                "entry_count": 4,
            },
        )
        original_entry = _row_dict(
            connection,
            "patch_manifest_entries",
            "WHERE manifest_id = ? AND ordinal = 0",
            (manifest.manifest_id,),
        )
        base_entry = original_entry | {
            "manifest_id": scratch_manifest_id,
            "ordinal": 0,
        }
        _insert_row(connection, "patch_manifest_entries", base_entry)
        candidate = base_entry | {
            "ordinal": 1,
            "path": "fresh.py",
            "path_key": "fresh.py",
        }
        changes = {
            "duplicate_ordinal": {"ordinal": 0},
            "duplicate_path": {"path": "a.py", "path_key": "unique-key"},
            "foreign_owner": {"project_id": "0" * 64},
            "case_path": {"path": "A.py", "path_key": "a.py"},
        }[constraint]
        with pytest.raises(sqlite3.IntegrityError):
            _insert_row(connection, "patch_manifest_entries", candidate | changes)


@pytest.mark.parametrize("table", ["patch_manifests", "patch_manifest_entries"])
def test_insert_or_replace_cannot_change_immutable_rows(tmp_path, table):
    context, _, session_id, manifest = manifest_fixture(tmp_path)
    store_for(context).save(manifest, agent_session_id=session_id)
    with sqlite3.connect(context.database_path) as connection:
        if table == "patch_manifests":
            original = _row_dict(
                connection,
                table,
                "WHERE manifest_id = ?",
                (manifest.manifest_id,),
            )
            replacement = original | {"workspace_sha256": "9" * 64}
            where = "WHERE manifest_id = ?"
            parameters = (manifest.manifest_id,)
        else:
            original = _row_dict(
                connection,
                table,
                "WHERE manifest_id = ? AND ordinal = 0",
                (manifest.manifest_id,),
            )
            replacement = original | {"after_size": 999}
            where = "WHERE manifest_id = ? AND ordinal = 0"
            parameters = (manifest.manifest_id,)

        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            _insert_row(
                connection,
                table,
                replacement,
                command="INSERT OR REPLACE",
            )

        assert _row_dict(connection, table, where, parameters) == original


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
