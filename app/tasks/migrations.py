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


MIGRATIONS: tuple[Migration, ...] = (
    (1, "agent state snapshots", _state_snapshots),
    (2, "repair budget ledger", _budget_ledger),
    (3, "file operation journal", _file_journal),
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
