from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from app.apply.manifest import ManifestValidationError, PatchManifest
from app.tasks.migrations import migrate
from app.tasks.store_context import StoreContext


def read_manifest(
    connection: sqlite3.Connection, context: StoreContext, manifest_id: str
) -> PatchManifest | None:
    """Reconstruct from authoritative rows; domain validation recomputes the ID."""
    row = connection.execute(
        "SELECT * FROM patch_manifests WHERE manifest_id = ? "
        "AND project_id = ? AND canonical_source_root = ?",
        (manifest_id, context.project_id, context.canonical_source_root),
    ).fetchone()
    if row is None:
        return None
    rows = connection.execute(
        "SELECT * FROM patch_manifest_entries WHERE manifest_id = ? ORDER BY ordinal",
        (manifest_id,),
    ).fetchall()
    if len(rows) != row["entry_count"]:
        raise ManifestValidationError("manifest entry count does not match content")
    entries = []
    for ordinal, entry in enumerate(rows):
        if (
            entry["ordinal"] != ordinal
            or entry["project_id"] != row["project_id"]
            or entry["canonical_source_root"] != row["canonical_source_root"]
            or entry["agent_session_id"] != row["agent_session_id"]
        ):
            raise ManifestValidationError("manifest entry ownership or order is invalid")
        entries.append({
            key: entry[key] for key in (
                "path", "operation", "before_sha256", "after_sha256",
                "before_size", "after_size",
            )
        } | {
            "reviewable": bool(entry["reviewable"]),
            "apply_safe": bool(entry["apply_safe"]),
            "reasons": json.loads(entry["reasons_json"]),
        })
    manifest = PatchManifest.from_dict({
        key: row[key] for key in (
            "schema_version", "manifest_id", "project_id", "canonical_source_root",
            "session_id", "baseline_sha256", "workspace_sha256", "verification_id",
        )
    } | {"entries": entries})
    if any(entry.path.comparison_key != stored["path_key"] for entry, stored in zip(manifest.entries, rows)):
        raise ManifestValidationError("manifest path ownership key is invalid")
    return manifest


def insert_manifest(
    connection: sqlite3.Connection,
    context: StoreContext,
    manifest: PatchManifest,
    *,
    agent_session_id: str,
    allow_existing: bool = True,
) -> None:
    """Insert using the caller's transaction; never commit or open a connection."""
    if not connection.in_transaction:
        raise ValueError("manifest insertion requires a transaction")
    # Revalidate even values forged through object.__setattr__ or subclassing.
    manifest = PatchManifest.from_dict(manifest.to_dict())
    if (manifest.project_id, manifest.canonical_source_root) != (
        context.project_id, context.canonical_source_root
    ):
        raise ValueError("manifest project scope does not match store context")
    owner = connection.execute(
        "SELECT id FROM agent_sessions WHERE id = ? AND project_id = ? "
        "AND canonical_source_root = ? AND sandbox_session_id = ?",
        (agent_session_id, context.project_id, context.canonical_source_root, manifest.session_id),
    ).fetchone()
    if owner is None:
        raise ValueError("manifest session does not match owning sandbox session")
    existing = connection.execute(
        "SELECT agent_session_id FROM patch_manifests WHERE manifest_id = ?",
        (manifest.manifest_id,),
    ).fetchone()
    if existing is not None:
        stored = read_manifest(connection, context, manifest.manifest_id)
        if stored != manifest or existing["agent_session_id"] != agent_session_id:
            raise ValueError("manifest ID collision with different content or session")
        if not allow_existing:
            raise ValueError("manifest already saved outside the terminal transaction")
        return
    connection.execute(
        """INSERT INTO patch_manifests (
            manifest_id, schema_version, project_id, canonical_source_root,
            agent_session_id, session_id, baseline_sha256, workspace_sha256,
            verification_id, entry_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (manifest.manifest_id, manifest.SCHEMA_VERSION, manifest.project_id,
         manifest.canonical_source_root, agent_session_id, manifest.session_id,
         manifest.baseline_sha256, manifest.workspace_sha256,
         manifest.verification_id, len(manifest.entries)),
    )
    connection.executemany(
        """INSERT INTO patch_manifest_entries (
            manifest_id, project_id, canonical_source_root, agent_session_id,
            ordinal, path, path_key, operation, before_sha256, after_sha256,
            before_size, after_size, reviewable, apply_safe, reasons_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (manifest.manifest_id, manifest.project_id, manifest.canonical_source_root,
             agent_session_id, ordinal, entry.path.value, entry.path.comparison_key,
             entry.operation.value, entry.before_sha256, entry.after_sha256,
             entry.before_size, entry.after_size, entry.reviewable, entry.apply_safe,
             json.dumps(entry.reasons, ensure_ascii=False, separators=(",", ":")))
            for ordinal, entry in enumerate(manifest.entries)
        ],
    )


class PatchManifestStore:
    """Immutable project-scoped storage. Runtime publication belongs to the UoW."""

    def __init__(self, context: StoreContext) -> None:
        if not isinstance(context, StoreContext):
            raise TypeError("PatchManifestStore requires a StoreContext")
        self.context = context
        migrate(context.database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.context.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def save(self, manifest: PatchManifest, *, agent_session_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            insert_manifest(connection, self.context, manifest, agent_session_id=agent_session_id)

    def get(self, manifest_id: str) -> PatchManifest | None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN")
            return read_manifest(connection, self.context, manifest_id)
