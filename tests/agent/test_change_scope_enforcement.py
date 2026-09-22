from __future__ import annotations

import json

from app.agent.coder.executor import CodeExecutor
from app.agent.coder.workspace import Workspace
from app.tasks.change_scope import AllowedChangeSet

from helpers import FakeLLM


class FakeTask:
    key = "scoped"
    title = "Scoped change"
    description = "Change only the declared file."
    success_criteria = ["declared file changed"]


def _executor(tmp_path, responses):
    return CodeExecutor(
        workspace=Workspace(tmp_path),
        llm=FakeLLM(responses),
        context=None,
    )


def _edit(*files):
    return json.dumps(
        {"action": "edit", "files": list(files), "tools": []}
    )


def _read(path):
    return json.dumps({"action": "read", "path": path})


def test_out_of_scope_write_is_rejected(tmp_path):
    executor = _executor(
        tmp_path,
        [_edit({"path": "src/other.py", "content": "bad"})],
    )

    result = executor.execute(
        FakeTask(),
        allowed_changes=AllowedChangeSet(["src/app.py"]),
    )

    assert result.ok is False
    assert "outside the exact Step change scope" in result.failure_reason
    assert not (tmp_path / "src" / "other.py").exists()


def test_same_directory_sibling_is_not_implicitly_allowed(tmp_path):
    executor = _executor(
        tmp_path,
        [_edit({"path": "src/helper.py", "content": "bad"})],
    )

    result = executor.execute(
        FakeTask(),
        allowed_changes=AllowedChangeSet(["src/app.py"]),
    )

    assert result.ok is False
    assert not (tmp_path / "src" / "helper.py").exists()


def test_existing_file_must_be_read_in_current_attempt(tmp_path):
    (tmp_path / "app.py").write_text("old", encoding="utf-8")
    executor = _executor(
        tmp_path,
        [_edit({"path": "app.py", "content": "new"})],
    )

    result = executor.execute(
        FakeTask(),
        allowed_changes=AllowedChangeSet(["app.py"]),
    )

    assert result.ok is False
    assert "observe-before-edit" in result.failure_reason
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "old"


def test_read_from_prior_attempt_does_not_count(tmp_path):
    (tmp_path / "app.py").write_text("old", encoding="utf-8")
    executor = _executor(
        tmp_path,
        [
            _read("app.py"),
            json.dumps({"action": "unknown"}),
            json.dumps({"action": "unknown"}),
            json.dumps({"action": "unknown"}),
            _edit({"path": "app.py", "content": "new"}),
        ],
    )
    scope = AllowedChangeSet(["app.py"])

    first = executor.execute(FakeTask(), allowed_changes=scope)
    second = executor.execute(FakeTask(), allowed_changes=scope)

    assert first.ok is False
    assert second.ok is False
    assert "observe-before-edit" in second.failure_reason
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "old"


def test_safe_exact_delete(tmp_path):
    (tmp_path / "obsolete.txt").write_text("old", encoding="utf-8")
    executor = _executor(
        tmp_path,
        [
            _read("obsolete.txt"),
            _edit({"path": "obsolete.txt", "operation": "delete"}),
        ],
    )

    result = executor.execute(
        FakeTask(),
        allowed_changes=AllowedChangeSet(["obsolete.txt"]),
    )

    assert result.ok is True
    assert not (tmp_path / "obsolete.txt").exists()
    assert "deleted obsolete.txt" in result.evidence


def test_missing_delete_is_rejected(tmp_path):
    executor = _executor(
        tmp_path,
        [_edit({"path": "missing.txt", "operation": "delete"})],
    )

    result = executor.execute(
        FakeTask(),
        allowed_changes=AllowedChangeSet(["missing.txt"]),
    )

    assert result.ok is False
    assert "does not exist" in result.failure_reason


def test_directory_delete_is_rejected(tmp_path):
    (tmp_path / "folder").mkdir()
    executor = _executor(
        tmp_path,
        [_edit({"path": "folder", "operation": "delete"})],
    )

    result = executor.execute(
        FakeTask(),
        allowed_changes=AllowedChangeSet(["folder"]),
    )

    assert result.ok is False
    assert "regular file" in result.failure_reason
    assert (tmp_path / "folder").is_dir()


def test_symlink_delete_is_rejected(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("keep", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        import pytest

        pytest.skip("symlink creation is unavailable")
    executor = _executor(
        tmp_path,
        [_edit({"path": "link.txt", "operation": "delete"})],
    )

    result = executor.execute(
        FakeTask(),
        allowed_changes=AllowedChangeSet(["link.txt"]),
    )

    assert result.ok is False
    assert "link or reparse point" in result.failure_reason
    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "keep"


def test_multi_file_apply_rolls_back_when_one_mutation_fails(
    tmp_path, monkeypatch
):
    executor = _executor(
        tmp_path,
        [
            _edit(
                {"path": "one.txt", "content": "one"},
                {"path": "two.txt", "content": "two"},
            )
        ],
    )
    original = executor.workspace.files.write_text

    def fail_second(path, content):
        if str(path).replace("\\", "/") == "two.txt":
            raise OSError("simulated write failure")
        return original(path, content)

    monkeypatch.setattr(executor.workspace.files, "write_text", fail_second)

    result = executor.execute(
        FakeTask(),
        allowed_changes=AllowedChangeSet(["one.txt", "two.txt"]),
    )

    assert result.ok is False
    assert "simulated write failure" in result.failure_reason
    assert not (tmp_path / "one.txt").exists()
    assert not (tmp_path / "two.txt").exists()
