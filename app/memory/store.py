from __future__ import annotations

import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATABASE_PATH = (
    PROJECT_ROOT
    / "data"
    / "persistent_coder.db"
)


class MemoryStore:
    def __init__(self) -> None:
        DATABASE_PATH.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.database_path = DATABASE_PATH

        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path
        )

        connection.row_factory = sqlite3.Row

        return connection

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    importance INTEGER NOT NULL DEFAULT 50,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    created_at DATETIME
                        NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def add_memory(
        self,
        *,
        memory_type: str,
        content: str,
        source: str = "USER",
        importance: int = 50,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memories (
                    type,
                    content,
                    source,
                    importance
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    memory_type,
                    content,
                    source,
                    importance,
                ),
            )

            return int(cursor.lastrowid)

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
                    source,
                    importance,
                    status,
                    created_at
                FROM memories
                WHERE status = 'ACTIVE'
                ORDER BY importance DESC, id ASC
                """
            ).fetchall()

        return [
            dict(row)
            for row in rows
        ]