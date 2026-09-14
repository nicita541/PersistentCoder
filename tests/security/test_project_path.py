from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.sandbox.project_path import ProjectPath, ProjectPathError


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (".github/workflows/ci.yml", ".github/workflows/ci.yml"),
        ("./src/app.py", "src/app.py"),
        (r"src\domain\model.py", "src/domain/model.py"),
        ("src//api///routes.py", "src/api/routes.py"),
        ("src/./app.py", "src/app.py"),
    ],
)
def test_project_path_normalizes_relative_files(
    raw: str,
    expected: str,
) -> None:
    assert ProjectPath.parse(raw).value == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        ".",
        "./",
        "../outside.py",
        "src/../../outside.py",
        "/etc/passwd",
        r"C:\Windows\system.ini",
        "C:relative.txt",
        r"\\server\share\file.txt",
        "//server/share/file.txt",
        "bad\x00name.txt",
    ],
)
def test_project_path_rejects_non_project_files(raw: str) -> None:
    with pytest.raises(ProjectPathError):
        ProjectPath.parse(raw)


def test_project_path_resolves_only_under_root(tmp_path: Path) -> None:
    target = ProjectPath.parse("src/app.py").resolve_under(tmp_path)
    assert target == (tmp_path / "src" / "app.py").resolve()


def test_project_path_comparison_matches_host_ownership_rules() -> None:
    upper = ProjectPath.parse("SRC/App.py")
    lower = ProjectPath.parse("src/app.py")

    if os.name == "nt":
        assert upper.comparison_key == lower.comparison_key
    else:
        assert upper.comparison_key != lower.comparison_key
