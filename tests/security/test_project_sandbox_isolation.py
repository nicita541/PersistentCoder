from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

import app.storage as storage_module
from app.project_identity import ProjectIdentity
from app.sandbox.workspace import (
    SandboxIdentityError,
    SandboxWorkspace,
)
from app.storage import ProjectStorage


@dataclass(frozen=True)
class ProjectCase:
    root: Path
    identity: ProjectIdentity
    storage: ProjectStorage


def _project_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> ProjectCase:
    framework_root = tmp_path / "framework"
    root = tmp_path / name
    framework_root.mkdir(exist_ok=True)
    root.mkdir()
    (root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")

    monkeypatch.setattr(storage_module, "PROJECT_ROOT", framework_root)
    monkeypatch.setattr(storage_module, "DATA_ROOT", framework_root / "data")
    monkeypatch.setattr(
        storage_module,
        "SANDBOX_ROOT",
        framework_root / ".sandbox",
    )

    identity = ProjectIdentity.from_source_root(root)
    storage = ProjectStorage.for_identity(identity)
    storage.ensure_layout()
    return ProjectCase(root, identity, storage)


def test_same_session_id_cannot_cross_project_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_a = _project_case(tmp_path, monkeypatch, "project-a")
    project_b = _project_case(tmp_path, monkeypatch, "project-b")

    workspace_a = SandboxWorkspace.create(
        project_root=project_a.root,
        identity=project_a.identity,
        storage=project_a.storage,
        session_id="fixed",
    )

    assert SandboxWorkspace.open_session(
        "fixed",
        identity=project_b.identity,
        storage=project_b.storage,
    ) is None
    assert project_a.identity.project_id in str(
        workspace_a.workspace_root
    )
    assert workspace_a.project_id == project_a.identity.project_id


def test_open_session_rejects_tampered_source_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_a = _project_case(tmp_path, monkeypatch, "project-a")
    project_b = _project_case(tmp_path, monkeypatch, "project-b")
    workspace = SandboxWorkspace.create(
        project_root=project_a.root,
        identity=project_a.identity,
        storage=project_a.storage,
    )
    metadata = json.loads(
        workspace.metadata_path.read_text(encoding="utf-8")
    )
    metadata["project_id"] = project_b.identity.project_id
    workspace.metadata_path.write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )

    with pytest.raises(SandboxIdentityError):
        SandboxWorkspace.open_session(
            workspace.session_id,
            identity=project_a.identity,
            storage=project_a.storage,
        )


def test_create_rejects_identity_for_another_source_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_a = _project_case(tmp_path, monkeypatch, "project-a")
    project_b = _project_case(tmp_path, monkeypatch, "project-b")

    with pytest.raises(SandboxIdentityError):
        SandboxWorkspace.create(
            project_root=project_a.root,
            identity=project_b.identity,
            storage=project_b.storage,
        )
