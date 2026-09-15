from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from enum import Enum
from typing import Iterator

from app.agent.state import AgentState
from app.agent.session import SessionStatus
from app.tasks.migrations import migrate
from app.tasks.models import AttemptStatus, AttemptTargetType, PlanStatus
from app.tasks.store_context import StoreContext, owns_plan, owns_step, owns_task


class StateAuthorityError(RuntimeError):
    pass


class ConcurrentWriterError(StateAuthorityError):
    pass


def _json_default(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


class RuntimeUnitOfWork:
    """Project-scoped authority for mandatory durable transitions."""

    def __init__(self, context: StoreContext, *, timeout: float = 5.0) -> None:
        if not isinstance(context, StoreContext):
            raise TypeError("RuntimeUnitOfWork requires a StoreContext")
        self.context = context
        self.database_path = context.database_path
        self.timeout = timeout
        migrate(self.database_path, timeout=timeout)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.timeout,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if "locked" in str(error).lower():
                    raise ConcurrentWriterError(
                        "another mandatory state writer is active"
                    ) from error
                raise
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _require_run(self, connection: sqlite3.Connection, run_id: int) -> None:
        row = connection.execute(
            """
            SELECT id FROM agent_runs
            WHERE id = ? AND project_id = ? AND canonical_source_root = ?
            """,
            (
                run_id,
                self.context.project_id,
                self.context.canonical_source_root,
            ),
        ).fetchone()
        if row is None:
            raise StateAuthorityError(
                f"run {run_id} does not belong to the current project"
            )

    def _require_running_run(
        self, connection: sqlite3.Connection, run_id: int
    ) -> None:
        self._require_run(connection, run_id)
        status = connection.execute(
            "SELECT status FROM agent_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if status is None or str(status["status"]) != "RUNNING":
            raise StateAuthorityError(f"run {run_id} is not active")

    def _insert_snapshot(
        self,
        connection: sqlite3.Connection,
        run_id: int,
        state: AgentState,
    ) -> int:
        serialized = json.dumps(
            asdict(state), ensure_ascii=False, default=_json_default
        )
        cursor = connection.execute(
            """
            INSERT INTO agent_state_snapshots (
                run_id, project_id, canonical_source_root, phase,
                plan_id, task_id, step_id, attempt_id, checkpoint_id,
                completion, state_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                self.context.project_id,
                self.context.canonical_source_root,
                state.phase.value,
                state.plan_id,
                state.active_task_id,
                state.active_step_id,
                state.attempt_id,
                state.checkpoint_id,
                state.completion,
                serialized,
            ),
        )
        return int(cursor.lastrowid)

    def start_run(
        self,
        request: str,
        *,
        sandbox_session_id: str | None = None,
        session_id: str | None = None,
        session_version: int | None = None,
    ) -> tuple[int, int | None]:
        request = (request or "").strip()
        if not request:
            raise ValueError("request required")
        if session_id is not None and session_version is None:
            raise ValueError("session version is required")
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO agent_runs (
                    request, project_id, canonical_source_root, status,
                    phase, sandbox_session_id
                ) VALUES (?, ?, ?, 'RUNNING', 'PLANNING', ?)
                """,
                (
                    request,
                    self.context.project_id,
                    self.context.canonical_source_root,
                    sandbox_session_id,
                ),
            )
            run_id = int(cursor.lastrowid)
            new_version = None
            if session_id is not None:
                session = connection.execute(
                    """
                    UPDATE agent_sessions SET status = 'RUNNING',
                        active_run_id = ?, version = version + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND project_id = ? AND canonical_source_root = ?
                        AND version = ? AND status = 'CLEAN'
                    """,
                    (
                        run_id,
                        session_id,
                        self.context.project_id,
                        self.context.canonical_source_root,
                        session_version,
                    ),
                )
                if session.rowcount != 1:
                    raise StateAuthorityError(
                        "session is not clean or was changed by another writer"
                    )
                new_version = session_version + 1
            return run_id, new_version

    def persist_state(self, run_id: int, state: AgentState) -> int:
        with self.transaction() as connection:
            self._require_running_run(connection, run_id)
            connection.execute(
                """
                UPDATE agent_runs SET
                    phase = ?, plan_id = ?, task_id = ?, step_id = ?,
                    attempt_id = ?, checkpoint_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND project_id = ? AND canonical_source_root = ?
                """,
                (
                    state.phase.value,
                    state.plan_id,
                    state.active_task_id,
                    state.active_step_id,
                    state.attempt_id,
                    state.checkpoint_id,
                    run_id,
                    self.context.project_id,
                    self.context.canonical_source_root,
                ),
            )
            return self._insert_snapshot(connection, run_id, state)

    def get_latest_snapshot(self, run_id: int) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM agent_state_snapshots
                WHERE run_id = ? AND project_id = ?
                    AND canonical_source_root = ?
                ORDER BY id DESC LIMIT 1
                """,
                (
                    run_id,
                    self.context.project_id,
                    self.context.canonical_source_root,
                ),
            ).fetchone()
        return dict(row) if row is not None else None

    def prepare_attempt(
        self,
        run_id: int,
        state: AgentState,
        *,
        target_type: AttemptTargetType,
        target_id: int,
        approach: str | None,
        sandbox_session_id: str,
        checkpoint_id: str,
    ) -> int:
        target_column = (
            "task_id" if target_type is AttemptTargetType.TASK else "step_id"
        )
        target_table = (
            "tasks" if target_type is AttemptTargetType.TASK else "steps"
        )
        previous_attempt = state.attempt_id
        previous_checkpoint = state.checkpoint_id
        try:
            with self.transaction() as connection:
                self._require_running_run(connection, run_id)
                owns_target = (
                    owns_task(connection, self.context, target_id)
                    if target_type is AttemptTargetType.TASK
                    else owns_step(connection, self.context, target_id)
                )
                if not owns_target:
                    raise StateAuthorityError(
                        f"{target_type.value.lower()} {target_id} does not "
                        "belong to the current project"
                    )
                active = connection.execute(
                    f"SELECT id FROM attempts WHERE {target_column} = ? "
                    "AND status = 'IN_PROGRESS'",
                    (target_id,),
                ).fetchone()
                if active is not None:
                    raise StateAuthorityError("target already has active attempt")
                row = connection.execute(
                    f"SELECT COALESCE(MAX(attempt_number), 0) FROM attempts "
                    f"WHERE {target_column} = ?",
                    (target_id,),
                ).fetchone()
                attempt_number = int(row[0]) + 1
                cursor = connection.execute(
                    f"""
                    INSERT INTO attempts (
                        {target_column}, attempt_number, status, approach,
                        result_artifacts_json
                    ) VALUES (?, ?, ?, ?, '[]')
                    """,
                    (
                        target_id,
                        attempt_number,
                        AttemptStatus.IN_PROGRESS.value,
                        approach,
                    ),
                )
                attempt_id = int(cursor.lastrowid)
                connection.execute(
                    f"UPDATE {target_table} SET attempt_count = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (attempt_number, target_id),
                )
                connection.execute(
                    """
                    INSERT INTO file_operation_journal (
                        run_id, project_id, canonical_source_root, attempt_id,
                        sandbox_session_id, checkpoint_id, state
                    ) VALUES (?, ?, ?, ?, ?, ?, 'PREPARING')
                    """,
                    (
                        run_id,
                        self.context.project_id,
                        self.context.canonical_source_root,
                        attempt_id,
                        sandbox_session_id,
                        checkpoint_id,
                    ),
                )
                state.attempt_id = attempt_id
                state.checkpoint_id = checkpoint_id
                connection.execute(
                    """
                    UPDATE agent_runs SET attempt_id = ?, checkpoint_id = ?,
                        task_id = ?, step_id = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND project_id = ? AND canonical_source_root = ?
                    """,
                    (
                        attempt_id,
                        checkpoint_id,
                        state.active_task_id,
                        state.active_step_id,
                        run_id,
                        self.context.project_id,
                        self.context.canonical_source_root,
                    ),
                )
                self._insert_snapshot(connection, run_id, state)
                return attempt_id
        except Exception:
            state.attempt_id = previous_attempt
            state.checkpoint_id = previous_checkpoint
            raise

    def mark_checkpoint_ready(
        self, run_id: int, attempt_id: int, state: AgentState
    ) -> None:
        with self.transaction() as connection:
            self._require_running_run(connection, run_id)
            result = connection.execute(
                """
                UPDATE file_operation_journal
                SET state = 'READY', error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE attempt_id = ? AND run_id = ? AND project_id = ?
                    AND canonical_source_root = ? AND state = 'PREPARING'
                """,
                (
                    attempt_id,
                    run_id,
                    self.context.project_id,
                    self.context.canonical_source_root,
                ),
            )
            if result.rowcount != 1:
                raise StateAuthorityError("attempt journal is not PREPARING")
            self._insert_snapshot(connection, run_id, state)

    def record_preparation_failure(
        self, run_id: int, attempt_id: int, error: str
    ) -> None:
        with self.transaction() as connection:
            self._require_running_run(connection, run_id)
            result = connection.execute(
                """
                UPDATE file_operation_journal
                SET error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE attempt_id = ? AND run_id = ? AND project_id = ?
                    AND canonical_source_root = ? AND state = 'PREPARING'
                """,
                (
                    error,
                    attempt_id,
                    run_id,
                    self.context.project_id,
                    self.context.canonical_source_root,
                ),
            )
            if result.rowcount != 1:
                raise StateAuthorityError("attempt journal is not PREPARING")
            connection.execute(
                """
                UPDATE attempts SET status = ?, failure_reason = ?,
                    finished_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = ?
                """,
                (
                    AttemptStatus.BLOCKED.value,
                    error,
                    attempt_id,
                    AttemptStatus.IN_PROGRESS.value,
                ),
            )

    def get_journal_for_attempt(
        self, attempt_id: int
    ) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM file_operation_journal
                WHERE attempt_id = ? AND project_id = ?
                    AND canonical_source_root = ?
                """,
                (
                    attempt_id,
                    self.context.project_id,
                    self.context.canonical_source_root,
                ),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_recovery_journals(
        self, run_id: int | None = None
    ) -> list[dict[str, object]]:
        run_filter = "" if run_id is None else "AND run_id = ?"
        parameters: list[object] = [
            self.context.project_id,
            self.context.canonical_source_root,
        ]
        if run_id is not None:
            parameters.append(run_id)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM file_operation_journal
                WHERE project_id = ? AND canonical_source_root = ?
                    AND state NOT IN ('COMMITTED', 'ROLLED_BACK')
                    {run_filter}
                ORDER BY id
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def record_recovery_failure(self, journal_id: int, error: str) -> None:
        with self.transaction() as connection:
            result = connection.execute(
                """
                UPDATE file_operation_journal
                SET state = 'RECOVERY_FAILED', error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND project_id = ? AND canonical_source_root = ?
                    AND state NOT IN ('COMMITTED', 'ROLLED_BACK')
                """,
                (
                    error,
                    journal_id,
                    self.context.project_id,
                    self.context.canonical_source_root,
                ),
            )
            if result.rowcount != 1:
                raise StateAuthorityError("recovery journal is not pending")

    def complete_recovery(
        self, journal_id: int, *, committed: bool, note: str
    ) -> None:
        final_journal = "COMMITTED" if committed else "ROLLED_BACK"
        attempt_status = (
            AttemptStatus.PASS if committed else AttemptStatus.BLOCKED
        )
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT run_id, attempt_id FROM file_operation_journal
                WHERE id = ? AND project_id = ? AND canonical_source_root = ?
                    AND state NOT IN ('COMMITTED', 'ROLLED_BACK')
                """,
                (
                    journal_id,
                    self.context.project_id,
                    self.context.canonical_source_root,
                ),
            ).fetchone()
            if row is None:
                raise StateAuthorityError("recovery journal is not pending")
            run_id = int(row["run_id"])
            attempt_id = int(row["attempt_id"])
            self._require_run(connection, run_id)
            connection.execute(
                """
                UPDATE file_operation_journal SET state = ?, error = NULL,
                    updated_at = CURRENT_TIMESTAMP WHERE id = ?
                """,
                (final_journal, journal_id),
            )
            connection.execute(
                """
                UPDATE attempts SET status = ?, failure_reason = ?,
                    finished_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'IN_PROGRESS'
                """,
                (
                    attempt_status.value,
                    None if committed else note,
                    attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE agent_runs SET attempt_id = NULL, checkpoint_id = NULL,
                    recovery_note = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND project_id = ? AND canonical_source_root = ?
                """,
                (
                    note,
                    run_id,
                    self.context.project_id,
                    self.context.canonical_source_root,
                ),
            )
            latest = connection.execute(
                """
                SELECT * FROM agent_state_snapshots
                WHERE run_id = ? AND project_id = ? AND canonical_source_root = ?
                ORDER BY id DESC LIMIT 1
                """,
                (
                    run_id,
                    self.context.project_id,
                    self.context.canonical_source_root,
                ),
            ).fetchone()
            if latest is not None:
                payload = json.loads(str(latest["state_json"]))
                payload["attempt_id"] = None
                payload["checkpoint_id"] = None
                connection.execute(
                    """
                    INSERT INTO agent_state_snapshots (
                        run_id, project_id, canonical_source_root, phase,
                        plan_id, task_id, step_id, attempt_id, checkpoint_id,
                        completion, state_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        run_id,
                        self.context.project_id,
                        self.context.canonical_source_root,
                        latest["phase"],
                        latest["plan_id"],
                        latest["task_id"],
                        latest["step_id"],
                        latest["completion"],
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )

    def begin_attempt_finalization(
        self, run_id: int, attempt_id: int, *, commit: bool
    ) -> None:
        target = "FINALIZING_COMMIT" if commit else "FINALIZING_ROLLBACK"
        with self.transaction() as connection:
            self._require_running_run(connection, run_id)
            result = connection.execute(
                """
                UPDATE file_operation_journal SET state = ?, error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE attempt_id = ? AND run_id = ? AND project_id = ?
                    AND canonical_source_root = ? AND state = 'READY'
                """,
                (
                    target,
                    attempt_id,
                    run_id,
                    self.context.project_id,
                    self.context.canonical_source_root,
                ),
            )
            if result.rowcount != 1:
                raise StateAuthorityError("attempt journal is not READY")

    def record_finalization_failure(
        self, run_id: int, attempt_id: int, error: str
    ) -> None:
        with self.transaction() as connection:
            self._require_running_run(connection, run_id)
            result = connection.execute(
                """
                UPDATE file_operation_journal
                SET state = 'RECOVERY_FAILED', error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE attempt_id = ? AND run_id = ? AND project_id = ?
                    AND canonical_source_root = ?
                    AND state IN ('FINALIZING_COMMIT', 'FINALIZING_ROLLBACK')
                """,
                (
                    error,
                    attempt_id,
                    run_id,
                    self.context.project_id,
                    self.context.canonical_source_root,
                ),
            )
            if result.rowcount != 1:
                raise StateAuthorityError("attempt is not being finalized")

    def complete_attempt(
        self,
        run_id: int,
        attempt_id: int,
        state: AgentState,
        *,
        status: AttemptStatus,
        failure_reason: str | None = None,
    ) -> None:
        if status is AttemptStatus.IN_PROGRESS:
            raise ValueError("attempt completion status cannot be IN_PROGRESS")
        journal_state = (
            "COMMITTED" if status is AttemptStatus.PASS else "ROLLED_BACK"
        )
        expected = (
            "FINALIZING_COMMIT"
            if status is AttemptStatus.PASS
            else "FINALIZING_ROLLBACK"
        )
        old_attempt = state.attempt_id
        old_checkpoint = state.checkpoint_id
        state.attempt_id = None
        state.checkpoint_id = None
        try:
            with self.transaction() as connection:
                self._require_running_run(connection, run_id)
                journal = connection.execute(
                    """
                    UPDATE file_operation_journal SET state = ?, error = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE attempt_id = ? AND run_id = ? AND project_id = ?
                        AND canonical_source_root = ? AND state = ?
                    """,
                    (
                        journal_state,
                        attempt_id,
                        run_id,
                        self.context.project_id,
                        self.context.canonical_source_root,
                        expected,
                    ),
                )
                if journal.rowcount != 1:
                    raise StateAuthorityError(
                        f"attempt journal is not {expected}"
                    )
                attempt = connection.execute(
                    """
                    UPDATE attempts SET status = ?, failure_reason = ?,
                        finished_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 'IN_PROGRESS'
                    """,
                    (status.value, failure_reason, attempt_id),
                )
                if attempt.rowcount != 1:
                    raise StateAuthorityError("attempt is not IN_PROGRESS")
                connection.execute(
                    """
                    UPDATE agent_runs SET attempt_id = NULL, checkpoint_id = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND project_id = ? AND canonical_source_root = ?
                    """,
                    (
                        run_id,
                        self.context.project_id,
                        self.context.canonical_source_root,
                    ),
                )
                self._insert_snapshot(connection, run_id, state)
        except Exception:
            state.attempt_id = old_attempt
            state.checkpoint_id = old_checkpoint
            raise

    def consume_budget(
        self, run_id: int, scope: str, target_id: int, *, limit: int
    ) -> int:
        normalized = scope.upper()
        if normalized not in {"TASK", "STEP"}:
            raise ValueError("budget scope must be TASK or STEP")
        if limit <= 0:
            raise ValueError("budget limit must be positive")
        with self.transaction() as connection:
            self._require_running_run(connection, run_id)
            connection.execute(
                """
                INSERT INTO budget_ledger (
                    run_id, project_id, canonical_source_root, scope,
                    target_id, consumed, limit_value
                ) VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(run_id, scope, target_id) DO UPDATE SET
                    consumed = consumed + 1,
                    limit_value = excluded.limit_value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    run_id,
                    self.context.project_id,
                    self.context.canonical_source_root,
                    normalized,
                    target_id,
                    limit,
                ),
            )
            row = connection.execute(
                """
                SELECT consumed FROM budget_ledger
                WHERE run_id = ? AND scope = ? AND target_id = ?
                    AND project_id = ? AND canonical_source_root = ?
                """,
                (
                    run_id,
                    normalized,
                    target_id,
                    self.context.project_id,
                    self.context.canonical_source_root,
                ),
            ).fetchone()
            return int(row["consumed"])

    def get_budget(
        self, run_id: int, scope: str, target_id: int
    ) -> dict[str, int] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT consumed, limit_value FROM budget_ledger
                WHERE run_id = ? AND scope = ? AND target_id = ?
                    AND project_id = ? AND canonical_source_root = ?
                """,
                (
                    run_id,
                    scope.upper(),
                    target_id,
                    self.context.project_id,
                    self.context.canonical_source_root,
                ),
            ).fetchone()
        if row is None:
            return None
        return {"consumed": int(row["consumed"]), "limit": int(row["limit_value"])}

    def finish_terminal(
        self,
        run_id: int,
        state: AgentState,
        *,
        run_status: str,
        plan_status: PlanStatus | None,
        session_id: str | None = None,
        session_status: SessionStatus | None = None,
        session_version: int | None = None,
    ) -> int | None:
        if run_status not in {"DONE", "FAILED", "INTERRUPTED"}:
            raise ValueError(f"invalid terminal run status: {run_status}")
        if (state.plan_id is None) != (plan_status is None):
            raise ValueError("plan id and terminal plan status must match")
        if (session_id is None) != (session_status is None):
            raise ValueError("session id and status must be supplied together")
        if session_id is not None and session_version is None:
            raise ValueError("session version is required")
        old_cursor = (
            state.active_task_id,
            state.active_step_id,
            state.attempt_id,
            state.checkpoint_id,
        )
        state.active_task_id = None
        state.active_step_id = None
        state.attempt_id = None
        state.checkpoint_id = None
        try:
            with self.transaction() as connection:
                self._require_running_run(connection, run_id)
                if state.plan_id is not None:
                    if not owns_plan(connection, self.context, state.plan_id):
                        raise StateAuthorityError(
                            f"plan {state.plan_id} does not belong to the current project"
                        )
                    plan = connection.execute(
                        """
                        UPDATE plans SET status = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND project_id = ? AND canonical_source_root = ?
                        """,
                        (
                            plan_status.value,
                            state.plan_id,
                            self.context.project_id,
                            self.context.canonical_source_root,
                        ),
                    )
                    if plan.rowcount != 1:
                        raise StateAuthorityError("terminal plan update failed")
                connection.execute(
                    """
                    UPDATE agent_runs SET status = ?, phase = ?, plan_id = ?,
                        task_id = NULL, step_id = NULL, attempt_id = NULL,
                        checkpoint_id = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND project_id = ? AND canonical_source_root = ?
                        AND status = 'RUNNING'
                    """,
                    (
                        run_status,
                        state.phase.value,
                        state.plan_id,
                        run_id,
                        self.context.project_id,
                        self.context.canonical_source_root,
                    ),
                )
                self._insert_snapshot(connection, run_id, state)
                new_version = None
                if session_id is not None:
                    result = connection.execute(
                        """
                        UPDATE agent_sessions SET status = ?, active_run_id = NULL,
                            version = version + 1, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND project_id = ? AND canonical_source_root = ?
                            AND version = ? AND status = 'RUNNING'
                        """,
                        (
                            session_status.value,
                            session_id,
                            self.context.project_id,
                            self.context.canonical_source_root,
                            session_version,
                        ),
                    )
                    if result.rowcount != 1:
                        raise StateAuthorityError(
                            "session was changed by another writer"
                        )
                    new_version = session_version + 1
                return new_version
        except Exception:
            (
                state.active_task_id,
                state.active_step_id,
                state.attempt_id,
                state.checkpoint_id,
            ) = old_cursor
            raise
