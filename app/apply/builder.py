from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from app.apply.manifest import PatchEntry, PatchManifest, PatchOperation
from app.project_identity import ProjectIdentity
from app.sandbox.limits import DEFAULT_LIMITS, SandboxLimits
from app.sandbox.project_path import ProjectPath
from app.sandbox.protected_paths import ProtectedPathPolicy
from app.sandbox.snapshot import SnapshotLimitError


class ManifestBuildError(RuntimeError):
    """Raised when the two trees cannot produce a stable safe manifest."""


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


def _metadata_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        int(getattr(metadata, "st_dev", 0)),
        int(getattr(metadata, "st_ino", 0)),
        int(metadata.st_mode),
        int(metadata.st_size),
        int(getattr(metadata, "st_mtime_ns", 0)),
        int(getattr(metadata, "st_file_attributes", 0)),
    )


def _is_link(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata)


@dataclass(frozen=True, slots=True)
class _InventoryEntry:
    path: ProjectPath
    size: int
    sha256: str
    kind: str
    reviewable: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Inventory:
    entries: tuple[_InventoryEntry, ...]
    total_bytes: int
    sha256: str

    @property
    def by_path(self) -> dict[str, _InventoryEntry]:
        return {entry.path.value: entry for entry in self.entries}


class PatchManifestBuilder:
    def __init__(self, *, limits: SandboxLimits = DEFAULT_LIMITS) -> None:
        self.limits = limits
        self._protected_paths = ProtectedPathPolicy()

    def build(
        self,
        *,
        project_identity: ProjectIdentity,
        session_id: str,
        baseline_root: str | Path,
        workspace_root: str | Path,
        verification_id: str,
    ) -> PatchManifest:
        if not isinstance(project_identity, ProjectIdentity):
            raise ManifestBuildError("project_identity is required")
        try:
            actual_identity = ProjectIdentity.from_source_root(
                project_identity.canonical_source_root
            )
        except ValueError as error:
            raise ManifestBuildError("project identity is invalid") from error
        if actual_identity != project_identity:
            raise ManifestBuildError("project identity does not match source root")

        baseline_path = self._directory_root(baseline_root, "baseline")
        workspace_path = self._directory_root(workspace_root, "workspace")
        if baseline_path == workspace_path:
            raise ManifestBuildError("baseline and workspace must be different trees")

        baseline = self._inventory(baseline_path)
        workspace = self._inventory(workspace_path)
        entries = self._compare(baseline, workspace)
        patch_bytes = sum(
            entry.after_size or 0
            for entry in entries
            if entry.operation in {PatchOperation.ADD, PatchOperation.MODIFY}
        )
        if patch_bytes > self.limits.max_patch_bytes:
            raise ManifestBuildError(
                "patch exceeds max_patch_bytes "
                f"({patch_bytes} > {self.limits.max_patch_bytes})"
            )

        final_baseline = self._inventory(baseline_path)
        final_workspace = self._inventory(workspace_path)
        if baseline.sha256 != final_baseline.sha256:
            raise ManifestBuildError("baseline changed during manifest build")
        if workspace.sha256 != final_workspace.sha256:
            raise ManifestBuildError("workspace changed during manifest build")

        return PatchManifest.create(
            project_id=project_identity.project_id,
            canonical_source_root=str(project_identity.canonical_source_root),
            session_id=session_id,
            baseline_sha256=baseline.sha256,
            workspace_sha256=workspace.sha256,
            verification_id=verification_id,
            entries=entries,
        )

    def _directory_root(self, value: str | Path, label: str) -> Path:
        candidate = Path(os.path.abspath(Path(value)))
        try:
            before = os.stat(candidate, follow_symlinks=False)
            if _is_link(before):
                raise ManifestBuildError(f"{label} root cannot be a link")
            resolved = candidate.resolve(strict=True)
            after = os.stat(candidate, follow_symlinks=False)
        except ManifestBuildError:
            raise
        except (OSError, RuntimeError) as error:
            raise ManifestBuildError(f"{label} root cannot be resolved") from error
        if (
            _is_link(after)
            or _metadata_identity(before) != _metadata_identity(after)
            or resolved != candidate
        ):
            raise ManifestBuildError(f"{label} root changed or resolves through a link")
        if not stat.S_ISDIR(after.st_mode):
            raise ManifestBuildError(f"{label} root is not a directory")
        return candidate

    def _directory_metadata(self, path: Path) -> os.stat_result:
        try:
            metadata = os.stat(path, follow_symlinks=False)
        except OSError as error:
            raise ManifestBuildError(f"cannot inspect manifest directory: {path}") from error
        if _is_link(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ManifestBuildError(f"manifest directory changed or became a link: {path}")
        return metadata

    def _validate_directory_chain(
        self,
        chain: tuple[tuple[Path, os.stat_result], ...],
    ) -> None:
        for path, expected in chain:
            current = self._directory_metadata(path)
            if _metadata_identity(current) != _metadata_identity(expected):
                raise ManifestBuildError(
                    f"manifest directory changed during build: {path}"
                )

    def _scan_directory(
        self,
        directory: Path,
        chain: tuple[tuple[Path, os.stat_result], ...],
    ) -> tuple[str, ...]:
        self._validate_directory_chain(chain)
        descriptor: int | None = None
        try:
            directory_flags = (
                getattr(os, "O_RDONLY", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            if getattr(os, "O_DIRECTORY", 0) and getattr(os, "O_NOFOLLOW", 0):
                descriptor = os.open(directory, directory_flags)
                opened = os.fstat(descriptor)
                if _metadata_identity(opened) != _metadata_identity(chain[-1][1]):
                    raise ManifestBuildError(
                        f"manifest directory changed before scan: {directory}"
                    )
                children = tuple(
                    child.name
                    for child in sorted(
                        os.scandir(descriptor), key=lambda item: item.name
                    )
                )
            else:
                children = tuple(
                    child.name
                    for child in sorted(
                        os.scandir(directory), key=lambda item: item.name
                    )
                )
        except ManifestBuildError:
            raise
        except OSError as error:
            raise ManifestBuildError(
                f"cannot scan manifest tree: {directory}"
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        self._validate_directory_chain(chain)
        return children

    def _inventory(self, root: Path) -> _Inventory:
        entries: list[_InventoryEntry] = []
        total_bytes = 0
        root_metadata = self._directory_metadata(root)
        root_chain = ((root, root_metadata),)

        def add(entry: _InventoryEntry) -> None:
            nonlocal total_bytes
            if entry.size > self.limits.max_snapshot_file_bytes:
                raise SnapshotLimitError(
                    f"snapshot file exceeds limit: {entry.path.value} "
                    f"({entry.size} > {self.limits.max_snapshot_file_bytes})"
                )
            if len(entries) + 1 > self.limits.max_snapshot_files:
                raise SnapshotLimitError(
                    "snapshot file count exceeds limit "
                    f"({len(entries) + 1} > {self.limits.max_snapshot_files})"
                )
            if total_bytes + entry.size > self.limits.max_snapshot_bytes:
                raise SnapshotLimitError(
                    "snapshot total bytes exceed limit "
                    f"({total_bytes + entry.size} > {self.limits.max_snapshot_bytes})"
                )
            entries.append(entry)
            total_bytes += entry.size

        def walk(
            directory: Path,
            prefix: tuple[str, ...],
            chain: tuple[tuple[Path, os.stat_result], ...],
        ) -> None:
            for child_name in self._scan_directory(directory, chain):
                self._validate_directory_chain(chain)
                project_path = ProjectPath.parse(
                    "/".join((*prefix, child_name))
                )
                child_path = directory / child_name
                try:
                    metadata = os.stat(child_path, follow_symlinks=False)
                except OSError as error:
                    raise ManifestBuildError(
                        f"cannot inspect manifest entry: {project_path.value}"
                    ) from error

                if stat.S_ISLNK(metadata.st_mode):
                    add(
                        self._descriptor_entry(
                            child_path,
                            project_path,
                            metadata,
                            "symbolic link",
                            chain,
                        )
                    )
                    continue
                if _is_reparse(metadata):
                    add(
                        self._descriptor_entry(
                            child_path,
                            project_path,
                            metadata,
                            "reparse point",
                            chain,
                        )
                    )
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    walk(
                        child_path,
                        (*prefix, child_name),
                        (*chain, (child_path, metadata)),
                    )
                    continue
                if stat.S_ISREG(metadata.st_mode):
                    add(
                        self._file_entry(
                            child_path,
                            project_path,
                            metadata,
                            chain,
                            self.limits.max_snapshot_bytes - total_bytes,
                        )
                    )
                    continue
                add(
                    self._descriptor_entry(
                        child_path,
                        project_path,
                        metadata,
                        "special file",
                        chain,
                    )
                )

        walk(root, (), root_chain)
        self._validate_directory_chain(root_chain)
        entries.sort(key=lambda entry: entry.path.value)
        keys = [entry.path.comparison_key for entry in entries]
        if len(keys) != len(set(keys)):
            raise ManifestBuildError("manifest tree contains colliding project paths")
        payload = [
            {
                "path": entry.path.value,
                "size": entry.size,
                "sha256": entry.sha256,
                "kind": entry.kind,
            }
            for entry in entries
        ]
        digest = hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        return _Inventory(tuple(entries), total_bytes, digest)

    def _file_entry(
        self,
        path: Path,
        project_path: ProjectPath,
        before: os.stat_result,
        ancestors: tuple[tuple[Path, os.stat_result], ...],
        remaining_inventory_bytes: int,
    ) -> _InventoryEntry:
        if int(before.st_size) > self.limits.max_snapshot_file_bytes:
            raise SnapshotLimitError(
                f"snapshot file exceeds limit: {project_path.value} "
                f"({before.st_size} > {self.limits.max_snapshot_file_bytes})"
            )
        if int(before.st_size) > remaining_inventory_bytes:
            raise SnapshotLimitError(
                "snapshot total bytes exceeds limit "
                f"({self.limits.max_snapshot_bytes - remaining_inventory_bytes + before.st_size} "
                f"> {self.limits.max_snapshot_bytes})"
            )
        content, digest, contains_binary_control = self._read_content(
            path,
            project_path,
            before,
            ancestors,
            remaining_inventory_bytes,
        )

        reasons: tuple[str, ...]
        if contains_binary_control:
            reasons = ("binary content",)
        else:
            try:
                content.decode("utf-8")
            except UnicodeDecodeError:
                reasons = ("non-UTF-8 content",)
            else:
                reasons = ()
        return _InventoryEntry(
            path=project_path,
            size=len(content),
            sha256=digest,
            kind="file",
            reviewable=not reasons,
            reasons=reasons,
        )

    def _read_content(
        self,
        path: Path,
        project_path: ProjectPath,
        before: os.stat_result,
        ancestors: tuple[tuple[Path, os.stat_result], ...],
        remaining_inventory_bytes: int,
    ) -> tuple[bytes, str, bool]:
        self._validate_directory_chain(ancestors)
        digest = hashlib.sha256()
        contains_binary_control = False
        content = bytearray()
        descriptor: int | None = None
        try:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            current = os.stat(path, follow_symlinks=False)
            if (
                _is_link(current)
                or not stat.S_ISREG(current.st_mode)
                or _metadata_identity(before) != _metadata_identity(current)
                or _metadata_identity(before) != _metadata_identity(opened)
            ):
                raise ManifestBuildError(
                    f"file changed or became a link before read: {project_path.value}"
                )

            while True:
                hard_limit = min(
                    self.limits.max_snapshot_file_bytes,
                    remaining_inventory_bytes,
                )
                read_size = min(65536, max(1, hard_limit - len(content) + 1))
                chunk = os.read(descriptor, read_size)
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > self.limits.max_snapshot_file_bytes:
                    raise SnapshotLimitError(
                        f"snapshot file exceeds limit while reading: "
                        f"{project_path.value}"
                    )
                if len(content) > remaining_inventory_bytes:
                    raise SnapshotLimitError(
                        "snapshot total bytes exceeds limit while reading: "
                        f"{project_path.value}"
                    )
                digest.update(chunk)
                contains_binary_control = contains_binary_control or any(
                    byte < 32 and byte not in {8, 9, 10, 12, 13}
                    for byte in chunk
                )

            opened_after = os.fstat(descriptor)
            after = os.stat(path, follow_symlinks=False)
        except OSError as error:
            raise ManifestBuildError(
                f"cannot read manifest entry: {project_path.value}"
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)

        self._validate_directory_chain(ancestors)
        if (
            _is_link(after)
            or _metadata_identity(before) != _metadata_identity(after)
            or _metadata_identity(before) != _metadata_identity(opened_after)
            or len(content) != int(before.st_size)
        ):
            raise ManifestBuildError(
                f"file changed while hashing: {project_path.value}"
            )
        return bytes(content), digest.hexdigest(), contains_binary_control

    def _descriptor_entry(
        self,
        path: Path,
        project_path: ProjectPath,
        metadata: os.stat_result,
        kind: str,
        ancestors: tuple[tuple[Path, os.stat_result], ...],
    ) -> _InventoryEntry:
        self._validate_directory_chain(ancestors)
        target = ""
        if kind in {"symbolic link", "reparse point"}:
            try:
                target = os.readlink(path)
                after = os.stat(path, follow_symlinks=False)
            except OSError as error:
                if kind == "symbolic link":
                    raise ManifestBuildError(
                        f"cannot inspect link: {project_path.value}"
                    ) from error
                after = os.stat(path, follow_symlinks=False)
        else:
            after = os.stat(path, follow_symlinks=False)
        self._validate_directory_chain(ancestors)
        if _metadata_identity(metadata) != _metadata_identity(after):
            raise ManifestBuildError(
                f"manifest entry changed while inspecting: {project_path.value}"
            )
        descriptor = (
            f"{kind}:{stat.S_IFMT(metadata.st_mode)}:{metadata.st_size}:{target}"
        )
        return _InventoryEntry(
            path=project_path,
            size=int(metadata.st_size),
            sha256=hashlib.sha256(descriptor.encode("utf-8")).hexdigest(),
            kind=kind,
            reviewable=False,
            reasons=(kind,),
        )

    def _compare(
        self, baseline: _Inventory, workspace: _Inventory
    ) -> tuple[PatchEntry, ...]:
        before = baseline.by_path
        after = workspace.by_path
        entries: list[PatchEntry] = []
        for path_value in sorted(set(before) | set(after)):
            old = before.get(path_value)
            new = after.get(path_value)
            if old is not None and new is not None:
                if old.sha256 == new.sha256 and old.kind == new.kind:
                    continue
                operation = PatchOperation.MODIFY
            elif new is not None:
                operation = PatchOperation.ADD
            else:
                operation = PatchOperation.DELETE

            present = tuple(item for item in (old, new) if item is not None)
            reviewable = all(item.reviewable and item.kind == "file" for item in present)
            reasons: list[str] = []
            decision = self._protected_paths.classify((new or old).path)
            if not decision.included and decision.reason:
                reasons.append(decision.reason)
            for item in present:
                for reason in item.reasons:
                    if reason not in reasons:
                        reasons.append(reason)
            apply_safe = reviewable and decision.included
            entries.append(
                PatchEntry(
                    path=(new or old).path,
                    operation=operation,
                    before_sha256=old.sha256 if old else None,
                    after_sha256=new.sha256 if new else None,
                    before_size=old.size if old else None,
                    after_size=new.size if new else None,
                    reviewable=reviewable,
                    apply_safe=apply_safe,
                    reasons=tuple(reasons),
                )
            )
        return tuple(entries)
