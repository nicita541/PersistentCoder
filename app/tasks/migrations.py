from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path


Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def _state_snapshots(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE agent_state_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            project_id TEXT NOT NULL,
            canonical_source_root TEXT NOT NULL,
            phase TEXT NOT NULL,
            plan_id INTEGER,
            task_id INTEGER,
            step_id INTEGER,
            attempt_id INTEGER,
            checkpoint_id TEXT,
            completion TEXT,
            state_json TEXT NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_state_snapshots_project_run
        ON agent_state_snapshots(project_id, canonical_source_root, run_id, id)
        """
    )


def _budget_ledger(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE budget_ledger (
            run_id INTEGER NOT NULL,
            project_id TEXT NOT NULL,
            canonical_source_root TEXT NOT NULL,
            scope TEXT NOT NULL,
            target_id INTEGER NOT NULL,
            consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed >= 0),
            limit_value INTEGER NOT NULL CHECK (limit_value > 0),
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (run_id, scope, target_id),
            FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
            CHECK (scope IN ('TASK', 'STEP'))
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_budget_ledger_project_run
        ON budget_ledger(project_id, canonical_source_root, run_id)
        """
    )


def _file_journal(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE file_operation_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            project_id TEXT NOT NULL,
            canonical_source_root TEXT NOT NULL,
            attempt_id INTEGER NOT NULL UNIQUE,
            sandbox_session_id TEXT NOT NULL,
            checkpoint_id TEXT NOT NULL,
            state TEXT NOT NULL,
            error TEXT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
            FOREIGN KEY (attempt_id) REFERENCES attempts(id) ON DELETE CASCADE,
            CHECK (state IN (
                'PREPARING', 'READY',
                'FINALIZING_COMMIT', 'FINALIZING_ROLLBACK',
                'COMMITTED', 'ROLLED_BACK', 'RECOVERY_FAILED'
            ))
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_file_journal_project_state
        ON file_operation_journal(project_id, canonical_source_root, state, id)
        """
    )


def _verification_context(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            step_id INTEGER,
            status TEXT NOT NULL,
            reason TEXT,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            context_json TEXT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (
                (task_id IS NOT NULL AND step_id IS NULL)
                OR (task_id IS NULL AND step_id IS NOT NULL)
            ),
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (step_id) REFERENCES steps(id) ON DELETE CASCADE
        )
        """
    )
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(verifications)").fetchall()
    }
    if "context_json" not in columns:
        connection.execute("ALTER TABLE verifications ADD COLUMN context_json TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_verifications_task "
        "ON verifications(task_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_verifications_step "
        "ON verifications(step_id)"
    )


def _patch_manifests(connection: sqlite3.Connection) -> None:
    # Sessions predate ordered migrations; retain their exact legacy layout.
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_sessions (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            canonical_source_root TEXT NOT NULL, sandbox_session_id TEXT NOT NULL,
            status TEXT NOT NULL, active_run_id INTEGER, patch_manifest_id TEXT,
            version INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    connection.execute("""
        CREATE UNIQUE INDEX idx_agent_sessions_manifest_owner
        ON agent_sessions(id, project_id, canonical_source_root)
    """)
    connection.execute("""
        CREATE TABLE patch_manifests (
            manifest_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL CHECK (schema_version = 1),
            project_id TEXT NOT NULL, canonical_source_root TEXT NOT NULL,
            agent_session_id TEXT NOT NULL, session_id TEXT NOT NULL,
            baseline_sha256 TEXT NOT NULL, workspace_sha256 TEXT NOT NULL,
            verification_id TEXT NOT NULL,
            entry_count INTEGER NOT NULL CHECK (entry_count >= 0),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (manifest_id, project_id, canonical_source_root, agent_session_id),
            FOREIGN KEY (agent_session_id, project_id, canonical_source_root)
                REFERENCES agent_sessions(id, project_id, canonical_source_root)
        )
    """)
    connection.execute("""
        CREATE TRIGGER patch_manifest_owner_insert BEFORE INSERT ON patch_manifests
        WHEN NOT EXISTS (
            SELECT 1 FROM agent_sessions WHERE id = NEW.agent_session_id
            AND project_id = NEW.project_id
            AND canonical_source_root = NEW.canonical_source_root
            AND sandbox_session_id = NEW.session_id
        )
        BEGIN SELECT RAISE(ABORT, 'manifest session ownership mismatch'); END
    """)
    connection.execute("""
        CREATE TABLE patch_manifest_entries (
            manifest_id TEXT NOT NULL, project_id TEXT NOT NULL,
            canonical_source_root TEXT NOT NULL, agent_session_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
            path TEXT NOT NULL, path_key TEXT NOT NULL,
            operation TEXT NOT NULL CHECK (operation IN ('ADD', 'MODIFY', 'DELETE')),
            before_sha256 TEXT, after_sha256 TEXT,
            before_size INTEGER CHECK (before_size >= 0),
            after_size INTEGER CHECK (after_size >= 0),
            reviewable INTEGER NOT NULL CHECK (reviewable IN (0, 1)),
            apply_safe INTEGER NOT NULL CHECK (apply_safe IN (0, 1)),
            reasons_json TEXT NOT NULL,
            PRIMARY KEY (manifest_id, ordinal),
            UNIQUE (manifest_id, path), UNIQUE (manifest_id, path_key),
            CHECK (apply_safe <= reviewable),
            FOREIGN KEY (manifest_id, project_id, canonical_source_root, agent_session_id)
                REFERENCES patch_manifests(manifest_id, project_id, canonical_source_root, agent_session_id)
        )
    """)
    connection.execute("""
        CREATE TRIGGER patch_manifest_entry_ordinal BEFORE INSERT ON patch_manifest_entries
        WHEN NEW.ordinal >= (SELECT entry_count FROM patch_manifests WHERE manifest_id = NEW.manifest_id)
        BEGIN SELECT RAISE(ABORT, 'manifest ordinal outside declared entries'); END
    """)
    for table in ("patch_manifests", "patch_manifest_entries"):
        for action in ("UPDATE", "DELETE"):
            connection.execute(f"""
                CREATE TRIGGER {table}_immutable_{action.lower()}
                BEFORE {action} ON {table}
                BEGIN SELECT RAISE(ABORT, 'manifest is immutable'); END
            """)


def _apply_journal(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE apply_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL, canonical_source_root TEXT NOT NULL,
            agent_session_id TEXT NOT NULL, manifest_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN (
                'PREPARING', 'APPLYING', 'COMMITTED', 'ROLLED_BACK',
                'CONFLICT', 'RECOVERY_FAILED'
            )),
            staging_id TEXT NOT NULL CHECK (
                length(staging_id) BETWEEN 1 AND 128
                AND staging_id NOT GLOB '*[^a-zA-Z0-9_-]*'
            ),
            backup_id TEXT NOT NULL CHECK (
                length(backup_id) BETWEEN 1 AND 128
                AND backup_id NOT GLOB '*[^a-zA-Z0-9_-]*'
            ),
            error TEXT CHECK (length(error) <= 2000),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (manifest_id, project_id, canonical_source_root, agent_session_id)
                REFERENCES patch_manifests(manifest_id, project_id, canonical_source_root, agent_session_id)
        )
    """)
    connection.execute("""
        CREATE UNIQUE INDEX idx_apply_journal_active_project
        ON apply_journal(project_id, canonical_source_root)
        WHERE state IN ('PREPARING', 'APPLYING', 'RECOVERY_FAILED')
    """)


MIGRATIONS: tuple[Migration, ...] = (
    (1, "agent state snapshots", _state_snapshots),
    (2, "repair budget ledger", _budget_ledger),
    (3, "file operation journal", _file_journal),
    (4, "revision-bound verification context", _verification_context),
    (5, "immutable patch manifests", _patch_manifests),
    (6, "transactional apply journal", _apply_journal),
)


def migrate(database_path: str | Path, *, timeout: float = 5.0) -> None:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        applied = {
            int(row[0])
            for row in connection.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }
        for version, name, operation in MIGRATIONS:
            if version in applied:
                continue
            operation(connection)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (version, name),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
