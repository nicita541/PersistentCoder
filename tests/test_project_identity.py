from __future__ import annotations

from pathlib import Path

import pytest

import app.storage as storage_module
from app.project_identity import (
    ProjectIdentity,
    ProjectIdentityError,
)
from app.storage import (
    ProjectStorage,
    ProjectStorageError,
)


def test_project_identity_is_stable_for_equivalent_roots(
    tmp_path: Path,
) -> None:
    project = tmp_path / "Project"
    project.mkdir()

    first = ProjectIdentity.from_source_root(project)
    second = ProjectIdentity.from_source_root(project / ".")

    assert first.project_id == second.project_id
    assert first.canonical_source_root == project.resolve()
    assert first.comparison_root
    assert len(first.project_id) == 64
    assert set(first.project_id) <= set("0123456789abcdef")


def test_project_identity_rejects_missing_or_file_roots(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    source_file = tmp_path / "file.py"
    source_file.write_text("pass\n", encoding="utf-8")

    with pytest.raises(ProjectIdentityError):
        ProjectIdentity.from_source_root(missing)

    with pytest.raises(ProjectIdentityError):
        ProjectIdentity.from_source_root(source_file)


def test_project_storage_never_writes_under_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    framework_root = tmp_path / "framework"
    target_root = tmp_path / "target"
    framework_root.mkdir()
    target_root.mkdir()

    monkeypatch.setattr(
        storage_module,
        "PROJECT_ROOT",
        framework_root,
    )
    monkeypatch.setattr(
        storage_module,
        "DATA_ROOT",
        framework_root / "data",
    )
    monkeypatch.setattr(
        storage_module,
        "SANDBOX_ROOT",
        framework_root / ".sandbox",
    )

    identity = ProjectIdentity.from_source_root(target_root)
    storage = ProjectStorage.for_identity(identity)
    storage.ensure_layout()

    expected_database = (
        framework_root
        / "data"
        / "projects"
        / identity.project_id
        / "persistent_coder.db"
    )
    expected_sandbox = (
        framework_root
        / ".sandbox"
        / "projects"
        / identity.project_id
    )

    assert storage.database_path == expected_database
    assert storage.sandbox_root == expected_sandbox
    assert storage.database_path.parent.is_dir()
    assert storage.sessions_root.is_dir()
    assert storage.snapshots_root.is_dir()
    assert storage.patches_root.is_dir()
    assert storage.logs_root.is_dir()
    assert storage.tmp_root.is_dir()
    assert not any(target_root.iterdir())


def test_project_storage_rejects_paths_outside_framework_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    framework_root = tmp_path / "framework"
    target_root = tmp_path / "target"
    escaped_root = tmp_path / "escaped"
    framework_root.mkdir()
    target_root.mkdir()

    monkeypatch.setattr(
        storage_module,
        "PROJECT_ROOT",
        framework_root,
    )

    identity = ProjectIdentity.from_source_root(target_root)
    storage = ProjectStorage(
        identity=identity,
        database_path=escaped_root / "persistent_coder.db",
        sandbox_root=escaped_root / "sandbox",
    )

    with pytest.raises(ProjectStorageError):
        storage.ensure_layout()

    assert not escaped_root.exists()
