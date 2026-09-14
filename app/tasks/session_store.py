from __future__ import annotations

import sqlite3
import uuid

from app.agent.session import AgentSession, SessionStatus
from app.tasks.store_context import StoreContext


class SessionConflictError(RuntimeError):
    pass


class SessionStore:
    def __init__(self, context: StoreContext) -> None:
        if not isinstance(context, StoreContext):
            raise TypeError("SessionStore requires a StoreContext")
        self.context = context
        self.database_path = context.database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    canonical_source_root TEXT NOT NULL,
                    sandbox_session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    active_run_id INTEGER,
                    patch_manifest_id TEXT,
                    version INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_sessions_project_status
                ON agent_sessions(project_id, status)
                """
            )

    def create(self, *, sandbox_session_id: str) -> str:
        session_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_sessions (
                    id,
                    project_id,
                    canonical_source_root,
                    sandbox_session_id,
                    status,
                    version
                )
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (
                    session_id,
                    self.context.project_id,
                    self.context.canonical_source_root,
                    sandbox_session_id,
                    SessionStatus.CLEAN.value,
                ),
            )
        return session_id

    def _row_to_session(self, row: sqlite3.Row) -> AgentSession:
        return AgentSession(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            canonical_source_root=str(row["canonical_source_root"]),
            sandbox_session_id=str(row["sandbox_session_id"]),
            status=SessionStatus(row["status"]),
            active_run_id=(
                int(row["active_run_id"])
                if row["active_run_id"] is not None
                else None
            ),
            patch_manifest_id=(
                str(row["patch_manifest_id"])
                if row["patch_manifest_id"] is not None
                else None
            ),
            version=int(row["version"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def get(self, session_id: str) -> AgentSession | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM agent_sessions
                WHERE id = ?
                    AND project_id = ?
                    AND canonical_source_root = ?
                """,
                (
                    session_id,
                    self.context.project_id,
                    self.context.canonical_source_root,
                ),
            ).fetchone()
        return self._row_to_session(row) if row is not None else None

    def get_by_sandbox_session_id(
        self,
        sandbox_session_id: str,
    ) -> AgentSession | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM agent_sessions
                WHERE sandbox_session_id = ?
                    AND project_id = ?
                    AND canonical_source_root = ?
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (
                    sandbox_session_id,
                    self.context.project_id,
                    self.context.canonical_source_root,
                ),
            ).fetchone()
        return self._row_to_session(row) if row is not None else None

    def update(self, session: AgentSession) -> AgentSession:
        if (
            session.project_id != self.context.project_id
            or session.canonical_source_root
            != self.context.canonical_source_root
        ):
            raise SessionConflictError(
                "session identity does not match store context"
            )

        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE agent_sessions
                SET
                    sandbox_session_id = ?,
                    status = ?,
                    active_run_id = ?,
                    patch_manifest_id = ?,
                    version = version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                    AND project_id = ?
                    AND canonical_source_root = ?
                    AND version = ?
                """,
                (
                    session.sandbox_session_id,
                    session.status.value,
                    session.active_run_id,
                    session.patch_manifest_id,
                    session.id,
                    self.context.project_id,
                    self.context.canonical_source_root,
                    session.version,
                ),
            )
            if result.rowcount != 1:
                raise SessionConflictError(
                    "session was changed by another writer"
                )

        updated = self.get(session.id)
        if updated is None:
            raise SessionConflictError("updated session disappeared")
        return updated

    def list_dirty(self) -> list[AgentSession]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM agent_sessions
                WHERE project_id = ?
                    AND canonical_source_root = ?
                    AND status IN (?, ?)
                ORDER BY created_at ASC, id ASC
                """,
                (
                    self.context.project_id,
                    self.context.canonical_source_root,
                    SessionStatus.DIRTY_VERIFIED.value,
                    SessionStatus.DIRTY_FAILED.value,
                ),
            ).fetchall()
        return [self._row_to_session(row) for row in rows]
