from __future__ import annotations

import hashlib
import json
import os
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

import app.apply.builder as manifest_builder
from app.apply.builder import ManifestBuildError, PatchManifestBuilder
from app.apply.manifest import (
    ManifestValidationError,
    PatchEntry,
    PatchManifest,
    PatchOperation,
)
from app.project_identity import ProjectIdentity
from app.sandbox.limits import SandboxLimits
from app.sandbox.project_path import ProjectPath
from app.sandbox.snapshot import SnapshotLimitError


ZERO_HASH = "0" * 64
ONE_HASH = "1" * 64


def _identity(root: Path) -> ProjectIdentity:
    root.mkdir(exist_ok=True)
    return ProjectIdentity.from_source_root(root)


def _entry(path: str = "src/app.py") -> PatchEntry:
    return PatchEntry(
        path=path,
        operation=PatchOperation.MODIFY,
        before_sha256=ZERO_HASH,
        after_sha256=ONE_HASH,
        before_size=1,
        after_size=2,
        reviewable=True,
        apply_safe=True,
        reasons=(),
    )


def test_patch_entry_is_frozen_and_enforces_operation_hash_contract() -> None:
    entry = _entry("./src/app.py")

    assert entry.path.value == "src/app.py"
    with pytest.raises(FrozenInstanceError):
        entry.after_size = 3  # type: ignore[misc]
    with pytest.raises(ValueError):
        PatchOperation("modify")

    invalid = [
        dict(operation=PatchOperation.ADD, before_sha256=ZERO_HASH, before_size=1),
        dict(operation=PatchOperation.ADD, after_sha256=None, after_size=None),
        dict(operation=PatchOperation.MODIFY, before_sha256=None, before_size=None),
        dict(operation=PatchOperation.DELETE, after_sha256=ONE_HASH, after_size=2),
        dict(operation=PatchOperation.DELETE, before_sha256=None, before_size=None),
    ]
    for changes in invalid:
        values = {
            "path": "file.txt",
            "operation": PatchOperation.ADD,
            "before_sha256": None,
            "after_sha256": ONE_HASH,
            "before_size": None,
            "after_size": 2,
            "reviewable": True,
            "apply_safe": True,
            "reasons": (),
        }
        values.update(changes)
        with pytest.raises(ManifestValidationError):
            PatchEntry(**values)

    with pytest.raises(ManifestValidationError):
        replace(entry, after_sha256="not-a-sha256")
    with pytest.raises(ManifestValidationError):
        replace(entry, reasons=("x" * 201,))
    with pytest.raises(ManifestValidationError):
        replace(entry, reviewable=False, apply_safe=True)
    with pytest.raises(ManifestValidationError):
        replace(entry, path=ProjectPath("../escape.py"))


def test_manifest_has_canonical_content_addressed_serialization(tmp_path: Path) -> None:
    identity = _identity(tmp_path / "source")
    entries = (_entry("a.py"), _entry("z.py"))
    manifest = PatchManifest.create(
        project_id=identity.project_id,
        canonical_source_root=str(identity.canonical_source_root),
        session_id="session-1",
        baseline_sha256=ZERO_HASH,
        workspace_sha256=ONE_HASH,
        verification_id="verification-1",
        entries=entries,
    )

    canonical = manifest.to_json()
    decoded = json.loads(canonical)
    payload = dict(decoded)
    manifest_id = payload.pop("manifest_id")

    assert canonical == json.dumps(
        decoded, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    assert manifest_id == hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()
    assert PatchManifest.from_json(canonical) == manifest


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(manifest_id="f" * 64),
        lambda data: data.update(project_id="f" * 64),
        lambda data: data["entries"].reverse(),
        lambda data: data["entries"].append(dict(data["entries"][0])),
        lambda data: data["entries"][0].update(after_sha256="bad"),
        lambda data: data["entries"][0].update(before_sha256=None),
    ],
)
def test_manifest_deserialization_rejects_tampering(
    tmp_path: Path, mutate
) -> None:
    identity = _identity(tmp_path / "source")
    manifest = PatchManifest.create(
        project_id=identity.project_id,
        canonical_source_root=str(identity.canonical_source_root),
        session_id="session-1",
        baseline_sha256=ZERO_HASH,
        workspace_sha256=ONE_HASH,
        verification_id="verification-1",
        entries=(_entry("a.py"), _entry("z.py")),
    )
    data = manifest.to_dict()
    mutate(data)

    with pytest.raises(ManifestValidationError):
        PatchManifest.from_dict(data)


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_manifest_deserialization_requires_exact_integer_schema(
    tmp_path: Path, schema_version: object
) -> None:
    identity = _identity(tmp_path / "source")
    manifest = PatchManifest.create(
        project_id=identity.project_id,
        canonical_source_root=str(identity.canonical_source_root),
        session_id="session-1",
        baseline_sha256=ZERO_HASH,
        workspace_sha256=ONE_HASH,
        verification_id="verification-1",
        entries=(_entry("a.py"),),
    )
    data = manifest.to_dict()
    data["schema_version"] = schema_version

    with pytest.raises(ManifestValidationError):
        PatchManifest.from_dict(data)


def test_manifest_deserialization_rejects_noncanonical_serialized_path(
    tmp_path: Path,
) -> None:
    identity = _identity(tmp_path / "source")
    manifest = PatchManifest.create(
        project_id=identity.project_id,
        canonical_source_root=str(identity.canonical_source_root),
        session_id="session-1",
        baseline_sha256=ZERO_HASH,
        workspace_sha256=ONE_HASH,
        verification_id="verification-1",
        entries=(_entry("a.py"),),
    )
    data = manifest.to_dict()
    data["entries"][0]["path"] = "./a.py"

    with pytest.raises(ManifestValidationError):
        PatchManifest.from_dict(data)


def test_manifest_json_rejects_duplicate_object_keys(tmp_path: Path) -> None:
    identity = _identity(tmp_path / "source")
    manifest = PatchManifest.create(
        project_id=identity.project_id,
        canonical_source_root=str(identity.canonical_source_root),
        session_id="session-1",
        baseline_sha256=ZERO_HASH,
        workspace_sha256=ONE_HASH,
        verification_id="verification-1",
        entries=(_entry("a.py"),),
    )
    duplicate = manifest.to_json().replace(
        '"schema_version":1',
        '"schema_version":1,"schema_version":1',
        1,
    )

    with pytest.raises(ManifestValidationError):
        PatchManifest.from_json(duplicate)


def test_builder_detects_add_modify_delete_in_stable_order(tmp_path: Path) -> None:
    identity = _identity(tmp_path / "source")
    baseline = tmp_path / "baseline"
    workspace = tmp_path / "workspace"
    baseline.mkdir()
    workspace.mkdir()
    (baseline / "b.txt").write_text("before", encoding="utf-8")
    (baseline / "c.txt").write_text("deleted", encoding="utf-8")
    (workspace / "a.txt").write_text("added", encoding="utf-8")
    (workspace / "b.txt").write_text("after", encoding="utf-8")

    manifest = PatchManifestBuilder().build(
        project_identity=identity,
        session_id="session-1",
        baseline_root=baseline,
        workspace_root=workspace,
        verification_id="verification-1",
    )

    assert [entry.path.value for entry in manifest.entries] == [
        "a.txt",
        "b.txt",
        "c.txt",
    ]
    assert [entry.operation for entry in manifest.entries] == [
        PatchOperation.ADD,
        PatchOperation.MODIFY,
        PatchOperation.DELETE,
    ]
    assert manifest.entries[0].before_sha256 is None
    assert manifest.entries[0].after_size == 5
    assert manifest.entries[1].before_sha256 == hashlib.sha256(b"before").hexdigest()
    assert manifest.entries[1].after_sha256 == hashlib.sha256(b"after").hexdigest()
    assert manifest.entries[2].after_sha256 is None
    assert all(entry.reviewable for entry in manifest.entries)
    assert all(entry.apply_safe for entry in manifest.entries)


def test_builder_keeps_binary_non_utf8_and_protected_changes_visible(
    tmp_path: Path,
) -> None:
    identity = _identity(tmp_path / "source")
    baseline = tmp_path / "baseline"
    workspace = tmp_path / "workspace"
    baseline.mkdir()
    workspace.mkdir()
    (workspace / "binary.bin").write_bytes(b"\x01\x02")
    (workspace / "invalid.txt").write_bytes(b"\xff")
    (workspace / ".env").write_text("SECRET=value", encoding="utf-8")

    manifest = PatchManifestBuilder().build(
        project_identity=identity,
        session_id="session-1",
        baseline_root=baseline,
        workspace_root=workspace,
        verification_id="verification-1",
    )
    entries = {entry.path.value: entry for entry in manifest.entries}

    assert set(entries) == {".env", "binary.bin", "invalid.txt"}
    assert entries[".env"].reviewable is True
    assert entries[".env"].apply_safe is False
    assert any("secret" in reason for reason in entries[".env"].reasons)
    assert entries["binary.bin"].reviewable is False
    assert entries["binary.bin"].apply_safe is False
    assert "binary content" in entries["binary.bin"].reasons
    assert entries["invalid.txt"].reviewable is False
    assert entries["invalid.txt"].apply_safe is False
    assert "non-UTF-8 content" in entries["invalid.txt"].reasons


def test_builder_detects_links_without_following_them(tmp_path: Path) -> None:
    identity = _identity(tmp_path / "source")
    baseline = tmp_path / "baseline"
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    baseline.mkdir()
    workspace.mkdir()
    outside.write_text("outside secret", encoding="utf-8")
    try:
        os.symlink(outside, workspace / "linked.txt")
    except OSError:
        pytest.skip("symbolic links are unavailable on this host")

    try:
        manifest = PatchManifestBuilder().build(
            project_identity=identity,
            session_id="session-1",
            baseline_root=baseline,
            workspace_root=workspace,
            verification_id="verification-1",
        )
    finally:
        (workspace / "linked.txt").unlink(missing_ok=True)

    entry = manifest.entries[0]
    assert entry.path.value == "linked.txt"
    assert entry.reviewable is False
    assert entry.apply_safe is False
    assert "symbolic link" in entry.reasons
    assert entry.after_sha256 != hashlib.sha256(outside.read_bytes()).hexdigest()


def test_builder_detects_same_length_symlink_retargeting(tmp_path: Path) -> None:
    identity = _identity(tmp_path / "source")
    baseline = tmp_path / "baseline"
    workspace = tmp_path / "workspace"
    first = tmp_path / "first.txt"
    other = tmp_path / "other.txt"
    baseline.mkdir()
    workspace.mkdir()
    first.write_text("same", encoding="utf-8")
    other.write_text("same", encoding="utf-8")
    baseline_link = baseline / "linked.txt"
    workspace_link = workspace / "linked.txt"
    try:
        os.symlink(first, baseline_link)
        os.symlink(other, workspace_link)
    except OSError:
        baseline_link.unlink(missing_ok=True)
        workspace_link.unlink(missing_ok=True)
        pytest.skip("symbolic links are unavailable on this host")

    try:
        manifest = PatchManifestBuilder().build(
            project_identity=identity,
            session_id="session-1",
            baseline_root=baseline,
            workspace_root=workspace,
            verification_id="verification-1",
        )
    finally:
        baseline_link.unlink(missing_ok=True)
        workspace_link.unlink(missing_ok=True)

    assert len(manifest.entries) == 1
    assert manifest.entries[0].operation is PatchOperation.MODIFY
    assert manifest.entries[0].before_sha256 != manifest.entries[0].after_sha256


def test_builder_enforces_inventory_and_patch_limits(tmp_path: Path) -> None:
    identity = _identity(tmp_path / "source")
    baseline = tmp_path / "baseline"
    workspace = tmp_path / "workspace"
    baseline.mkdir()
    workspace.mkdir()
    (workspace / "large.txt").write_text("12345", encoding="utf-8")

    with pytest.raises(SnapshotLimitError):
        PatchManifestBuilder(
            limits=SandboxLimits(max_snapshot_file_bytes=4)
        ).build(
            project_identity=identity,
            session_id="session-1",
            baseline_root=baseline,
            workspace_root=workspace,
            verification_id="verification-1",
        )

    with pytest.raises(ManifestBuildError, match="patch exceeds"):
        PatchManifestBuilder(limits=SandboxLimits(max_patch_bytes=4)).build(
            project_identity=identity,
            session_id="session-1",
            baseline_root=baseline,
            workspace_root=workspace,
            verification_id="verification-1",
        )


def test_builder_rejects_workspace_mutation_during_final_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity(tmp_path / "source")
    baseline = tmp_path / "baseline"
    workspace = tmp_path / "workspace"
    baseline.mkdir()
    workspace.mkdir()
    target = workspace / "file.txt"
    target.write_text("first", encoding="utf-8")
    builder = PatchManifestBuilder()
    original_inventory = builder._inventory
    calls = 0

    def mutate_after_workspace(root: Path):
        nonlocal calls
        inventory = original_inventory(root)
        calls += 1
        if calls == 2:
            target.write_text("second", encoding="utf-8")
        return inventory

    monkeypatch.setattr(builder, "_inventory", mutate_after_workspace)

    with pytest.raises(ManifestBuildError, match="changed during manifest build"):
        builder.build(
            project_identity=identity,
            session_id="session-1",
            baseline_root=baseline,
            workspace_root=workspace,
            verification_id="verification-1",
        )


def _replace_directory_with_symlink_during_scan(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target: Path,
    outside: Path,
) -> tuple[Path, list[str]]:
    original_scandir = os.scandir
    original_os_open = os.open
    backup = target.with_name(f"{target.name}-original")
    replaced = False
    outside_enumerations: list[str] = []

    def replace_target(path: object) -> None:
        nonlocal replaced
        try:
            selected = Path(path) == target
        except TypeError:
            selected = False
        if not replaced and selected:
            target.replace(backup)
            try:
                os.symlink(outside, target, target_is_directory=True)
            except OSError:
                backup.replace(target)
                pytest.skip("directory symbolic links are unavailable on this host")
            replaced = True

    def racing_scandir(path):
        replace_target(path)
        if replaced:
            outside_enumerations.append(str(path))
        return original_scandir(path)

    def racing_os_open(path, flags, *args, **kwargs):
        replace_target(path)
        return original_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", racing_scandir)
    monkeypatch.setattr(os, "open", racing_os_open)
    return backup, outside_enumerations


def _restore_replaced_path(target: Path, backup: Path) -> None:
    if target.is_symlink():
        target.unlink()
    if backup.exists():
        backup.replace(target)


def _track_reads_under(
    monkeypatch: pytest.MonkeyPatch, outside: Path
) -> list[str]:
    original_path_open = Path.open
    original_os_open = os.open
    original_os_read = os.read
    tracked_descriptors: set[int] = set()
    reads: list[str] = []
    outside_root = outside.resolve()

    class TrackingHandle:
        def __init__(self, handle) -> None:
            self._handle = handle

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *args):
            return self._handle.__exit__(*args)

        def read(self, size=-1):
            reads.append("path")
            return self._handle.read(size)

    def is_outside(path: object) -> bool:
        try:
            return Path(path).resolve().is_relative_to(outside_root)
        except (OSError, TypeError, ValueError):
            return False

    def tracking_path_open(path: Path, *args, **kwargs):
        handle = original_path_open(path, *args, **kwargs)
        return TrackingHandle(handle) if is_outside(path) else handle

    def tracking_os_open(path, flags, *args, **kwargs):
        descriptor = original_os_open(path, flags, *args, **kwargs)
        if is_outside(path):
            tracked_descriptors.add(descriptor)
        return descriptor

    def tracking_os_read(descriptor: int, size: int) -> bytes:
        if descriptor in tracked_descriptors:
            reads.append("descriptor")
        return original_os_read(descriptor, size)

    monkeypatch.setattr(Path, "open", tracking_path_open)
    monkeypatch.setattr(os, "open", tracking_os_open)
    monkeypatch.setattr(os, "read", tracking_os_read)
    return reads


def test_builder_rejects_root_replaced_with_link_before_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity(tmp_path / "source")
    baseline = tmp_path / "baseline"
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    baseline.mkdir()
    workspace.mkdir()
    outside.mkdir()
    (workspace / "local.txt").write_text("local", encoding="utf-8")
    (outside / "outside.txt").write_text("outside", encoding="utf-8")
    reads = _track_reads_under(monkeypatch, outside)
    backup, outside_enumerations = _replace_directory_with_symlink_during_scan(
        monkeypatch, target=workspace, outside=outside
    )

    try:
        with pytest.raises(ManifestBuildError, match="changed|link|scan"):
            PatchManifestBuilder().build(
                project_identity=identity,
                session_id="session-1",
                baseline_root=baseline,
                workspace_root=workspace,
                verification_id="verification-1",
            )
        assert reads == []
        assert outside_enumerations == []
    finally:
        _restore_replaced_path(workspace, backup)


def test_builder_rejects_directory_replaced_with_link_before_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity(tmp_path / "source")
    baseline = tmp_path / "baseline"
    workspace = tmp_path / "workspace"
    nested = workspace / "nested"
    outside = tmp_path / "outside"
    baseline.mkdir()
    nested.mkdir(parents=True)
    outside.mkdir()
    (nested / "local.txt").write_text("local", encoding="utf-8")
    (outside / "outside.txt").write_text("outside", encoding="utf-8")
    reads = _track_reads_under(monkeypatch, outside)
    backup, outside_enumerations = _replace_directory_with_symlink_during_scan(
        monkeypatch, target=nested, outside=outside
    )

    try:
        with pytest.raises(ManifestBuildError, match="changed|link|scan"):
            PatchManifestBuilder().build(
                project_identity=identity,
                session_id="session-1",
                baseline_root=baseline,
                workspace_root=workspace,
                verification_id="verification-1",
            )
        assert reads == []
        assert outside_enumerations == []
    finally:
        _restore_replaced_path(nested, backup)


def test_builder_does_not_read_file_replaced_with_link_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity(tmp_path / "source")
    baseline = tmp_path / "baseline"
    workspace = tmp_path / "workspace"
    baseline.mkdir()
    workspace.mkdir()
    target = workspace / "file.txt"
    backup = workspace / "file-original.txt"
    outside = tmp_path / "outside.txt"
    target.write_text("local", encoding="utf-8")
    outside.write_text("outside", encoding="utf-8")
    original_path_open = Path.open
    original_os_open = os.open
    reads = 0
    followed_opens = 0
    replaced = False

    class TrackingHandle:
        def __init__(self, handle) -> None:
            self._handle = handle

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *args):
            return self._handle.__exit__(*args)

        def read(self, size=-1):
            nonlocal reads
            reads += 1
            return self._handle.read(size)

    def replace_target() -> None:
        nonlocal replaced
        if replaced:
            return
        target.replace(backup)
        try:
            os.symlink(outside, target)
        except OSError:
            backup.replace(target)
            pytest.skip("file symbolic links are unavailable on this host")
        replaced = True

    def racing_path_open(path: Path, *args, **kwargs):
        if path == target:
            replace_target()
            return TrackingHandle(original_path_open(path, *args, **kwargs))
        return original_path_open(path, *args, **kwargs)

    def racing_os_open(path, flags, *args, **kwargs):
        nonlocal followed_opens
        if Path(path) == target:
            replace_target()
            followed_opens += 1
        return original_os_open(path, flags, *args, **kwargs)

    original_windows_create = getattr(
        manifest_builder, "_windows_create_handle", None
    )

    def racing_windows_create(path, *args, **kwargs):
        if Path(path) == target:
            replace_target()
        return original_windows_create(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_path_open)
    monkeypatch.setattr(os, "open", racing_os_open)
    if original_windows_create is not None:
        monkeypatch.setattr(
            manifest_builder, "_windows_create_handle", racing_windows_create
        )

    try:
        with pytest.raises(
            ManifestBuildError, match="changed|link|read|reparse"
        ):
            PatchManifestBuilder().build(
                project_identity=identity,
                session_id="session-1",
                baseline_root=baseline,
                workspace_root=workspace,
                verification_id="verification-1",
            )
        assert reads == 0
        assert followed_opens == 0
    finally:
        _restore_replaced_path(target, backup)


@pytest.mark.skipif(os.name != "nt", reason="Windows share modes are required")
def test_windows_file_handle_blocks_replacement_before_descriptor_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import msvcrt

    identity = _identity(tmp_path / "source")
    baseline = tmp_path / "baseline"
    workspace = tmp_path / "workspace"
    baseline.mkdir()
    workspace.mkdir()
    target = workspace / "file.txt"
    backup = workspace / "file-original.txt"
    target.write_text("local", encoding="utf-8")
    original_open_osfhandle = msvcrt.open_osfhandle
    attempted = False
    replacement_allowed = False

    def racing_open_osfhandle(handle: int, flags: int) -> int:
        nonlocal attempted, replacement_allowed
        attempted = True
        try:
            target.replace(backup)
        except OSError:
            raise
        replacement_allowed = True
        return original_open_osfhandle(handle, flags)

    monkeypatch.setattr(msvcrt, "open_osfhandle", racing_open_osfhandle)

    try:
        with pytest.raises(ManifestBuildError):
            PatchManifestBuilder().build(
                project_identity=identity,
                session_id="session-1",
                baseline_root=baseline,
                workspace_root=workspace,
                verification_id="verification-1",
            )
        assert attempted is True
        assert replacement_allowed is False
    finally:
        _restore_replaced_path(target, backup)


def test_builder_bounds_bytes_read_from_growing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity(tmp_path / "source")
    baseline = tmp_path / "baseline"
    workspace = tmp_path / "workspace"
    baseline.mkdir()
    workspace.mkdir()
    target = workspace / "growing.txt"
    target.write_bytes(b"x")
    original_path_open = Path.open
    original_os_open = os.open
    original_os_read = os.read
    target_fd: int | None = None
    bytes_read = 0
    chunks_left = 5

    class GrowingHandle:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size=-1):
            nonlocal bytes_read, chunks_left
            if chunks_left == 0:
                return b""
            chunks_left -= 1
            bytes_read += 4
            return b"xxxx"

    def growing_path_open(path: Path, *args, **kwargs):
        if path == target:
            return GrowingHandle()
        return original_path_open(path, *args, **kwargs)

    def tracking_os_open(path, flags, *args, **kwargs):
        nonlocal target_fd
        descriptor = original_os_open(path, flags, *args, **kwargs)
        if Path(path) == target:
            target_fd = descriptor
        return descriptor

    def growing_os_read(descriptor: int, size: int) -> bytes:
        nonlocal bytes_read, chunks_left
        if descriptor != target_fd:
            return original_os_read(descriptor, size)
        if chunks_left == 0:
            return b""
        chunks_left -= 1
        chunk = b"x" * min(4, size)
        bytes_read += len(chunk)
        return chunk

    monkeypatch.setattr(Path, "open", growing_path_open)
    monkeypatch.setattr(os, "open", tracking_os_open)
    monkeypatch.setattr(os, "read", growing_os_read)
    if os.name == "nt":
        import msvcrt

        original_open_osfhandle = msvcrt.open_osfhandle

        def tracking_open_osfhandle(handle: int, flags: int) -> int:
            nonlocal target_fd
            descriptor = original_open_osfhandle(handle, flags)
            target_fd = descriptor
            return descriptor

        monkeypatch.setattr(
            msvcrt, "open_osfhandle", tracking_open_osfhandle
        )

    with pytest.raises(SnapshotLimitError):
        PatchManifestBuilder(
            limits=SandboxLimits(
                max_snapshot_file_bytes=5,
                max_snapshot_bytes=5,
            )
        ).build(
            project_identity=identity,
            session_id="session-1",
            baseline_root=baseline,
            workspace_root=workspace,
            verification_id="verification-1",
        )

    assert bytes_read <= 6
