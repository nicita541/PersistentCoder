from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from app.sandbox.limits import SandboxLimits
from app.sandbox.snapshot import (
    SnapshotLimitError,
    SnapshotLinkError,
    SnapshotManifest,
)
from app.sandbox.workspace import SandboxWorkspace


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_manifest_keeps_target_data_models_and_excludes_secrets(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    (project / "models").mkdir()
    (project / ".github").mkdir()
    (project / "data" / "domain.db").write_bytes(b"domain")
    (project / "models" / "domain.py").write_text("MODEL = 1")
    (project / ".github" / "ci.yml").write_text("jobs: {}")
    (project / ".env").write_text("TOKEN=secret")
    (project / ".env.example").write_text("TOKEN=example")
    (project / "server.pem").write_text("private")

    manifest = SnapshotManifest.build(project)

    assert manifest.paths == (
        ".env.example",
        ".github/ci.yml",
        "data/domain.db",
        "models/domain.py",
    )
    assert manifest.total_bytes == sum(
        (project / Path(*path.split("/"))).stat().st_size
        for path in manifest.paths
    )


def test_gitignored_files_do_not_enter_manifest(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".gitignore").write_text("build/\n*.log\n")
    (project / "build").mkdir()
    (project / "build" / "out.py").write_text("ignored")
    (project / "debug.log").write_text("ignored")
    (project / "app.py").write_text("kept")

    assert SnapshotManifest.build(project).paths == (".gitignore", "app.py")


@pytest.mark.parametrize(
    ("limits", "files"),
    [
        (SandboxLimits(max_snapshot_file_bytes=3), {"big.txt": b"1234"}),
        (
            SandboxLimits(max_snapshot_bytes=5),
            {"a.txt": b"123", "b.txt": b"456"},
        ),
        (
            SandboxLimits(max_snapshot_files=1),
            {"a.txt": b"1", "b.txt": b"2"},
        ),
    ],
)
def test_manifest_limits_fail_before_materialization(
    tmp_path: Path,
    limits: SandboxLimits,
    files: dict[str, bytes],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    for name, content in files.items():
        (project / name).write_bytes(content)
    destination = tmp_path / "destination"

    with pytest.raises(SnapshotLimitError):
        SnapshotManifest.build(project, limits=limits)

    assert not destination.exists()


def test_symlink_file_and_directory_are_refused(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (outside / "value.txt").write_text("outside")
    try:
        os.symlink(outside / "value.txt", project / "linked.txt")
        os.symlink(outside, project / "linked-dir", target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable on this Windows host")

    with pytest.raises(SnapshotLinkError):
        SnapshotManifest.build(project)


def test_windows_junction_is_refused(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("junctions are Windows-specific")
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(project / "linked"), str(outside)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("junction creation is unavailable")

    with pytest.raises(SnapshotLinkError):
        SnapshotManifest.build(project)


def test_workspace_trees_share_one_vetted_manifest(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("VALUE = 1")
    workspace = SandboxWorkspace.create(
        project_root=project,
        sandbox_root=tmp_path / "sandbox",
        session_id="manifest-session",
    )

    metadata = json.loads(workspace.metadata_path.read_text(encoding="utf-8"))
    assert metadata["snapshot_manifest_sha256"]
    assert _files(workspace.baseline_root) == _files(workspace.workspace_root)
    assert _files(workspace.baseline_root) == {"app.py": b"VALUE = 1"}


def test_manifest_checkpoint_rollback_removes_new_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("VALUE = 1")
    workspace = SandboxWorkspace.create(
        project_root=project,
        sandbox_root=tmp_path / "sandbox",
        session_id="rollback-session",
    )
    workspace.checkpoint("attempt-1")
    (workspace.workspace_root / "artifact.txt").write_text("new")

    assert workspace.rollback("attempt-1") is True

    assert not (workspace.workspace_root / "artifact.txt").exists()
    assert (workspace.workspace_root / "app.py").read_text() == "VALUE = 1"
