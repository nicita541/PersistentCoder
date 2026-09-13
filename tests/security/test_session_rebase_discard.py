from __future__ import annotations

from pathlib import Path

import pytest

from app.sandbox.workspace import (
    SandboxResetError,
    SandboxWorkspace,
)


def _workspace(tmp_path: Path) -> SandboxWorkspace:
    project = tmp_path / "project"
    project.mkdir()
    (project / "value.txt").write_text("source-v1", encoding="utf-8")
    return SandboxWorkspace.create(
        project_root=project,
        sandbox_root=tmp_path / "sandbox",
        session_id="reset-session",
    )


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_rebase_uses_current_source_for_both_trees(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    assert workspace.source_project_root is not None
    (workspace.source_project_root / "value.txt").write_text(
        "source-v2",
        encoding="utf-8",
    )
    (workspace.workspace_root / "value.txt").write_text(
        "dirty",
        encoding="utf-8",
    )
    workspace.checkpoint("old")

    workspace.rebase_from_source()

    assert (workspace.baseline_root / "value.txt").read_text() == "source-v2"
    assert (workspace.workspace_root / "value.txt").read_text() == "source-v2"
    assert workspace.changed_files() == []
    assert workspace.list_checkpoints() == []


def test_discard_recreates_clean_trees_from_source(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    (workspace.workspace_root / "value.txt").write_text(
        "dirty",
        encoding="utf-8",
    )
    (workspace.workspace_root / "new.txt").write_text(
        "unwanted",
        encoding="utf-8",
    )

    workspace.discard_and_recreate()

    assert workspace.changed_files() == []
    assert (workspace.workspace_root / "value.txt").read_text() == "source-v1"
    assert not (workspace.workspace_root / "new.txt").exists()


def test_failed_rebase_restores_previous_baseline_and_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    assert workspace.source_project_root is not None
    (workspace.workspace_root / "value.txt").write_text(
        "dirty",
        encoding="utf-8",
    )
    (workspace.source_project_root / "value.txt").write_text(
        "source-v2",
        encoding="utf-8",
    )
    before = _tree(workspace.baseline_root), _tree(workspace.workspace_root)
    real_replace = workspace._replace_tree
    calls = 0

    def fail_on_second_replace(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected promotion failure")
        real_replace(source, destination)

    monkeypatch.setattr(workspace, "_replace_tree", fail_on_second_replace)

    with pytest.raises(SandboxResetError):
        workspace.rebase_from_source()

    assert (
        _tree(workspace.baseline_root),
        _tree(workspace.workspace_root),
    ) == before
