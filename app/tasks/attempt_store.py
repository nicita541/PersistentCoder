from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.tasks.models import (
    AttemptRecord,
    AttemptStatus,
    AttemptTargetType,
)
from app.tasks.store import (
    DEFAULT_DATABASE_PATH,
)


class AttemptStoreError(
    RuntimeError
):
    pass


class AttemptStore:
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

    def _connect(
        self,
    ) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path
        )

        connection.row_factory = (
            sqlite3.Row
        )

        connection.execute(
            "PRAGMA foreign_keys = ON"
        )

        return connection

    def _initialize_database(
        self,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER
                        PRIMARY KEY AUTOINCREMENT,

                    task_id INTEGER,

                    step_id INTEGER,

                    attempt_number INTEGER
                        NOT NULL,

                    status TEXT
                        NOT NULL,

                    approach TEXT,

                    failure_reason TEXT,

                    result_summary TEXT,

                    result_artifacts_json TEXT
                        NOT NULL
                        DEFAULT '[]',

                    created_at DATETIME
                        NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,

                    started_at DATETIME
                        NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,

                    finished_at DATETIME,

                    updated_at DATETIME
                        NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,

                    CHECK (
                        (
                            task_id IS NOT NULL
                            AND step_id IS NULL
                        )
                        OR
                        (
                            task_id IS NULL
                            AND step_id IS NOT NULL
                        )
                    ),

                    FOREIGN KEY (task_id)
                        REFERENCES tasks(id)
                        ON DELETE CASCADE,

                    FOREIGN KEY (step_id)
                        REFERENCES steps(id)
                        ON DELETE CASCADE
                )
                """
            )

            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_attempt_task_number

                ON attempts(
                    task_id,
                    attempt_number
                )

                WHERE task_id IS NOT NULL
                """
            )

            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_attempt_step_number

                ON attempts(
                    step_id,
                    attempt_number
                )

                WHERE step_id IS NOT NULL
                """
            )

            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_one_active_task_attempt

                ON attempts(task_id)

                WHERE
                    task_id IS NOT NULL
                    AND status = 'IN_PROGRESS'
                """
            )

            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_one_active_step_attempt

                ON attempts(step_id)

                WHERE
                    step_id IS NOT NULL
                    AND status = 'IN_PROGRESS'
                """
            )

    @staticmethod
    def _dump_list(
        value: list[str],
    ) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
        )

    @staticmethod
    def _load_list(
        value: str | None,
    ) -> list[str]:
        if not value:
            return []

        data = json.loads(
            value
        )

        if not isinstance(
            data,
            list,
        ):
            raise AttemptStoreError(
                "invalid artifact list"
            )

        return [
            str(item)
            for item in data
        ]

    def _row_to_attempt(
        self,
        row: sqlite3.Row,
    ) -> AttemptRecord:
        if row["task_id"] is not None:
            target_type = (
                AttemptTargetType.TASK
            )

            target_id = int(
                row["task_id"]
            )

        else:
            target_type = (
                AttemptTargetType.STEP
            )

            target_id = int(
                row["step_id"]
            )

        return AttemptRecord(
            id=int(
                row["id"]
            ),
            target_type=target_type,
            target_id=target_id,
            attempt_number=int(
                row["attempt_number"]
            ),
            status=AttemptStatus(
                row["status"]
            ),
            approach=(
                str(row["approach"])
                if row["approach"]
                is not None
                else None
            ),
            failure_reason=(
                str(
                    row["failure_reason"]
                )
                if row["failure_reason"]
                is not None
                else None
            ),
            result_summary=(
                str(
                    row["result_summary"]
                )
                if row["result_summary"]
                is not None
                else None
            ),
            result_artifacts=(
                self._load_list(
                    row[
                        "result_artifacts_json"
                    ]
                )
            ),
            created_at=str(
                row["created_at"]
            ),
            started_at=str(
                row["started_at"]
            ),
            finished_at=(
                str(
                    row["finished_at"]
                )
                if row["finished_at"]
                is not None
                else None
            ),
            updated_at=str(
                row["updated_at"]
            ),
        )

    def _start_attempt(
        self,
        *,
        target_type: AttemptTargetType,
        target_id: int,
        approach: str | None,
    ) -> AttemptRecord:
        if target_type is AttemptTargetType.TASK:
            target_table = "tasks"
            target_column = "task_id"

        else:
            target_table = "steps"
            target_column = "step_id"

        try:
            with self._connect() as connection:
                target = connection.execute(
                    f"""
                    SELECT
                        id,
                        attempt_count

                    FROM {target_table}

                    WHERE id = ?
                    """,
                    (target_id,),
                ).fetchone()

                if target is None:
                    raise AttemptStoreError(
                        f"unknown "
                        f"{target_type.value.lower()}: "
                        f"{target_id}"
                    )

                active = connection.execute(
                    f"""
                    SELECT id

                    FROM attempts

                    WHERE
                        {target_column} = ?
                        AND status = 'IN_PROGRESS'

                    LIMIT 1
                    """,
                    (target_id,),
                ).fetchone()

                if active is not None:
                    raise AttemptStoreError(
                        "target already has "
                        "active attempt"
                    )

                row = connection.execute(
                    f"""
                    SELECT
                        COALESCE(
                            MAX(attempt_number),
                            0
                        ) AS max_number

                    FROM attempts

                    WHERE {target_column} = ?
                    """,
                    (target_id,),
                ).fetchone()

                next_number = (
                    int(row["max_number"])
                    + 1
                )

                cursor = connection.execute(
                    f"""
                    INSERT INTO attempts (
                        {target_column},
                        attempt_number,
                        status,
                        approach,
                        result_artifacts_json
                    )
                    VALUES (
                        ?,
                        ?,
                        ?,
                        ?,
                        '[]'
                    )
                    """,
                    (
                        target_id,
                        next_number,
                        AttemptStatus
                        .IN_PROGRESS
                        .value,
                        approach,
                    ),
                )

                connection.execute(
                    f"""
                    UPDATE {target_table}

                    SET
                        attempt_count = ?,
                        updated_at =
                            CURRENT_TIMESTAMP

                    WHERE id = ?
                    """,
                    (
                        next_number,
                        target_id,
                    ),
                )

                attempt_id = int(
                    cursor.lastrowid
                )

        except sqlite3.IntegrityError as error:
            raise AttemptStoreError(
                "target already has "
                "active attempt"
            ) from error

        attempt = self.get_attempt(
            attempt_id
        )

        if attempt is None:
            raise AttemptStoreError(
                "attempt was not created"
            )

        return attempt

    def start_task_attempt(
        self,
        task_id: int,
        *,
        approach: str | None = None,
    ) -> AttemptRecord:
        return self._start_attempt(
            target_type=(
                AttemptTargetType.TASK
            ),
            target_id=task_id,
            approach=approach,
        )

    def start_step_attempt(
        self,
        step_id: int,
        *,
        approach: str | None = None,
    ) -> AttemptRecord:
        return self._start_attempt(
            target_type=(
                AttemptTargetType.STEP
            ),
            target_id=step_id,
            approach=approach,
        )

    def get_attempt(
        self,
        attempt_id: int,
    ) -> AttemptRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM attempts
                WHERE id = ?
                """,
                (attempt_id,),
            ).fetchone()

        if row is None:
            return None

        return self._row_to_attempt(
            row
        )

    def get_step_attempts(
        self,
        step_id: int,
    ) -> list[AttemptRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM attempts
                WHERE step_id = ?
                ORDER BY attempt_number ASC
                """,
                (step_id,),
            ).fetchall()

        return [
            self._row_to_attempt(row)
            for row in rows
        ]

    def get_task_attempts(
        self,
        task_id: int,
    ) -> list[AttemptRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM attempts
                WHERE task_id = ?
                ORDER BY attempt_number ASC
                """,
                (task_id,),
            ).fetchall()

        return [
            self._row_to_attempt(row)
            for row in rows
        ]

    def finish_attempt(
        self,
        attempt_id: int,
        *,
        status: AttemptStatus,
        failure_reason: str | None = None,
        result_summary: str | None = None,
        result_artifacts: list[str] | None = None,
    ) -> None:
        if status is AttemptStatus.IN_PROGRESS:
            raise AttemptStoreError(
                "finish status cannot be "
                "IN_PROGRESS"
            )

        if (
            status
            in {
                AttemptStatus.FAILED,
                AttemptStatus.BLOCKED,
            }
            and (
                failure_reason is None
                or not failure_reason.strip()
            )
        ):
            raise AttemptStoreError(
                "failure reason is required"
            )

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT status
                FROM attempts
                WHERE id = ?
                """,
                (attempt_id,),
            ).fetchone()

            if row is None:
                raise AttemptStoreError(
                    f"unknown attempt: "
                    f"{attempt_id}"
                )

            if (
                AttemptStatus(
                    row["status"]
                )
                is not AttemptStatus.IN_PROGRESS
            ):
                raise AttemptStoreError(
                    "attempt is already finished"
                )

            connection.execute(
                """
                UPDATE attempts

                SET
                    status = ?,
                    failure_reason = ?,
                    result_summary = ?,
                    result_artifacts_json = ?,
                    finished_at =
                        CURRENT_TIMESTAMP,
                    updated_at =
                        CURRENT_TIMESTAMP

                WHERE id = ?
                """,
                (
                    status.value,
                    failure_reason,
                    result_summary,
                    self._dump_list(
                        result_artifacts or []
                    ),
                    attempt_id,
                ),
            )