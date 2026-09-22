from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from enum import Enum
from pathlib import Path
from typing import Iterator

from app.agent.state import AgentState
from app.apply.manifest import PatchManifest
from app.apply.store import insert_manifest, read_manifest
from app.agent.session import SessionStatus
from app.tasks.migrations import migrate
from app.tasks.models import (
    AttemptStatus,
    AttemptTargetType,
    PlanStatus,
    VerificationTargetType,
)
from app.tasks.verification_context import VerificationContext
from app.tasks.store_context import StoreContext, owns_plan, owns_step, owns_task


class StateAuthorityError(RuntimeError):
    pass


class ConcurrentWriterError(StateAuthorityError):
    pass


_APPLY_TRANSITIONS = {
    "PREPARING": frozenset({"APPLYING", "ROLLED_BACK", "CONFLICT", "RECOVERY_FAILED"}),
    "APPLYING": frozenset({"COMMITTED", "ROLLED_BACK", "RECOVERY_FAILED"}),
    "RECOVERY_FAILED": frozenset({"ROLLED_BACK"}),
}


def _json_default(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


class RuntimeUnitOfWork:
    """Project-scoped authority for mandatory durable transitions."""

    def __init__(
        self,
        context: StoreContext | str | Path,
        *,
        timeout: float = 5.0,
    ) -> None:
        self.context = context if isinstance(context, StoreContext) else None
        self.database_path = (
            context.database_path if isinstance(context, StoreContext) else Path(context)
        )
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
        if self.context is None:
            raise StateAuthorityError("runtime transitions require a StoreContext")
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

    def finish_verification(
        self,
        *,
        target_type: VerificationTargetType,
        target_id: int,
        verification_status: str,
        evidence: list[str],
        reason: str | None,
        context: VerificationContext | None,
        target_status: str,
        failure_reason: str | None = None,
    ) -> int:
        """Persist evidence and its Task/Step transition atomically."""

        if verification_status == "PASS" and context is None:
            raise StateAuthorityError(
                "PASS requires revision-bound verification context"
            )

        evidence_json = json.dumps(evidence, ensure_ascii=False)
        context_json = (
            json.dumps(context.to_dict(), sort_keys=True)
            if context is not None
            else None
        )
        with self.transaction() as connection:
            if target_type is VerificationTargetType.TASK:
                if not owns_task(connection, self.context, target_id):
                    raise StateAuthorityError(f"unknown task: {target_id}")
                row = connection.execute(
                    "SELECT status FROM tasks WHERE id = ?", (target_id,)
                ).fetchone()
                if row is None or str(row["status"]) != "VERIFYING":
                    raise StateAuthorityError(
                        "task must be VERIFYING before verification completion"
                    )
                cursor = connection.execute(
                    """
                    INSERT INTO verifications (
                        task_id, status, reason, evidence_json, context_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        target_id,
                        verification_status,
                        reason,
                        evidence_json,
                        context_json,
                    ),
                )
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, verification_status = ?,
                        verification_evidence_json = ?,
                        finished_at = CASE
                            WHEN ? = 'DONE' THEN CURRENT_TIMESTAMP
                            ELSE finished_at
                        END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        target_status,
                        verification_status,
                        evidence_json,
                        target_status,
                        target_id,
                    ),
                )
            else:
                if not owns_step(connection, self.context, target_id):
                    raise StateAuthorityError(f"unknown step: {target_id}")
                row = connection.execute(
                    "SELECT status FROM steps WHERE id = ?", (target_id,)
                ).fetchone()
                if row is None or str(row["status"]) != "VERIFYING":
                    raise StateAuthorityError(
                        "step must be VERIFYING before verification completion"
                    )
                cursor = connection.execute(
                    """
                    INSERT INTO verifications (
                        step_id, status, reason, evidence_json, context_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        target_id,
                        verification_status,
                        reason,
                        evidence_json,
                        context_json,
                    ),
                )
                connection.execute(
                    """
                    UPDATE steps
                    SET status = ?, verification_status = ?,
                        verification_evidence_json = ?, failure_reason = ?,
                        finished_at = CASE
                            WHEN ? = 'DONE' THEN CURRENT_TIMESTAMP
                            ELSE finished_at
                        END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        target_status,
                        verification_status,
                        evidence_json,
                        failure_reason,
                        target_status,
                        target_id,
                    ),
                )
            return int(cursor.lastrowid)

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

    def prepare_apply(
        self,
        *,
        session_id: str,
        manifest_id: str,
        session_version: int,
        staging_id: str,
        backup_id: str,
    ) -> tuple[int, int]:
        """Reserve project apply ownership before any source filesystem changes.

        Artifact IDs name children of framework-owned project staging/backup
        directories. Callers must not interpret them as caller-supplied paths.
        """
        for identifier in (staging_id, backup_id):
            if not isinstance(identifier, str) or re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", identifier) is None:
                raise ValueError("apply artifact identifier must be project-local")
        if self.context is None:
            raise StateAuthorityError("apply requires a StoreContext")
        with self.transaction() as connection:
            manifest = read_manifest(connection, self.context, manifest_id)
            if manifest is None:
                raise StateAuthorityError("manifest does not belong to the current project")
            result = connection.execute(
                """UPDATE agent_sessions SET version = version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND project_id = ? AND canonical_source_root = ?
                    AND sandbox_session_id = ? AND patch_manifest_id = ?
                    AND status = 'DIRTY_VERIFIED' AND active_run_id IS NULL
                    AND version = ?""",
                (session_id, self.context.project_id, self.context.canonical_source_root,
                 manifest.session_id, manifest_id, session_version),
            )
            if result.rowcount != 1:
                raise StateAuthorityError("apply session binding or version does not match")
            cursor = connection.execute(
                """INSERT INTO apply_journal (
                    project_id, canonical_source_root, agent_session_id, manifest_id,
                    state, staging_id, backup_id
                ) VALUES (?, ?, ?, ?, 'PREPARING', ?, ?)""",
                (self.context.project_id, self.context.canonical_source_root,
                 session_id, manifest_id, staging_id, backup_id),
            )
            return int(cursor.lastrowid), session_version + 1

    def get_apply_session(
        self, session_id: str, manifest_id: str
    ) -> dict[str, object] | None:
        """Read the complete owning binding without reserving or changing it."""
        if self.context is None:
            raise StateAuthorityError("apply requires a StoreContext")
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT s.* FROM agent_sessions s JOIN patch_manifests m
                    ON m.agent_session_id = s.id AND m.project_id = s.project_id
                    AND m.canonical_source_root = s.canonical_source_root
                    AND m.session_id = s.sandbox_session_id
                WHERE s.id = ? AND s.project_id = ? AND s.canonical_source_root = ?
                    AND s.patch_manifest_id = m.manifest_id AND m.manifest_id = ?""",
                (session_id, self.context.project_id, self.context.canonical_source_root, manifest_id),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def record_apply_recovery_failure(self, journal_id: int, error: str) -> None:
        """Keep a project reserved even when its session binding is damaged.

        This failure-only edge cannot publish source changes or mark a session
        applied. Recovery success still requires the normal session/journal CAS.
        """
        if self.context is None:
            raise StateAuthorityError("apply requires a StoreContext")
        with self.transaction() as connection:
            result = connection.execute(
                """UPDATE apply_journal SET state = 'RECOVERY_FAILED', error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND project_id = ? AND canonical_source_root = ?
                    AND state IN ('PREPARING', 'APPLYING', 'RECOVERY_FAILED')""",
                (error[:2000], journal_id, self.context.project_id, self.context.canonical_source_root),
            )
            if result.rowcount != 1:
                raise StateAuthorityError("pending apply journal does not belong to project")

    def transition_apply(
        self,
        journal_id: int,
        *,
        expected_state: str,
        target_state: str,
        session_version: int,
        error: str | None = None,
    ) -> int:
        """CAS a legal journal edge and the owning session in one transaction.

        Failed restoration stays pending as RECOVERY_FAILED and reserves the
        project until a recovery caller confirms ROLLED_BACK. Only COMMITTED
        moves the session to APPLIED; other outcomes retain its manifest.
        """
        if target_state not in _APPLY_TRANSITIONS.get(expected_state, ()):
            raise StateAuthorityError(f"illegal apply transition: {expected_state} -> {target_state}")
        if error is not None and not isinstance(error, str):
            raise ValueError("apply error must be text")
        if self.context is None:
            raise StateAuthorityError("apply requires a StoreContext")
        with self.transaction() as connection:
            journal = connection.execute(
                """SELECT j.*, m.session_id AS sandbox_session_id
                FROM apply_journal j JOIN patch_manifests m ON m.manifest_id = j.manifest_id
                WHERE j.id = ? AND j.project_id = ? AND j.canonical_source_root = ?
                    AND j.state = ?""",
                (journal_id, self.context.project_id, self.context.canonical_source_root, expected_state),
            ).fetchone()
            if journal is None:
                raise StateAuthorityError("apply journal state or project does not match")
            connection.execute(
                """UPDATE apply_journal SET state = ?, error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND state = ?""",
                (target_state, error[:2000] if error is not None else None, journal_id, expected_state),
            )
            result = connection.execute(
                """UPDATE agent_sessions SET status = ?, version = version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND project_id = ? AND canonical_source_root = ?
                    AND patch_manifest_id = ? AND sandbox_session_id = ?
                    AND version = ? AND status = 'DIRTY_VERIFIED'
                    AND active_run_id IS NULL""",
                ("APPLIED" if target_state == "COMMITTED" else "DIRTY_VERIFIED",
                 journal["agent_session_id"], self.context.project_id,
                 self.context.canonical_source_root, journal["manifest_id"],
                 journal["sandbox_session_id"], session_version),
            )
            if result.rowcount != 1:
                raise StateAuthorityError("apply session binding or version does not match")
            return session_version + 1

    def get_apply_journal(self, journal_id: int) -> dict[str, object] | None:
        if self.context is None:
            raise StateAuthorityError("apply requires a StoreContext")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM apply_journal WHERE id = ? AND project_id = ? AND canonical_source_root = ?",
                (journal_id, self.context.project_id, self.context.canonical_source_root),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def pending_apply_journals(self) -> list[dict[str, object]]:
        if self.context is None:
            raise StateAuthorityError("apply requires a StoreContext")
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT * FROM apply_journal WHERE project_id = ? AND canonical_source_root = ?
                    AND state IN ('PREPARING', 'APPLYING', 'RECOVERY_FAILED') ORDER BY id""",
                (self.context.project_id, self.context.canonical_source_root),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

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
        patch_manifest: PatchManifest | None = None,
    ) -> int | None:
        if run_status not in {"DONE", "FAILED", "INTERRUPTED"}:
            raise ValueError(f"invalid terminal run status: {run_status}")
        if (state.plan_id is None) != (plan_status is None):
            raise ValueError("plan id and terminal plan status must match")
        if (session_id is None) != (session_status is None):
            raise ValueError("session id and status must be supplied together")
        if session_id is not None and session_version is None:
            raise ValueError("session version is required")
        if patch_manifest is not None and (
            run_status != "DONE"
            or state.phase.value != "DONE"
            or state.completion != "DONE"
            or (plan_status is not None and plan_status is not PlanStatus.DONE)
            or session_status is not SessionStatus.DIRTY_VERIFIED
            or session_id is None
        ):
            raise ValueError("manifest binding requires a successful DIRTY_VERIFIED terminal session")
        if run_status != "DONE" and session_status is SessionStatus.DIRTY_VERIFIED:
            raise ValueError("failed terminal run cannot become DIRTY_VERIFIED")
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
                if patch_manifest is not None:
                    owner = connection.execute(
                        """SELECT s.id FROM agent_sessions s JOIN agent_runs r
                            ON r.id = s.active_run_id
                        WHERE s.id = ? AND s.project_id = ?
                            AND s.canonical_source_root = ? AND s.version = ?
                            AND s.status = 'RUNNING' AND s.active_run_id = ?
                            AND s.sandbox_session_id = ? AND r.sandbox_session_id = ?""",
                        (session_id, self.context.project_id, self.context.canonical_source_root,
                         session_version, run_id, patch_manifest.session_id, patch_manifest.session_id),
                    ).fetchone()
                    if owner is None:
                        raise StateAuthorityError("terminal session, sandbox or active run does not match")
                    insert_manifest(
                        connection, self.context, patch_manifest,
                        agent_session_id=session_id, allow_existing=False,
                    )
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
                            patch_manifest_id = COALESCE(?, patch_manifest_id),
                            version = version + 1, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND project_id = ? AND canonical_source_root = ?
                            AND version = ? AND status = 'RUNNING'
                        """,
                        (
                            session_status.value,
                            patch_manifest.manifest_id if patch_manifest is not None else None,
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
