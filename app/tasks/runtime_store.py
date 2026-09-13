from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.tasks.store import (
    DEFAULT_DATABASE_PATH,
)
from app.tasks.store_context import (
    StoreContext,
    split_store_binding,
)


RUNNING = "RUNNING"
DONE = "DONE"
FAILED = "FAILED"
INTERRUPTED = "INTERRUPTED"

TERMINAL_RUN_STATUSES = frozenset(
    {DONE, FAILED, INTERRUPTED}
)


class RuntimeStore:
    """
    Durable execution state + event log.

    SQLite is the source of truth for:

      - which run/plan/task/step/attempt/phase was active;
      - whether a run was interrupted by a crash;
      - a compact, append-only event log.

    It never stores full model prompts or file contents.
    """

    def __init__(
        self,
        database_path: StoreContext | str | Path | None = None,
    ) -> None:
        self.database_path, self.context = split_store_binding(
            database_path,
            default_database_path=DEFAULT_DATABASE_PATH,
        )

        self.database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._initialize_database()
        self._migrate_project_binding()
        self._initialize_project_indexes()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path
        )

        connection.row_factory = sqlite3.Row

        connection.execute(
            "PRAGMA foreign_keys = ON"
        )

        return connection

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id INTEGER
                        PRIMARY KEY AUTOINCREMENT,

                    request TEXT NOT NULL,

                    project_id TEXT,
                    canonical_source_root TEXT,

                    status TEXT
                        NOT NULL
                        DEFAULT 'RUNNING',

                    phase TEXT,

                    plan_id INTEGER,
                    task_id INTEGER,
                    step_id INTEGER,
                    attempt_id INTEGER,

                    sandbox_session_id TEXT,
                    checkpoint_id TEXT,

                    recovery_note TEXT,

                    created_at DATETIME
                        NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,

                    updated_at DATETIME
                        NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_events (
                    id INTEGER
                        PRIMARY KEY AUTOINCREMENT,

                    run_id INTEGER NOT NULL,

                    project_id TEXT,

                    event_type TEXT NOT NULL,

                    plan_id INTEGER,
                    task_id INTEGER,
                    step_id INTEGER,
                    attempt_id INTEGER,

                    payload TEXT
                        NOT NULL
                        DEFAULT '{}',

                    created_at DATETIME
                        NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def _migrate_project_binding(self) -> None:
        with self._connect() as connection:
            run_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(agent_runs)"
                ).fetchall()
            }
            event_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(agent_events)"
                ).fetchall()
            }

            if "project_id" not in run_columns:
                connection.execute(
                    "ALTER TABLE agent_runs ADD COLUMN project_id TEXT"
                )
            if "canonical_source_root" not in run_columns:
                connection.execute(
                    "ALTER TABLE agent_runs "
                    "ADD COLUMN canonical_source_root TEXT"
                )
            if "project_id" not in event_columns:
                connection.execute(
                    "ALTER TABLE agent_events ADD COLUMN project_id TEXT"
                )

    def _initialize_project_indexes(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_runs_project_status
                ON agent_runs(project_id, status)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_events_run
                ON agent_events(run_id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_events_project_run
                ON agent_events(project_id, run_id)
                """
            )

    def _run_scope(
        self,
    ) -> tuple[str, tuple[object, ...]]:
        if self.context is None:
            return "project_id IS NULL", ()

        return (
            "project_id = ? AND canonical_source_root = ?",
            (
                self.context.project_id,
                self.context.canonical_source_root,
            ),
        )

    def _event_scope(
        self,
    ) -> tuple[str, tuple[object, ...]]:
        if self.context is None:
            return "project_id IS NULL", ()

        return "project_id = ?", (self.context.project_id,)

    def start_run(
        self,
        request: str,
        *,
        sandbox_session_id: str | None = None,
    ) -> int:
        project_id = (
            self.context.project_id
            if self.context is not None
            else None
        )
        source_root = (
            self.context.canonical_source_root
            if self.context is not None
            else None
        )

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO agent_runs (
                    request,
                    project_id,
                    canonical_source_root,
                    status,
                    phase,
                    sandbox_session_id
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    request,
                    project_id,
                    source_root,
                    RUNNING,
                    "PLANNING",
                    sandbox_session_id,
                ),
            )

            return int(cursor.lastrowid)

    def update_run(
        self,
        run_id: int,
        *,
        phase: str | None = None,
        plan_id: int | None = None,
        task_id: int | None = None,
        step_id: int | None = None,
        attempt_id: int | None = None,
        sandbox_session_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> None:
        fields: dict[str, object] = {}

        for name, value in (
            ("phase", phase),
            ("plan_id", plan_id),
            ("task_id", task_id),
            ("step_id", step_id),
            ("attempt_id", attempt_id),
            ("sandbox_session_id", sandbox_session_id),
            ("checkpoint_id", checkpoint_id),
        ):
            if value is not None:
                fields[name] = value

        if not fields:
            return

        assignments = ", ".join(
            f"{name} = ?" for name in fields
        )

        parameters = list(fields.values())
        parameters.append(run_id)
        scope_sql, scope_parameters = self._run_scope()
        parameters.extend(scope_parameters)

        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE agent_runs
                SET
                    {assignments},
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND {scope_sql}
                """,
                parameters,
            )

    def finish_run(
        self,
        run_id: int,
        status: str,
    ) -> None:
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(
                f"invalid terminal status: {status}"
            )

        with self._connect() as connection:
            scope_sql, scope_parameters = self._run_scope()
            connection.execute(
                f"""
                UPDATE agent_runs
                SET
                    status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND {scope_sql}
                """,
                (status, run_id, *scope_parameters),
            )

    # ==================================
    # CRASH RECOVERY
    # ==================================

    def get_run(
        self,
        run_id: int,
    ) -> dict[str, object] | None:
        scope_sql, scope_parameters = self._run_scope()
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM agent_runs "
                f"WHERE id = ? AND {scope_sql}",
                (run_id, *scope_parameters),
            ).fetchone()

        return dict(row) if row is not None else None

    def get_running_runs(
        self,
    ) -> list[dict[str, object]]:
        scope_sql, scope_parameters = self._run_scope()
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM agent_runs
                WHERE status = ? AND {scope_sql}
                ORDER BY id ASC
                """,
                (RUNNING, *scope_parameters),
            ).fetchall()

        return [dict(row) for row in rows]

    def mark_interrupted(
        self,
        run_id: int,
        note: str,
    ) -> None:
        scope_sql, scope_parameters = self._run_scope()
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE agent_runs
                SET
                    status = ?,
                    recovery_note = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = ? AND {scope_sql}
                """,
                (
                    INTERRUPTED,
                    note,
                    run_id,
                    RUNNING,
                    *scope_parameters,
                ),
            )

    def set_recovery_note(
        self,
        run_id: int,
        note: str,
    ) -> None:
        """Record the outcome of crash recovery for a run."""

        scope_sql, scope_parameters = self._run_scope()
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE agent_runs
                SET
                    recovery_note = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND {scope_sql}
                """,
                (note, run_id, *scope_parameters),
            )

    def recover_interrupted(
        self,
    ) -> list[dict[str, object]]:
        """
        Detect runs that were RUNNING when the process died.

        They are marked INTERRUPTED, never silently DONE. The caller
        must not auto-continue a potentially half-written attempt.
        """

        interrupted: list[dict[str, object]] = []

        for run in self.get_running_runs():
            self.mark_interrupted(
                int(run["id"]),
                "run interrupted (process restart); "
                "not auto-continued",
            )

            updated = self.get_run(int(run["id"]))

            if updated is not None:
                interrupted.append(updated)

        return interrupted

    # ==================================
    # DURABLE EVENT LOG
    # ==================================

    def log_event(
        self,
        run_id: int,
        event_type: str,
        *,
        plan_id: int | None = None,
        task_id: int | None = None,
        step_id: int | None = None,
        attempt_id: int | None = None,
        payload: dict[str, object] | None = None,
    ) -> int:
        if self.get_run(run_id) is None:
            raise ValueError(f"Unknown run for current project: {run_id}")

        serialized = json.dumps(
            payload or {},
            ensure_ascii=False,
        )[:2000]

        with self._connect() as connection:
            project_id = (
                self.context.project_id
                if self.context is not None
                else None
            )
            cursor = connection.execute(
                """
                INSERT INTO agent_events (
                    run_id,
                    project_id,
                    event_type,
                    plan_id,
                    task_id,
                    step_id,
                    attempt_id,
                    payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    project_id,
                    event_type,
                    plan_id,
                    task_id,
                    step_id,
                    attempt_id,
                    serialized,
                ),
            )

            return int(cursor.lastrowid)

    def get_events(
        self,
        run_id: int,
    ) -> list[dict[str, object]]:
        scope_sql, scope_parameters = self._event_scope()
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM agent_events
                WHERE run_id = ? AND {scope_sql}
                ORDER BY id ASC
                """,
                (run_id, *scope_parameters),
            ).fetchall()

        return [dict(row) for row in rows]

    def event_timeline(
        self,
        run_id: int,
    ) -> list[dict[str, object]]:
        """
        Per-stage durations for one run.

        Uses the millisecond timestamp the runtime writes into each
        event payload, falling back to the second-resolution
        created_at column. This is how a slow stage is located
        without guessing.
        """

        timeline: list[dict[str, object]] = []

        previous_ms: int | None = None

        for row in self.get_events(run_id):
            payload = row.get("payload")

            stamp: int | None = None

            if isinstance(payload, str):
                try:
                    data = json.loads(payload)

                except ValueError:
                    data = {}

                candidate = data.get("ts_ms")

                if isinstance(candidate, int):
                    stamp = candidate

            delta = (
                None
                if (
                    stamp is None
                    or previous_ms is None
                )
                else stamp - previous_ms
            )

            timeline.append(
                {
                    "event": row.get("event_type"),
                    "at": row.get("created_at"),
                    "ts_ms": stamp,
                    "since_previous_ms": delta,
                    "task_id": row.get("task_id"),
                    "step_id": row.get("step_id"),
                    "attempt_id": row.get(
                        "attempt_id"
                    ),
                }
            )

            if stamp is not None:
                previous_ms = stamp

        return timeline

    def count_events(
        self,
        run_id: int,
    ) -> int:
        scope_sql, scope_parameters = self._event_scope()
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM agent_events
                WHERE run_id = ? AND {scope_sql}
                """,
                (run_id, *scope_parameters),
            ).fetchone()

        return int(row["count"]) if row is not None else 0

