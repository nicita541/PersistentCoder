from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.tasks.models import (
    VerificationRecord,
    VerificationStatus,
    VerificationTargetType,
)
from app.tasks.store import (
    DEFAULT_DATABASE_PATH,
)


class VerificationStore:
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
                CREATE TABLE IF NOT EXISTS
                    verifications (
                        id INTEGER
                            PRIMARY KEY AUTOINCREMENT,

                        task_id INTEGER,

                        step_id INTEGER,

                        status TEXT
                            NOT NULL,

                        reason TEXT,

                        evidence_json TEXT
                            NOT NULL
                            DEFAULT '[]',

                        created_at DATETIME
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
                CREATE INDEX IF NOT EXISTS
                    idx_verifications_task

                ON verifications(task_id)
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_verifications_step

                ON verifications(step_id)
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
        value: str,
    ) -> list[str]:
        data = json.loads(
            value
        )

        if not isinstance(
            data,
            list,
        ):
            raise ValueError(
                "verification evidence "
                "must be a list"
            )

        return [
            str(item)
            for item in data
        ]

    def _row_to_record(
        self,
        row: sqlite3.Row,
    ) -> VerificationRecord:
        if row["task_id"] is not None:
            target_type = (
                VerificationTargetType.TASK
            )

            target_id = int(
                row["task_id"]
            )

        else:
            target_type = (
                VerificationTargetType.STEP
            )

            target_id = int(
                row["step_id"]
            )

        return VerificationRecord(
            id=int(
                row["id"]
            ),
            target_type=target_type,
            target_id=target_id,
            status=VerificationStatus(
                row["status"]
            ),
            reason=(
                str(row["reason"])
                if row["reason"]
                is not None
                else None
            ),
            evidence=self._load_list(
                row["evidence_json"]
            ),
            created_at=str(
                row["created_at"]
            ),
        )

    def _record(
        self,
        *,
        target_type: VerificationTargetType,
        target_id: int,
        status: VerificationStatus,
        evidence: list[str],
        reason: str | None,
    ) -> VerificationRecord:
        column = (
            "task_id"
            if target_type
            is VerificationTargetType.TASK
            else "step_id"
        )

        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                INSERT INTO verifications (
                    {column},
                    status,
                    reason,
                    evidence_json
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    target_id,
                    status.value,
                    reason,
                    self._dump_list(
                        evidence
                    ),
                ),
            )

            verification_id = int(
                cursor.lastrowid
            )

            row = connection.execute(
                """
                SELECT *
                FROM verifications
                WHERE id = ?
                """,
                (
                    verification_id,
                ),
            ).fetchone()

        assert row is not None

        return self._row_to_record(
            row
        )

    def record_step(
        self,
        step_id: int,
        *,
        status: VerificationStatus,
        evidence: list[str],
        reason: str | None = None,
    ) -> VerificationRecord:
        return self._record(
            target_type=(
                VerificationTargetType.STEP
            ),
            target_id=step_id,
            status=status,
            evidence=evidence,
            reason=reason,
        )

    def record_task(
        self,
        task_id: int,
        *,
        status: VerificationStatus,
        evidence: list[str],
        reason: str | None = None,
    ) -> VerificationRecord:
        return self._record(
            target_type=(
                VerificationTargetType.TASK
            ),
            target_id=task_id,
            status=status,
            evidence=evidence,
            reason=reason,
        )

    def get_step_verifications(
        self,
        step_id: int,
    ) -> list[VerificationRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM verifications

                WHERE step_id = ?

                ORDER BY id ASC
                """,
                (step_id,),
            ).fetchall()

        return [
            self._row_to_record(row)
            for row in rows
        ]

    def get_task_verifications(
        self,
        task_id: int,
    ) -> list[VerificationRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM verifications

                WHERE task_id = ?

                ORDER BY id ASC
                """,
                (task_id,),
            ).fetchall()

        return [
            self._row_to_record(row)
            for row in rows
        ]