from __future__ import annotations

from pathlib import Path

import pytest

from app.sandbox.gitignore import GitIgnoreMatcher
from app.sandbox.project_path import ProjectPath
from app.sandbox.protected_paths import ProtectedPathPolicy


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        ".venv/Lib/site.py",
        "src/__pycache__/app.pyc",
        ".pytest_cache/state",
        "node_modules/pkg/index.js",
        ".sandbox/session.json",
        ".env",
        ".env.local",
        "deploy/private.pem",
        "deploy/client.key",
        "deploy/client.p12",
        "credentials.json",
        "config/service-account.json",
        "id_rsa",
        ".npmrc",
    ],
)
def test_protected_snapshot_paths_are_excluded(path: str) -> None:
    decision = ProtectedPathPolicy().classify(ProjectPath.parse(path))
    assert decision.included is False
    assert decision.reason


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".env.example",
        "data/domain.sqlite",
        "models/domain.py",
        ".vscode/settings.json",
        ".idea/codeStyles.xml",
        "src/my_secret_recipe.py",
    ],
)
def test_normal_target_paths_are_not_excluded_by_name(path: str) -> None:
    decision = ProtectedPathPolicy().classify(ProjectPath.parse(path))
    assert decision.included is True
    assert decision.reason is None


def test_root_gitignore_patterns_and_negation(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(
        "\n".join(
            [
                "# generated output",
                "build/",
                "*.log",
                "!important.log",
                "/generated.txt",
                "docs/*.tmp",
            ]
        ),
        encoding="utf-8",
    )
    matcher = GitIgnoreMatcher.from_project(tmp_path)

    assert matcher.is_ignored(ProjectPath.parse("build/app.py"))
    assert matcher.is_ignored(ProjectPath.parse("logs/debug.log"))
    assert not matcher.is_ignored(ProjectPath.parse("important.log"))
    assert matcher.is_ignored(ProjectPath.parse("generated.txt"))
    assert not matcher.is_ignored(ProjectPath.parse("src/generated.txt"))
    assert matcher.is_ignored(ProjectPath.parse("docs/draft.tmp"))
    assert not matcher.is_ignored(ProjectPath.parse("docs/nested/draft.tmp"))
    assert not matcher.is_ignored(ProjectPath.parse("src/app.py"))


def test_missing_gitignore_matches_nothing(tmp_path: Path) -> None:
    matcher = GitIgnoreMatcher.from_project(tmp_path)
    assert not matcher.is_ignored(ProjectPath.parse("build/app.py"))
