from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from app.vscode_backend import (
    ProtocolError,
    parse_client_message,
    serve,
    state_to_result,
)


def test_parse_ping_message() -> None:
    message = parse_client_message(
        json.dumps(
            {
                "type": "ping",
                "request_id": "abc",
            }
        )
    )

    assert message == {
        "type": "ping",
        "request_id": "abc",
    }


def test_parse_run_message() -> None:
    message = parse_client_message(
        json.dumps(
            {
                "type": "run",
                "request_id": "run-1",
                "request": "Исправь тест",
                "project_root": r"F:\Project",
            },
            ensure_ascii=False,
        )
    )

    assert message["type"] == "run"
    assert message["request_id"] == "run-1"
    assert message["request"] == "Исправь тест"
    assert message["project_root"] == r"F:\Project"


def test_empty_request_is_rejected() -> None:
    with pytest.raises(
        ProtocolError,
        match="request is required",
    ):
        parse_client_message(
            json.dumps(
                {
                    "type": "run",
                    "request_id": "run-1",
                    "request": "   ",
                    "project_root": r"F:\Project",
                }
            )
        )


def test_unknown_message_is_rejected() -> None:
    with pytest.raises(
        ProtocolError,
        match="unknown message type",
    ):
        parse_client_message(
            json.dumps(
                {
                    "type": "destroy_windows",
                }
            )
        )


def test_state_to_result_is_json_safe() -> None:
    state = SimpleNamespace(
        phase=SimpleNamespace(
            value="DONE",
        ),
        plan_id=42,
        global_goal="Исправить ошибку",
        completion="Готово",
        patch_path=r"F:\PersistentCoder\.sandbox\patch.patch",
        execution=SimpleNamespace(
            read_files=[
                "app/test.py",
            ],
            artifacts=[
                "app/test.py",
            ],
            commands=[
                SimpleNamespace(
                    command="python -m pytest -q",
                    returncode=0,
                )
            ],
        ),
        verification=SimpleNamespace(
            ok=True,
            status="PASS",
            reason="all criteria passed",
            criterion_results=[
                SimpleNamespace(
                    criterion="tests pass",
                    status="PASS",
                    check="pytest",
                    reason="",
                    evidence=[
                        "1 passed",
                    ],
                )
            ],
        ),
        repair=SimpleNamespace(
            required=False,
            action=None,
            scope=None,
            reason=None,
        ),
    )

    runtime = SimpleNamespace(
        last_run_id=7,
        last_patch_path=state.patch_path,
    )

    result = state_to_result(
        state,
        runtime,
    )

    assert result["phase"] == "DONE"
    assert result["plan_id"] == 42
    assert result["verification"]["status"] == "PASS"
    assert result["changed_files"] == [
        "app/test.py",
    ]

    json.dumps(
        result,
        ensure_ascii=False,
    )


class FakeSession:
    def execute(
        self,
        request: str,
        project_root: str,
    ) -> dict[str, object]:
        assert request == "Исправь баг"
        assert project_root == r"F:\Demo"

        return {
            "phase": "DONE",
            "plan_id": 1,
            "patch_path": "patch.diff",
        }


def test_json_lines_server_protocol() -> None:
    stdin = io.StringIO(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "ping",
                        "request_id": "ping-1",
                    }
                ),
                json.dumps(
                    {
                        "type": "run",
                        "request_id": "run-1",
                        "request": "Исправь баг",
                        "project_root": r"F:\Demo",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "type": "shutdown",
                    }
                ),
            ]
        )
        + "\n"
    )

    stdout = io.StringIO()

    serve(
        stdin=stdin,
        stdout=stdout,
        session=FakeSession(),
    )

    responses = [
        json.loads(line)
        for line in stdout.getvalue().splitlines()
        if line.strip()
    ]

    assert responses[0]["type"] == "ready"

    assert responses[1] == {
        "type": "pong",
        "request_id": "ping-1",
    }

    assert responses[2] == {
        "type": "run_started",
        "request_id": "run-1",
    }

    assert responses[3]["type"] == "run_completed"
    assert responses[3]["request_id"] == "run-1"
    assert responses[3]["result"]["phase"] == "DONE"

    assert responses[4]["type"] == "shutdown_complete"