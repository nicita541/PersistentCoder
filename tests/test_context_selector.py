from __future__ import annotations

from pathlib import Path

from app.context.builder import ContextBuilder
from app.context.selector import (
    ContextLimits,
    RepoContextSelector,
)
from app.tools.file_tools import FileTools
from app.tools.project_tools import ProjectTools


def _make_project(root: Path) -> Path:
    (root / "src" / "users").mkdir(parents=True)
    (root / "src" / "billing").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)

    (root / "src" / "users" / "service.py").write_text(
        "class UserService:\n"
        "    def authenticate(self, email, password):\n"
        "        return email == 'a@b.c' and password\n",
        encoding="utf-8",
    )

    (root / "src" / "users" / "models.py").write_text(
        "class User:\n"
        "    def __init__(self, email):\n"
        "        self.email = email\n",
        encoding="utf-8",
    )

    (root / "src" / "billing" / "exporter.py").write_text(
        "def export_csv(rows):\n"
        "    return ';'.join(rows)\n",
        encoding="utf-8",
    )

    (root / "tests" / "test_user_service.py").write_text(
        "def test_authenticate_ok():\n"
        "    assert True\n",
        encoding="utf-8",
    )

    (root / "README.md").write_text(
        "# demo project\n",
        encoding="utf-8",
    )

    return root


class _Task:
    def __init__(self, title, description, criteria):
        self.key = "login"
        self.title = title
        self.description = description
        self.requires = []
        self.produces = []
        self.success_criteria = criteria


def _selector(root: Path) -> RepoContextSelector:
    files = FileTools(root)

    return RepoContextSelector(
        ProjectTools(root, files=files)
    )


def test_selector_picks_referenced_file_and_its_test(tmp_path):
    root = _make_project(tmp_path)

    selector = _selector(root)

    task = _Task(
        "Исправить падение UserService.authenticate",
        "Падает в src/users/service.py",
        ["tests pass"],
    )

    paths = selector.select_paths(task=task)

    assert "src/users/service.py" in paths
    assert "tests/test_user_service.py" in paths
    assert "src/billing/exporter.py" not in paths


def test_selector_only_returns_relative_sandbox_paths(tmp_path):
    root = _make_project(tmp_path)

    selector = _selector(root)

    task = _Task(
        "Fix UserService.authenticate",
        "in src/users/service.py",
        [],
    )

    for path in selector.select_paths(task=task):
        assert not path.startswith("/")
        assert ":" not in path
        assert ".." not in Path(path).parts


def test_selector_is_bounded_and_deterministic(tmp_path):
    root = _make_project(tmp_path)

    selector = _selector(root)

    task = _Task(
        "Fix UserService.authenticate",
        "in src/users/service.py",
        [],
    )

    first = selector.select(task=task)
    second = selector.select(task=task)

    assert first.files == second.files

    assert len(first.files) <= selector.limits.max_selected_files
    assert (
        len(first.text.encode("utf-8"))
        <= selector.limits.max_total_context_bytes
        + 4096
    )

    assert "FILE: src/users/service.py" in first.text


def test_selector_truncates_large_files(tmp_path):
    root = _make_project(tmp_path)

    (root / "src" / "users" / "service.py").write_text(
        "x = 1\n" + "y = 2\n" * 5000,
        encoding="utf-8",
    )

    selector = RepoContextSelector(
        ProjectTools(root),
        limits=ContextLimits(max_file_bytes=500),
    )

    context = selector.select(
        task=_Task(
            "Fix UserService.authenticate",
            "src/users/service.py",
            [],
        )
    )

    assert context.files
    assert len(context.text.encode("utf-8")) < 5000


def test_referenced_task_excludes_unrelated_search_hits(tmp_path):
    root = _make_project(tmp_path)

    # An unrelated module that mentions the same identifier.
    (root / "src" / "billing" / "exporter.py").write_text(
        "def export_csv(rows):\n"
        "    # UserService.authenticate is mentioned here too\n"
        "    return ';'.join(rows)\n",
        encoding="utf-8",
    )

    builder = ContextBuilder(selector=_selector(root))

    task = _Task(
        "Исправить падение UserService.authenticate",
        "Падает в src/users/service.py",
        ["tests pass"],
    )

    content = builder.build_task_messages(
        task=task
    )[-1]["content"]

    # The referenced file and its test are included...
    assert "FILE: src/users/service.py" in content
    assert "test_user_service.py" in content

    # ... but an unrelated search hit's body is not.
    assert "def export_csv" not in content


def test_no_referenced_file_means_no_file_content(tmp_path):
    root = _make_project(tmp_path)

    builder = ContextBuilder(selector=_selector(root))

    task = _Task(
        "Create a calculator module with add and subtract",
        "Add pytest tests for both functions.",
        ["tests pass"],
    )

    content = builder.build_task_messages(
        task=task
    )[-1]["content"]

    # No unrelated file body is dumped into the prompt: either a
    # paths-only listing or nothing at all.
    assert "def export_csv" not in content
    assert "class UserService" not in content
    assert "def authenticate" not in content
    assert "FILE: " not in content


def test_context_builder_uses_relevance_selection(tmp_path):
    root = _make_project(tmp_path)

    builder = ContextBuilder(selector=_selector(root))

    task = _Task(
        "Исправить падение UserService.authenticate",
        "Падает в src/users/service.py",
        ["tests pass"],
    )

    messages = builder.build_task_messages(task=task)

    content = messages[-1]["content"]

    assert "RELEVANT PROJECT CONTEXT" in content
    assert "src/users/service.py" in content
    assert "def authenticate" in content

    # The unrelated module is not dumped into the prompt.
    assert "export_csv" not in content
