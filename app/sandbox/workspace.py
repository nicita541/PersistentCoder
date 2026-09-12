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

    @classmethod
    def create(
        cls,
        *,
        project_root: str | Path | None = None,
        session_id: str | None = None,
        sandbox_root: str | Path | None = None,
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
        )

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
        changed = self.changed_files()

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

        patch_path.write_text(
            "".join(lines),
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

