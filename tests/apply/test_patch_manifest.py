from __future__ import annotations

import hashlib
import json
import os
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from app.apply.builder import ManifestBuildError, PatchManifestBuilder
from app.apply.manifest import (
    ManifestValidationError,
    PatchEntry,
    PatchManifest,
    PatchOperation,
)
from app.project_identity import ProjectIdentity
from app.sandbox.limits import SandboxLimits
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
