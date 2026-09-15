from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.sandbox.project_path import ProjectGlob, ProjectPath, ProjectPathError


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

@pytest.mark.parametrize(
    "value",
    [
        "file.txt:secret",
        "CON",
        "src/nul.txt",
        "aux.py",
        "COM1.log",
        "CONIN$",
        "CONOUT$.txt",
        "COM¹.log",
        "LPT³",
        "dir/trailing. ",
        "dir/file ",
        "*.py",
    ],
)
def test_windows_ads_and_reserved_names_are_rejected_on_every_host(value):
    with pytest.raises(ProjectPathError):
        ProjectPath.parse(value)


@pytest.mark.parametrize("value", ["COM[1-9]", "[A-Z]UX", "src/C?N.py"])
def test_globs_that_can_select_windows_devices_are_rejected(value):
    with pytest.raises(ProjectPathError):
        ProjectGlob.parse(value)
