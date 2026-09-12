from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.tasks.store import (
    DEFAULT_DATABASE_PATH,
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
        database_path: Path | None = None,
    ) -> None:
        self.database_path = (
            Path(database_path)
            if database_path is not None
            else DEFAULT_DATABASE_PATH
        )

        self.database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._initialize_database()

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

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_agent_events_run
                ON agent_events(run_id)
                """
            )

    def start_run(
        self,
        request: str,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO agent_runs (
                    request,
                    status,
                    phase
                )
                VALUES (?, ?, ?)
                """,
                (request, RUNNING, "PLANNING"),
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

        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE agent_runs
                SET
                    {assignments},
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
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
            connection.execute(
                """
                UPDATE agent_runs
                SET
                    status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, run_id),
            )

    # ==================================
    # CRASH RECOVERY
    # ==================================

    def get_run(
        self,
        run_id: int,
    ) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE id = ?",
                (run_id,),
            ).fetchone()

        return dict(row) if row is not None else None

    def get_running_runs(
        self,
    ) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_runs
                WHERE status = ?
                ORDER BY id ASC
                """,
                (RUNNING,),
            ).fetchall()

        return [dict(row) for row in rows]

    def mark_interrupted(
        self,
        run_id: int,
        note: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_runs
                SET
                    status = ?,
                    recovery_note = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = ?
                """,
                (INTERRUPTED, note, run_id, RUNNING),
            )

    def set_recovery_note(
        self,
        run_id: int,
        note: str,
    ) -> None:
        """Record the outcome of crash recovery for a run."""

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_runs
                SET
                    recovery_note = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (note, run_id),
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
        serialized = json.dumps(
            payload or {},
            ensure_ascii=False,
        )[:2000]

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO agent_events (
                    run_id,
                    event_type,
                    plan_id,
                    task_id,
                    step_id,
                    attempt_id,
                    payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
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
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_events
                WHERE run_id = ?
                ORDER BY id ASC
                """,
                (run_id,),
            ).fetchall()

        return [dict(row) for row in rows]

    def count_events(
        self,
        run_id: int,
    ) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM agent_events
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()

        return int(row["count"]) if row is not None else 0

