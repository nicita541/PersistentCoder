from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from app.sandbox.gitignore import GitIgnoreMatcher
from app.sandbox.limits import DEFAULT_LIMITS, LimitExceeded, SandboxLimits
from app.sandbox.project_path import ProjectPath
from app.sandbox.protected_paths import ProtectedPathPolicy


class SnapshotError(RuntimeError):
    pass


class SnapshotLinkError(SnapshotError):
    pass


class SnapshotLimitError(LimitExceeded):
    pass


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    path: ProjectPath
    size: int
    sha256: str
    source_path: Path


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    source_root: Path
    entries: tuple[SnapshotEntry, ...]
    total_bytes: int
    sha256: str

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(entry.path.value for entry in self.entries)

    @classmethod
    def build(
        cls,
        source_root: str | Path,
        *,
        limits: SandboxLimits = DEFAULT_LIMITS,
        policy: ProtectedPathPolicy | None = None,
    ) -> "SnapshotManifest":
        root = Path(source_root).resolve(strict=True)
        if not root.is_dir():
            raise SnapshotError(f"snapshot source is not a directory: {root}")

        protected = policy or ProtectedPathPolicy()
        ignored = GitIgnoreMatcher.from_project(root)
        entries: list[SnapshotEntry] = []
        total_bytes = 0

        def walk(directory: Path, prefix: tuple[str, ...] = ()) -> None:
            nonlocal total_bytes
            try:
                children = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError as error:
                raise SnapshotError(f"cannot scan snapshot directory: {directory}") from error

            for child in children:
                project_path = ProjectPath.parse("/".join((*prefix, child.name)))
                decision = protected.classify(project_path)
                if not decision.included:
                    continue

                try:
                    metadata = child.stat(follow_symlinks=False)
                except OSError as error:
                    raise SnapshotError(
                        f"cannot inspect snapshot entry: {project_path.value}"
                    ) from error

                if child.is_symlink() or _is_reparse(metadata):
                    raise SnapshotLinkError(
                        f"links and reparse points are unsupported: {project_path.value}"
                    )

                is_directory = child.is_dir(follow_symlinks=False)
                if ignored.is_ignored(project_path, is_dir=is_directory):
                    continue

                if is_directory:
                    walk(Path(child.path), (*prefix, child.name))
                    continue
                if not child.is_file(follow_symlinks=False):
                    raise SnapshotError(
                        f"unsupported snapshot entry: {project_path.value}"
                    )

                size = int(metadata.st_size)
                if size > limits.max_snapshot_file_bytes:
                    raise SnapshotLimitError(
                        f"snapshot file exceeds limit: {project_path.value} "
                        f"({size} > {limits.max_snapshot_file_bytes})"
                    )
                if len(entries) + 1 > limits.max_snapshot_files:
                    raise SnapshotLimitError(
                        "snapshot file count exceeds limit "
                        f"({len(entries) + 1} > {limits.max_snapshot_files})"
                    )
                if total_bytes + size > limits.max_snapshot_bytes:
                    raise SnapshotLimitError(
                        "snapshot total bytes exceed limit "
                        f"({total_bytes + size} > {limits.max_snapshot_bytes})"
                    )

                source = Path(child.path)
                entries.append(
                    SnapshotEntry(
                        path=project_path,
                        size=size,
                        sha256=_sha256_file(source),
                        source_path=source,
                    )
                )
                total_bytes += size

        walk(root)
        entries.sort(key=lambda entry: entry.path.value)
        payload = [
            {"path": entry.path.value, "size": entry.size, "sha256": entry.sha256}
            for entry in entries
        ]
        digest = hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        return cls(root, tuple(entries), total_bytes, digest)

    def materialize(self, destination: str | Path) -> None:
        target_root = Path(destination)
        if target_root.exists() and any(target_root.iterdir()):
            raise SnapshotError(f"snapshot destination is not empty: {target_root}")
        target_root.mkdir(parents=True, exist_ok=True)

        for entry in self.entries:
            try:
                metadata = entry.source_path.stat(follow_symlinks=False)
                if entry.source_path.is_symlink() or _is_reparse(metadata):
                    raise SnapshotLinkError(
                        f"snapshot source became a link: {entry.path.value}"
                    )
                content = entry.source_path.read_bytes()
            except SnapshotLinkError:
                raise
            except OSError as error:
                raise SnapshotError(
                    f"cannot materialize snapshot entry: {entry.path.value}"
                ) from error

            digest = hashlib.sha256(content).hexdigest()
            if len(content) != entry.size or digest != entry.sha256:
                raise SnapshotError(
                    f"snapshot source changed during copy: {entry.path.value}"
                )
            target = target_root / Path(*entry.path.value.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
