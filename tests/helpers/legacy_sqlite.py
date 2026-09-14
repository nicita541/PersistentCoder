from __future__ import annotations

import sqlite3
from pathlib import Path


def seed_legacy_run(
    database: Path,
    request: str,
    sandbox_session_id: str,
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'RUNNING',
                phase TEXT,
                plan_id INTEGER,
                task_id INTEGER,
                step_id INTEGER,
                attempt_id INTEGER,
                sandbox_session_id TEXT,
                checkpoint_id TEXT,
                recovery_note TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO agent_runs (
                request,
                status,
                sandbox_session_id
            )
            VALUES (?, 'RUNNING', ?)
            """,
            (request, sandbox_session_id),
        )


def seed_legacy_memory(
    database: Path,
    memory_type: str,
    content: str,
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                why TEXT,
                source TEXT NOT NULL,
                importance INTEGER NOT NULL DEFAULT 50,
                confidence REAL NOT NULL DEFAULT 1.0,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                superseded_by INTEGER,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO memories (type, content, source)
            VALUES (?, ?, 'USER')
            """,
            (memory_type, content),
        )
