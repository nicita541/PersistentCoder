from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent.coder.agent import CodingAgent
from app.agent.coder.executor import CodeExecutor
from app.agent.coder.workspace import Workspace
from app.context.builder import ContextBuilder
from app.sandbox.paths import (
    DATA_ROOT,
    DATABASE_PATH,
    HF_CACHE,
    MODELS_ROOT,
    PROJECT_ROOT,
    SANDBOX_ROOT,
    TMP_ROOT,
    is_within_project,
)
from app.sandbox.policy import (
    PathPolicy,
    PolicyViolation,
)
from app.tasks.store import DEFAULT_DATABASE_PATH
from app.tools.file_tools import (
    FileTools,
    FileToolsError,
)


class _FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)

    def chat(
        self,
        messages,
        max_new_tokens: int = 512,
    ) -> str:
        return self.responses.pop(0)


class _Task:
    key = "build"
    title = "Build"
    description = "Build."
    success_criteria = ["done"]


def _envelope(
    path: str,
    content: str = "x",
) -> str:
    return json.dumps(
        {
            "files": [
                {"path": path, "content": content}
            ],
            "commands": [],
        }
    )


def _coder(tmp_path, path: str) -> CodingAgent:
    workspace = Workspace(tmp_path)

    executor = CodeExecutor(
        workspace=workspace,
        llm=_FakeLLM([_envelope(path)]),
        context=ContextBuilder(
            project=workspace.project
        ),
    )

    return CodingAgent(executor)


# 1
def test_write_to_c_drive_is_blocked(tmp_path):
    tools = FileTools(tmp_path)

    with pytest.raises(FileToolsError):
        tools.write_text(r"C:\outside.txt", "x")

    assert not Path(r"C:\outside.txt").exists()


# 2
def test_write_to_d_drive_is_blocked(tmp_path):
    tools = FileTools(tmp_path)

    with pytest.raises(FileToolsError):
        tools.write_text(r"D:\outside.txt", "x")

    assert not Path(r"D:\outside.txt").exists()


# 3
def test_write_outside_project_is_blocked(tmp_path):
    tools = FileTools(tmp_path)

    with pytest.raises(FileToolsError):
        tools.write_text(r"F:\outside_project.txt", "x")

    assert not Path(r"F:\outside_project.txt").exists()


# 4
def test_parent_traversal_is_blocked(tmp_path):
    tools = FileTools(tmp_path)

    with pytest.raises(FileToolsError):
        tools.write_text("../escape.txt", "x")

    with pytest.raises(FileToolsError):
        tools.read_text("../../etc/passwd")


@pytest.mark.parametrize(
    "pattern",
    [
        "../*",
        "../../**/*",
        r"C:\\*",
        "/tmp/*",
        "dir/file:stream",
        "CON/**",
    ],
)
def test_list_files_cannot_glob_outside_workspace(tmp_path, pattern):
    with pytest.raises(FileToolsError):
        FileTools(tmp_path).list_files(pattern)


def test_absolute_path_rejected_even_inside_root(
    tmp_path,
):
    policy = PathPolicy(tmp_path)

    with pytest.raises(PolicyViolation):
        policy.resolve(str(tmp_path / "inside.txt"))


# 5
def test_coding_agent_accepts_only_relative_paths(
    tmp_path,
):
    blocked = _coder(
        tmp_path,
        r"F:\PersistentCoder\src\main.py",
    )

    result = blocked.execute(_Task())

    assert result.ok is False
    assert not Path(
        r"F:\PersistentCoder\src\main.py"
    ).exists()

    allowed = _coder(tmp_path, "src/main.py")

    result_ok = allowed.execute(_Task())

    assert result_ok.ok is True
    assert (tmp_path / "src" / "main.py").exists()


# 7
def test_model_cache_paths_are_project_local():
    assert is_within_project(HF_CACHE)
    assert is_within_project(MODELS_ROOT)
    assert HF_CACHE == (
        PROJECT_ROOT / "models" / "huggingface"
    )

    from app.llm.client import (
        MODEL_CACHE,
        MODEL_NAME,
    )

    assert MODEL_CACHE == HF_CACHE
    assert MODEL_NAME == (
        "Qwen/Qwen2.5-Coder-1.5B-Instruct"
    )


# 8
def test_database_is_under_project_data():
    assert DATA_ROOT == PROJECT_ROOT / "data"
    assert is_within_project(DEFAULT_DATABASE_PATH)
    assert is_within_project(DATABASE_PATH)
    assert DEFAULT_DATABASE_PATH.parent == DATA_ROOT


# 9
def test_sandbox_metadata_is_under_project_sandbox():
    assert SANDBOX_ROOT == PROJECT_ROOT / ".sandbox"
    assert is_within_project(SANDBOX_ROOT)
    assert is_within_project(TMP_ROOT)
