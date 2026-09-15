from __future__ import annotations

import uuid
import shutil
from collections.abc import Callable
from pathlib import Path

from app.agent.state import AgentState
from app.tasks.models import AttemptStatus, AttemptTargetType
from app.tasks.unit_of_work import RuntimeUnitOfWork
from app.sandbox.limits import DEFAULT_LIMITS
from app.sandbox.snapshot import SnapshotManifest
from app.sandbox.protected_paths import PathDecision, ProtectedPathPolicy
from app.sandbox.project_path import ProjectPath


class AttemptPreparationError(RuntimeError):
    pass


class AttemptFinalizationError(RuntimeError):
    pass


class AttemptRecoveryError(RuntimeError):
    pass


class _DirectCheckpointPolicy:
    def __init__(self, excluded: frozenset[str]) -> None:
        self.excluded = excluded
        self.base = ProtectedPathPolicy()

    def classify(self, path: ProjectPath) -> PathDecision:
        if path.value.casefold() in self.excluded:
            return PathDecision(False, "runtime database")
        return self.base.classify(path)


class DirectWorkspaceCheckpoints:
    """External checkpoints for the explicit in-place workspace mode."""

    def __init__(
        self,
        workspace_root: str | Path,
        checkpoints_root: str | Path,
        *,
        session_id: str,
        preserve: tuple[str | Path, ...] = (),
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.checkpoints_root = Path(checkpoints_root).resolve()
        self.session_id = session_id
        self.limits = DEFAULT_LIMITS
        preserved = {Path(item).resolve() for item in preserve}
        self.preserve = preserved | {
            Path(str(item) + suffix).resolve()
            for item in preserved
            for suffix in ("-wal", "-shm")
        }
        excluded: set[str] = set()
        for path in self.preserve:
            try:
                excluded.add(
                    path.relative_to(self.workspace_root).as_posix().casefold()
                )
            except ValueError:
                continue
        self.policy = _DirectCheckpointPolicy(frozenset(excluded))
        self.protected_roots: set[Path] = set()
        if self.checkpoints_root.is_relative_to(self.workspace_root):
            first = self.checkpoints_root.relative_to(
                self.workspace_root
            ).parts[0]
            self.protected_roots.add((self.workspace_root / first).resolve())
        self.checkpoints_root.mkdir(parents=True, exist_ok=True)

    def _target(self, label: str) -> Path:
        allowed = "abcdefghijklmnopqrstuvwxyz0123456789-_"
        if not label or any(ch not in allowed for ch in label.lower()):
            raise ValueError("invalid checkpoint label")
        return self.checkpoints_root / label

    def checkpoint(self, label: str) -> Path:
        target = self._target(label)
        if target.exists():
            shutil.rmtree(target)
        SnapshotManifest.build(
            self.workspace_root, limits=self.limits, policy=self.policy
        ).materialize(target)
        for path in self.preserve:
            try:
                relative = path.relative_to(self.workspace_root)
            except ValueError:
                continue
            copied = target / relative
            if copied.is_dir():
                shutil.rmtree(copied)
            elif copied.exists():
                copied.unlink()
        return target

    def has_checkpoint(self, label: str) -> bool:
        return self._target(label).is_dir()

    def _clear_workspace(self, directory: Path) -> None:
        for child in list(directory.iterdir()):
            resolved = child.resolve()
            if resolved in self.preserve or resolved in self.protected_roots:
                continue
            if child.is_dir() and any(
                path.is_relative_to(resolved) for path in self.preserve
            ):
                self._clear_workspace(child)
                try:
                    child.rmdir()
                except OSError:
                    pass
            elif child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    def rollback(self, label: str) -> bool:
        source = self._target(label)
        if not source.is_dir():
            return False
        manifest = SnapshotManifest.build(source, limits=self.limits)
        restore_stage = self.checkpoints_root.parent / f"restore-{uuid.uuid4().hex}"
        self._clear_workspace(self.workspace_root)
        try:
            manifest.materialize(restore_stage)
            shutil.copytree(
                restore_stage,
                self.workspace_root,
                dirs_exist_ok=True,
            )
        finally:
            if restore_stage.exists():
                shutil.rmtree(restore_stage)
        return True

    def commit(self, label: str) -> None:
        target = self._target(label)
        if target.exists():
            shutil.rmtree(target)


class DurableAttemptCoordinator:
    """Coordinates the SQLite intent record with a filesystem checkpoint."""

    def __init__(
        self,
        *,
        authority: RuntimeUnitOfWork,
        workspace,
        run_id: Callable[[], int | None],
    ) -> None:
        self.authority = authority
        self.workspace = workspace
        self._run_id = run_id

    def _require_run_id(self) -> int:
        run_id = self._run_id()
        if run_id is None:
            raise AttemptPreparationError("no active durable run")
        return run_id

    def begin(self, state: AgentState, task, step) -> None:
        run_id = self._require_run_id()
        checkpoint_id = f"attempt-{uuid.uuid4().hex}"
        target_type = (
            AttemptTargetType.STEP if step is not None else AttemptTargetType.TASK
        )
        target_id = step.id if step is not None else task.id
        approach = state.repair.approach or state.repair.strategy or None
        attempt_id = self.authority.prepare_attempt(
            run_id,
            state,
            target_type=target_type,
            target_id=target_id,
            approach=approach,
            sandbox_session_id=str(self.workspace.session_id),
            checkpoint_id=checkpoint_id,
        )
        try:
            self.workspace.checkpoint(checkpoint_id)
        except Exception as error:
            self.authority.record_preparation_failure(
                run_id, attempt_id, str(error)
            )
            raise AttemptPreparationError(
                f"checkpoint creation failed: {error}"
            ) from error
        try:
            self.authority.mark_checkpoint_ready(run_id, attempt_id, state)
        except Exception as error:
            try:
                restored = self.workspace.rollback(checkpoint_id)
                if not restored:
                    raise RuntimeError("checkpoint disappeared before rollback")
                self.workspace.commit(checkpoint_id)
            except Exception as rollback_error:
                self.authority.record_preparation_failure(
                    run_id,
                    attempt_id,
                    f"state persistence failed: {error}; rollback failed: {rollback_error}",
                )
                raise AttemptPreparationError(
                    "checkpoint state persistence and rollback failed: "
                    f"{error}; {rollback_error}"
                ) from rollback_error
            self.authority.record_preparation_failure(
                run_id, attempt_id, f"checkpoint state persistence failed: {error}"
            )
            raise AttemptPreparationError(
                f"checkpoint state persistence failed: {error}"
            ) from error

    def finish(
        self,
        state: AgentState,
        *,
        ok: bool,
        status: str,
        reason: str = "",
    ) -> None:
        run_id = self._require_run_id()
        attempt_id = state.attempt_id
        checkpoint_id = state.checkpoint_id
        if attempt_id is None or not checkpoint_id:
            raise AttemptFinalizationError(
                "durable attempt and checkpoint are required"
            )
        self.authority.begin_attempt_finalization(
            run_id, attempt_id, commit=ok
        )
        try:
            if ok:
                self.workspace.commit(checkpoint_id)
            else:
                restored = self.workspace.rollback(checkpoint_id)
                if not restored:
                    raise RuntimeError(
                        f"checkpoint is missing: {checkpoint_id}"
                    )
                self.workspace.commit(checkpoint_id)
        except Exception as error:
            self.authority.record_finalization_failure(
                run_id, attempt_id, str(error)
            )
            raise AttemptFinalizationError(
                f"attempt filesystem finalization failed: {error}"
            ) from error
        attempt_status = (
            AttemptStatus.PASS
            if ok
            else (
                AttemptStatus.BLOCKED
                if status == "BLOCKED"
                else AttemptStatus.FAILED
            )
        )
        self.authority.complete_attempt(
            run_id,
            attempt_id,
            state,
            status=attempt_status,
            failure_reason=None if ok else (reason or "attempt failed"),
        )

    def recover(self, journal: dict[str, object]) -> dict[str, object]:
        journal_id = int(journal["id"])
        checkpoint_id = str(journal["checkpoint_id"])
        journal_state = str(journal["state"])
        checkpoint_exists = self.workspace.has_checkpoint(checkpoint_id)
        try:
            if journal_state == "FINALIZING_COMMIT":
                if checkpoint_exists:
                    self.workspace.commit(checkpoint_id)
                self.authority.complete_recovery(
                    journal_id,
                    committed=True,
                    note="completed interrupted checkpoint commit",
                )
                return {"journal_id": journal_id, "committed": True}

            if not checkpoint_exists:
                if journal_state in {"PREPARING", "FINALIZING_ROLLBACK"}:
                    self.authority.complete_recovery(
                        journal_id,
                        committed=False,
                        note=(
                            "attempt stopped before checkpoint became ready"
                            if journal_state == "PREPARING"
                            else "completed interrupted checkpoint rollback"
                        ),
                    )
                    return {"journal_id": journal_id, "rolled_back": False}
                raise AttemptRecoveryError(
                    f"recovery checkpoint is missing: {checkpoint_id}"
                )

            restored = self.workspace.rollback(checkpoint_id)
            if not restored:
                raise AttemptRecoveryError(
                    f"recovery checkpoint is missing: {checkpoint_id}"
                )
            self.workspace.commit(checkpoint_id)
            self.authority.complete_recovery(
                journal_id,
                committed=False,
                note=f"rolled back interrupted attempt to {checkpoint_id}",
            )
            return {"journal_id": journal_id, "rolled_back": True}
        except Exception as error:
            self.authority.record_recovery_failure(journal_id, str(error))
            if isinstance(error, AttemptRecoveryError):
                raise
            raise AttemptRecoveryError(
                f"attempt recovery failed: {error}"
            ) from error

    def recover_pending(self, run_id: int | None = None) -> list[dict[str, object]]:
        return [
            self.recover(journal)
            for journal in self.authority.list_recovery_journals(run_id)
        ]
