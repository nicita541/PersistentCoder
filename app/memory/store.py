from __future__ import annotations

import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT
    / "data"
    / "persistent_coder.db"
)


class MemoryStore:
    def __init__(
        self,
        database_path: Path | None = None,
    ) -> None:
        self.database_path = (
            database_path
            if database_path is not None
            else DEFAULT_DATABASE_PATH
        )

        self.database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._initialize_database()
        self._migrate_database()

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
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER
                        PRIMARY KEY AUTOINCREMENT,

                    type TEXT NOT NULL,

                    content TEXT NOT NULL,

                    why TEXT,

                    source TEXT NOT NULL,

                    importance INTEGER
                        NOT NULL
                        DEFAULT 50,

                    confidence REAL
                        NOT NULL
                        DEFAULT 1.0,

                    status TEXT
                        NOT NULL
                        DEFAULT 'ACTIVE',

                    superseded_by INTEGER,

                    created_at DATETIME
                        NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,

                    updated_at DATETIME
                        NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def _get_columns(
        self,
        connection: sqlite3.Connection,
    ) -> set[str]:
        rows = connection.execute(
            """
            PRAGMA table_info(memories)
            """
        ).fetchall()

        return {
            str(row["name"])
            for row in rows
        }

    def _migrate_database(
        self,
    ) -> None:
        """
        Безопасно обновляет старую базу
        без удаления существующей памяти.
        """

        with self._connect() as connection:
            columns = self._get_columns(
                connection
            )

            if "why" not in columns:
                connection.execute(
                    """
                    ALTER TABLE memories
                    ADD COLUMN why TEXT
                    """
                )

            if "confidence" not in columns:
                connection.execute(
                    """
                    ALTER TABLE memories
                    ADD COLUMN confidence REAL
                    NOT NULL
                    DEFAULT 1.0
                    """
                )

            if "updated_at" not in columns:
                connection.execute(
                    """
                    ALTER TABLE memories
                    ADD COLUMN updated_at DATETIME
                    """
                )

                connection.execute(
                    """
                    UPDATE memories
                    SET updated_at = created_at
                    WHERE updated_at IS NULL
                    """
                )

            if "superseded_by" not in columns:
                connection.execute(
                    """
                    ALTER TABLE memories
                    ADD COLUMN superseded_by INTEGER
                    """
                )

    def add_memory(
        self,
        *,
        memory_type: str,
        content: str,
        source: str = "USER",
        importance: int = 50,
        why: str | None = None,
        confidence: float = 1.0,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memories (
                    type,
                    content,
                    why,
                    source,
                    importance,
                    confidence,
                    status,
                    superseded_by,
                    updated_at
                )
                VALUES (
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    'ACTIVE',
                    NULL,
                    CURRENT_TIMESTAMP
                )
                """,
                (
                    memory_type,
                    content,
                    why,
                    source,
                    importance,
                    confidence,
                ),
            )

            return int(
                cursor.lastrowid
            )

    def supersede_memories(
        self,
        *,
        memory_ids: list[int],
        superseded_by: int,
    ) -> None:
        if not memory_ids:
            return

        placeholders = ", ".join(
            "?"
            for _ in memory_ids
        )

        parameters: list[object] = [
            superseded_by,
            *memory_ids,
            superseded_by,
        ]

        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE memories

                SET
                    status = 'SUPERSEDED',
                    superseded_by = ?,
                    updated_at = CURRENT_TIMESTAMP

                WHERE
                    id IN ({placeholders})
                    AND status = 'ACTIVE'
                    AND id != ?
                """,
                parameters,
            )

    def get_active_memories(
        self,
    ) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    type,
                    content,
                    why,
                    source,
                    importance,
                    confidence,
                    status,
                    superseded_by,
                    created_at,
                    updated_at

                FROM memories

                WHERE status = 'ACTIVE'

                ORDER BY
                    importance DESC,
                    id ASC
                """
            ).fetchall()

        return [
            dict(row)
            for row in rows
        ]