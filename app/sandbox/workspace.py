from __future__ import annotations

import difflib
import hashlib
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.sandbox.paths import (
    PROJECT_ROOT,
    SANDBOX_PATCHES,
    SANDBOX_SESSIONS,
    SANDBOX_SNAPSHOTS,
)

from app.sandbox.limits import (
    DEFAULT_LIMITS,
    LimitExceeded,
)


IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "models",
        "data",
        ".sandbox",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "node_modules",
        ".idea",
        ".vscode",
    }
)

IGNORED_SUFFIXES = (".pyc", ".pyo")

# Patch safety: never emit these into a patch file.
PATCH_DENIED_PARTS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "models",
        "data",
        ".sandbox",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "node_modules",
        ".idea",
        ".vscode",
    }
)

PATCH_DENIED_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
)

PATCH_DENIED_NAME_MARKERS = (
    ".env",
    "credentials",
    "secret",
    "id_rsa",
    "id_ed25519",
)



def _project_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue

        relative = path.relative_to(root)

        if any(
            part in IGNORED_DIRECTORIES
            for part in relative.parts
        ):
            continue

        if path.suffix in IGNORED_SUFFIXES:
            continue

        yield path, relative


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(
            lambda: handle.read(65536),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def _safe_label(label: str) -> str:
    cleaned = "".join(
        ch if (ch.isalnum() or ch in "-_.") else "_"
        for ch in str(label)
    )

    cleaned = cleaned.strip("._")

    return (cleaned or "checkpoint")[:64]


def _copy_tree(
    source: Path,
    destination: Path,
) -> None:
    for path, relative in _project_files(source):
        target = destination / relative

        target.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        shutil.copy2(path, target)


class SandboxWorkspace:
    """
    Filesystem sandbox for the CodingAgent.

    Flow:

        F:\\PersistentCoder            (host project, read-only for agents)
            -> snapshot (baseline)
            -> sandbox session workspace (agents edit here)
            -> patch (.sandbox/patches)
            -> explicit apply back to the project

    Agents never receive the host project root as a write target.
    """

    def __init__(
        self,
        *,
        session_id: str,
        workspace_root: Path,
        baseline_root: Path,
        patch_root: Path,
        checkpoints_root: Path | None = None,
        limits=DEFAULT_LIMITS,
    ) -> None:
        self.session_id = session_id
        self.workspace_root = (
            Path(workspace_root).resolve()
        )
        self.baseline_root = (
            Path(baseline_root).resolve()
        )
        self.patch_root = (
            Path(patch_root).resolve()
        )
        self.checkpoints_root = (
            Path(checkpoints_root).resolve()
            if checkpoints_root is not None
            else (
                self.baseline_root.parent
                / f"{session_id}__checkpoints"
            )
        )
        self.limits = limits
        self._active_checkpoint: str | None = None

    @classmethod
    def create(
        cls,
        *,
        project_root: str | Path | None = None,
        session_id: str | None = None,
        sandbox_root: str | Path | None = None,
        limits=DEFAULT_LIMITS,
    ) -> "SandboxWorkspace":
        project_root = Path(
            project_root or PROJECT_ROOT
        ).resolve()

        session_id = (
            session_id
            or (
                datetime.now(timezone.utc).strftime(
                    "%Y%m%dT%H%M%SZ"
                )
                + "-"
                + uuid.uuid4().hex[:8]
            )
        )

        if sandbox_root is not None:
            sandbox_root = Path(sandbox_root)
            sessions = sandbox_root / "sessions"
            snapshots = (
                sandbox_root / "snapshots"
            )
            patches = sandbox_root / "patches"

        else:
            sessions = SANDBOX_SESSIONS
            snapshots = SANDBOX_SNAPSHOTS
            patches = SANDBOX_PATCHES

        baseline_root = snapshots / session_id

        workspace_root = (
            sessions / session_id / "workspace"
        )

        for directory in (
            baseline_root,
            workspace_root,
        ):
            if directory.exists():
                shutil.rmtree(directory)

        _copy_tree(project_root, baseline_root)
        _copy_tree(project_root, workspace_root)

        return cls(
            session_id=session_id,
            workspace_root=workspace_root,
            baseline_root=baseline_root,
            patch_root=patches,
            checkpoints_root=(
                snapshots
                / f"{session_id}__checkpoints"
            ),
            limits=limits,
        )

    # ==================================
    # TRANSACTIONAL CHECKPOINTS
    # ==================================

    def checkpoint(
        self,
        label: str,
    ) -> Path:
        """
        Snapshot the current workspace state.

        Returns the checkpoint directory. Overwrites any previous
        checkpoint with the same label.
        """

        if not label or not label.strip():
            raise ValueError("label is required")

        target = (
            self.checkpoints_root
            / _safe_label(label)
        )

        if target.exists():
            shutil.rmtree(target)

        _copy_tree(self.workspace_root, target)

        self._active_checkpoint = label

        return target

    def rollback(
        self,
        label: str | None = None,
    ) -> bool:
        """
        Restore the workspace to a checkpoint.

        Everything written since the checkpoint is discarded. Fail
        closed: if the checkpoint does not exist, nothing changes.
        """

        label = label or self._active_checkpoint

        if not label:
            return False

        source = (
            self.checkpoints_root
            / _safe_label(label)
        )

        if not source.exists():
            return False

        # Wipe current workspace, then restore the checkpoint.
        for child in list(
            self.workspace_root.iterdir()
        ):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

        _copy_tree(source, self.workspace_root)

        return True

    def commit(
        self,
        label: str | None = None,
    ) -> None:
        """Drop a checkpoint (the attempt is accepted)."""

        label = label or self._active_checkpoint

        if not label:
            return

        target = (
            self.checkpoints_root
            / _safe_label(label)
        )

        if target.exists():
            shutil.rmtree(target)

        if label == self._active_checkpoint:
            self._active_checkpoint = None

    def has_checkpoint(
        self,
        label: str,
    ) -> bool:
        return (
            self.checkpoints_root
            / _safe_label(label)
        ).exists()

    # ==================================
    # PATCH SAFETY
    # ==================================

    @staticmethod
    def is_patch_safe(relative: str) -> bool:
        """
        A changed file may enter a patch only if it is a normal,
        relative project source/artifact path.
        """

        if not relative or not relative.strip():
            return False

        text = relative.replace("\\", "/")

        if text.startswith("/") or ":" in text:
            return False

        parts = [
            part
            for part in text.split("/")
            if part
        ]

        if not parts:
            return False

        if any(part == ".." for part in parts):
            return False

        if any(
            part in PATCH_DENIED_PARTS
            for part in parts
        ):
            return False

        name = parts[-1].casefold()

        if name.endswith(PATCH_DENIED_SUFFIXES):
            return False

        if any(
            marker in name
            for marker in PATCH_DENIED_NAME_MARKERS
        ):
            return False

        return True


    # ==================================
    # DIFF / PATCH / APPLY
    # ==================================

    def _manifest(
        self,
        root: Path,
    ) -> dict[str, str]:
        return {
            relative.as_posix(): _hash_file(path)
            for path, relative in _project_files(
                root
            )
        }

    def changed_files(self) -> list[str]:
        baseline = self._manifest(
            self.baseline_root
        )
        current = self._manifest(
            self.workspace_root
        )

        changed: set[str] = set()

        for relative, digest in current.items():
            if baseline.get(relative) != digest:
                changed.add(relative)

        for relative in baseline:
            if relative not in current:
                changed.add(relative)

        return sorted(changed)

    def write_patch(
        self,
    ) -> Path | None:
        changed = [
            relative
            for relative in self.changed_files()
            if self.is_patch_safe(relative)
        ]

        if not changed:
            return None

        lines: list[str] = []

        for relative in changed:
            base_file = (
                self.baseline_root / relative
            )
            work_file = (
                self.workspace_root / relative
            )

            try:
                base_text = (
                    base_file.read_text(
                        encoding="utf-8"
                    ).splitlines(keepends=True)
                    if base_file.exists()
                    else []
                )

                work_text = (
                    work_file.read_text(
                        encoding="utf-8"
                    ).splitlines(keepends=True)
                    if work_file.exists()
                    else []
                )

            except (UnicodeDecodeError, OSError):
                continue

            lines.extend(
                difflib.unified_diff(
                    base_text,
                    work_text,
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                )
            )

        if not lines:
            return None

        self.patch_root.mkdir(
            parents=True,
            exist_ok=True,
        )

        patch_path = (
            self.patch_root
            / f"{self.session_id}.patch"
        )

        text = "".join(lines)

        encoded = text.encode("utf-8")

        if len(encoded) > self.limits.max_patch_bytes:
            raise LimitExceeded(
                "patch exceeds max_patch_bytes "
                f"({len(encoded)} > "
                f"{self.limits.max_patch_bytes})"
            )

        patch_path.write_text(
            text,
            encoding="utf-8",
        )

        return patch_path


    def apply_to_project(
        self,
        project_root: str | Path | None = None,
        *,
        paths: list[str] | None = None,
    ) -> list[str]:
        project_root = Path(
            project_root or PROJECT_ROOT
        ).resolve()

        targets = (
            list(paths)
            if paths is not None
            else self.changed_files()
        )

        applied: list[str] = []

        for relative in targets:
            source = (
                self.workspace_root / relative
            )
            destination = (
                project_root / relative
            ).resolve()

            if (
                destination != project_root
                and project_root
                not in destination.parents
            ):
                continue

            if not source.exists():
                continue

            destination.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            shutil.copy2(source, destination)

            applied.append(relative)

        return applied

