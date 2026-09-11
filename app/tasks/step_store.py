from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.tasks.models import (
    StepDraft,
    StepRecord,
    StepStatus,
)
from app.tasks.store import (
    DEFAULT_DATABASE_PATH,
)


class StepStoreError(
    RuntimeError
):
    pass


class StepStore:
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
                CREATE TABLE IF NOT EXISTS steps (
                    id INTEGER
                        PRIMARY KEY AUTOINCREMENT,

                    task_id INTEGER
                        NOT NULL,

                    position INTEGER
                        NOT NULL,

                    title TEXT
                        NOT NULL,

                    description TEXT
                        NOT NULL,

                    status TEXT
                        NOT NULL
                        DEFAULT 'PENDING',

                    requires_json TEXT
                        NOT NULL
                        DEFAULT '[]',

                    produces_json TEXT
                        NOT NULL
                        DEFAULT '[]',

                    success_criteria_json TEXT
                        NOT NULL
                        DEFAULT '[]',

                    attempt_count INTEGER
                        NOT NULL
                        DEFAULT 0,

                    result_summary TEXT,

                    result_artifacts_json TEXT
                        NOT NULL
                        DEFAULT '[]',

                    failure_reason TEXT,

                    verification_status TEXT,

                    verification_evidence_json TEXT
                        NOT NULL
                        DEFAULT '[]',

                    created_at DATETIME
                        NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,

                    started_at DATETIME,

                    finished_at DATETIME,

                    updated_at DATETIME
                        NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,

                    UNIQUE (
                        task_id,
                        position
                    ),

                    FOREIGN KEY (task_id)
                        REFERENCES tasks(id)
                        ON DELETE CASCADE
                )
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_steps_task_id
                ON steps(task_id)
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_steps_status
                ON steps(status)
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
            raise ValueError(
                "Ожидался JSON-массив."
            )

        return [
            str(item)
            for item in data
        ]

    def create_steps(
        self,
        task_id: int,
        steps: list[StepDraft],
    ) -> list[int]:
        if not steps:
            raise StepStoreError(
                "task requires at least one step"
            )

        with self._connect() as connection:
            task_exists = connection.execute(
                """
                SELECT id
                FROM tasks
                WHERE id = ?
                """,
                (task_id,),
            ).fetchone()

            if task_exists is None:
                raise StepStoreError(
                    f"unknown task: {task_id}"
                )

            existing = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM steps
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()

            if (
                existing is not None
                and int(existing["count"]) > 0
            ):
                raise StepStoreError(
                    "task already has steps"
                )

            created_ids: list[int] = []

            for position, step in enumerate(
                steps,
                start=1,
            ):
                cursor = connection.execute(
                    """
                    INSERT INTO steps (
                        task_id,
                        position,
                        title,
                        description,
                        status,
                        requires_json,
                        produces_json,
                        success_criteria_json,
                        attempt_count,
                        result_artifacts_json,
                        verification_evidence_json
                    )
                    VALUES (
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        0,
                        '[]',
                        '[]'
                    )
                    """,
                    (
                        task_id,
                        position,
                        step.title,
                        step.description,
                        StepStatus.PENDING.value,
                        self._dump_list(
                            step.requires
                        ),
                        self._dump_list(
                            step.produces
                        ),
                        self._dump_list(
                            step.success_criteria
                        ),
                    ),
                )

                created_ids.append(
                    int(cursor.lastrowid)
                )

            return created_ids

    def _row_to_step(
        self,
        row: sqlite3.Row,
    ) -> StepRecord:
        return StepRecord(
            id=int(
                row["id"]
            ),
            task_id=int(
                row["task_id"]
            ),
            position=int(
                row["position"]
            ),
            title=str(
                row["title"]
            ),
            description=str(
                row["description"]
            ),
            status=StepStatus(
                row["status"]
            ),
            requires=self._load_list(
                row["requires_json"]
            ),
            produces=self._load_list(
                row["produces_json"]
            ),
            success_criteria=(
                self._load_list(
                    row[
                        "success_criteria_json"
                    ]
                )
            ),
            attempt_count=int(
                row["attempt_count"]
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
            failure_reason=(
                str(
                    row["failure_reason"]
                )
                if row["failure_reason"]
                is not None
                else None
            ),
            verification_status=(
                str(
                    row[
                        "verification_status"
                    ]
                )
                if row[
                    "verification_status"
                ] is not None
                else None
            ),
            verification_evidence=(
                self._load_list(
                    row[
                        "verification_evidence_json"
                    ]
                )
            ),
            created_at=str(
                row["created_at"]
            ),
            started_at=(
                str(
                    row["started_at"]
                )
                if row["started_at"]
                is not None
                else None
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

    def get_steps(
        self,
        task_id: int,
    ) -> list[StepRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM steps
                WHERE task_id = ?
                ORDER BY position ASC
                """,
                (task_id,),
            ).fetchall()

        return [
            self._row_to_step(row)
            for row in rows
        ]

    def get_step(
        self,
        step_id: int,
    ) -> StepRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM steps
                WHERE id = ?
                """,
                (step_id,),
            ).fetchone()

        if row is None:
            return None

        return self._row_to_step(
            row
        )

    def set_current_step(
        self,
        *,
        task_id: int,
        step_id: int | None,
    ) -> None:
        with self._connect() as connection:
            if step_id is not None:
                row = connection.execute(
                    """
                    SELECT
                        id,
                        task_id
                    FROM steps
                    WHERE id = ?
                    """,
                    (step_id,),
                ).fetchone()

                if row is None:
                    raise StepStoreError(
                        f"unknown step: {step_id}"
                    )

                if (
                    int(row["task_id"])
                    != task_id
                ):
                    raise StepStoreError(
                        "step does not belong "
                        "to task"
                    )

            connection.execute(
                """
                UPDATE tasks

                SET
                    current_step = ?,
                    updated_at =
                        CURRENT_TIMESTAMP

                WHERE id = ?
                """,
                (
                    step_id,
                    task_id,
                ),
            )

    def update_step_status(
        self,
        step_id: int,
        status: StepStatus,
    ) -> None:
        with self._connect() as connection:
            if (
                status
                is StepStatus.IN_PROGRESS
            ):
                connection.execute(
                    """
                    UPDATE steps

                    SET
                        status = ?,
                        started_at =
                            COALESCE(
                                started_at,
                                CURRENT_TIMESTAMP
                            ),
                        updated_at =
                            CURRENT_TIMESTAMP

                    WHERE id = ?
                    """,
                    (
                        status.value,
                        step_id,
                    ),
                )

                return

            if status in {
                StepStatus.DONE,
                StepStatus.FAILED,
                StepStatus.SUPERSEDED,
            }:
                connection.execute(
                    """
                    UPDATE steps

                    SET
                        status = ?,
                        finished_at =
                            CURRENT_TIMESTAMP,
                        updated_at =
                            CURRENT_TIMESTAMP

                    WHERE id = ?
                    """,
                    (
                        status.value,
                        step_id,
                    ),
                )

                return

            connection.execute(
                """
                UPDATE steps

                SET
                    status = ?,
                    updated_at =
                        CURRENT_TIMESTAMP

                WHERE id = ?
                """,
                (
                    status.value,
                    step_id,
                ),
            )

    def set_step_result(
        self,
        step_id: int,
        *,
        result_summary: str,
        result_artifacts: list[str],
        verification_status: str,
        verification_evidence: list[str],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE steps

                SET
                    result_summary = ?,
                    result_artifacts_json = ?,
                    verification_status = ?,
                    verification_evidence_json = ?,
                    updated_at =
                        CURRENT_TIMESTAMP

                WHERE id = ?
                """,
                (
                    result_summary,
                    self._dump_list(
                        result_artifacts
                    ),
                    verification_status,
                    self._dump_list(
                        verification_evidence
                    ),
                    step_id,
                ),
            )

    def set_step_verification(
        self,
        step_id: int,
        *,
        verification_status: str,
        verification_evidence: list[str],
        failure_reason: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE steps

                SET
                    verification_status = ?,
                    verification_evidence_json = ?,
                    failure_reason = ?,
                    updated_at =
                        CURRENT_TIMESTAMP

                WHERE id = ?
                """,
                (
                    verification_status,
                    self._dump_list(
                        verification_evidence
                    ),
                    failure_reason,
                    step_id,
                ),
            )