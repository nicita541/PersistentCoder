from __future__ import annotations

import sqlite3
from pathlib import Path

from app.tasks.models import (
    ReplanDecision,
    ReplanRecord,
    ReplanScope,
    ReplanTargetType,
    ReplanTrigger,
)
from app.tasks.store import (
    DEFAULT_DATABASE_PATH,
)
from app.tasks.store_context import (
    StoreContext,
    owns_plan,
    owns_step,
    owns_task,
    split_store_binding,
)


class ReplanStore:
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

    def _owns_target(
        self,
        connection: sqlite3.Connection,
        target_type: ReplanTargetType,
        target_id: int,
    ) -> bool:
        if target_type is ReplanTargetType.PLAN:
            return owns_plan(connection, self.context, target_id)
        if target_type is ReplanTargetType.TASK:
            return owns_task(connection, self.context, target_id)
        return owns_step(connection, self.context, target_id)

    def _connect(
        self,
    ) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path
        )

        connection.row_factory = (
            sqlite3.Row
        )

        return connection

    def _initialize_database(
        self,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS
                    replan_events (
                        id INTEGER
                            PRIMARY KEY AUTOINCREMENT,

                        scope TEXT
                            NOT NULL,

                        trigger TEXT
                            NOT NULL,

                        target_type TEXT
                            NOT NULL,

                        target_id INTEGER
                            NOT NULL,

                        reason TEXT
                            NOT NULL,

                        created_at DATETIME
                            NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    )
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_replan_target

                ON replan_events(
                    target_type,
                    target_id
                )
                """
            )

    @staticmethod
    def _row_to_record(
        row: sqlite3.Row,
    ) -> ReplanRecord:
        return ReplanRecord(
            id=int(
                row["id"]
            ),
            scope=ReplanScope(
                row["scope"]
            ),
            trigger=ReplanTrigger(
                row["trigger"]
            ),
            target_type=ReplanTargetType(
                row["target_type"]
            ),
            target_id=int(
                row["target_id"]
            ),
            reason=str(
                row["reason"]
            ),
            created_at=str(
                row["created_at"]
            ),
        )

    def record(
        self,
        decision: ReplanDecision,
    ) -> ReplanRecord:
        with self._connect() as connection:
            if not self._owns_target(
                connection,
                decision.target_type,
                decision.target_id,
            ):
                raise ValueError(
                    "unknown replan target for current project: "
                    f"{decision.target_type.value} {decision.target_id}"
                )

            cursor = connection.execute(
                """
                INSERT INTO replan_events (
                    scope,
                    trigger,
                    target_type,
                    target_id,
                    reason
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    decision.scope.value,
                    decision.trigger.value,
                    decision.target_type.value,
                    decision.target_id,
                    decision.reason,
                ),
            )

            event_id = int(
                cursor.lastrowid
            )

            row = connection.execute(
                """
                SELECT *
                FROM replan_events
                WHERE id = ?
                """,
                (event_id,),
            ).fetchone()

        assert row is not None

        return self._row_to_record(
            row
        )

    def get_for_target(
        self,
        target_type: ReplanTargetType,
        target_id: int,
    ) -> list[ReplanRecord]:
        with self._connect() as connection:
            if not self._owns_target(
                connection,
                target_type,
                target_id,
            ):
                return []

            rows = connection.execute(
                """
                SELECT *
                FROM replan_events

                WHERE
                    target_type = ?
                    AND target_id = ?

                ORDER BY id ASC
                """,
                (
                    target_type.value,
                    target_id,
                ),
            ).fetchall()

        return [
            self._row_to_record(row)
            for row in rows
        ]
