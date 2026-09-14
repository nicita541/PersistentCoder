from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.project_identity import ProjectIdentity
from app.sandbox.paths import (
    DATA_ROOT,
    PROJECT_ROOT,
    SANDBOX_ROOT,
)


class ProjectStorageError(RuntimeError):
    """Raised when framework-owned storage escapes its root."""


@dataclass(frozen=True, slots=True)
class ProjectStorage:
    identity: ProjectIdentity
    database_path: Path
    sandbox_root: Path

    @property
    def sessions_root(self) -> Path:
        return self.sandbox_root / "sessions"

    @property
    def snapshots_root(self) -> Path:
        return self.sandbox_root / "snapshots"

    @property
    def patches_root(self) -> Path:
        return self.sandbox_root / "patches"

    @property
    def logs_root(self) -> Path:
        return self.sandbox_root / "logs"

    @property
    def tmp_root(self) -> Path:
        return self.sandbox_root / "tmp"

    @classmethod
    def for_identity(
        cls,
        identity: ProjectIdentity,
    ) -> "ProjectStorage":
        return cls(
            identity=identity,
            database_path=(
                DATA_ROOT
                / "projects"
                / identity.project_id
                / "persistent_coder.db"
            ),
            sandbox_root=(
                SANDBOX_ROOT
                / "projects"
                / identity.project_id
            ),
        )

    def ensure_layout(self) -> None:
        directories = (
            self.database_path.parent,
            self.sessions_root,
            self.snapshots_root,
            self.patches_root,
            self.logs_root,
            self.tmp_root,
        )

        project_root = PROJECT_ROOT.resolve()

        for directory in directories:
            resolved = directory.resolve()
            if not resolved.is_relative_to(project_root):
                raise ProjectStorageError(
                    "Framework storage escaped the PersistentCoder root: "
                    f"{resolved}"
                )

        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
