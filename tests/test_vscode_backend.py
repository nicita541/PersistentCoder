from __future__ import annotations

import io
import json

from types import SimpleNamespace

import pytest

from app.vscode_backend import (
    BackendSession,
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


def test_parse_run_message_with_sandbox_mode() -> None:
    message = parse_client_message(
        json.dumps(
            {
                "type": "run",
                "request_id": "run-1",
                "request": "Исправь тест",
                "project_root": r"F:\Project",
                "work_mode": "sandbox",
            },
            ensure_ascii=False,
        )
    )

    assert message["type"] == "run"
    assert message["request_id"] == "run-1"
    assert message["request"] == "Исправь тест"
    assert message["project_root"] == r"F:\Project"
    assert message["work_mode"] == "sandbox"


def test_parse_run_message_with_auto_apply_mode() -> None:
    message = parse_client_message(
        json.dumps(
            {
                "type": "run",
                "request_id": "run-2",
                "request": "Исправь баг",
                "project_root": r"F:\Project",
                "work_mode": "auto_apply",
            },
            ensure_ascii=False,
        )
    )

    assert message["work_mode"] == "auto_apply"


def test_missing_work_mode_is_rejected() -> None:
    with pytest.raises(
        ProtocolError,
        match="work_mode",
    ):
        parse_client_message(
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


def test_unknown_work_mode_is_rejected() -> None:
    with pytest.raises(
        ProtocolError,
        match="work_mode",
    ):
        parse_client_message(
            json.dumps(
                {
                    "type": "run",
                    "request_id": "run-1",
                    "request": "Исправь тест",
                    "project_root": r"F:\Project",
                    "work_mode": "real_direct_host_write",
                },
                ensure_ascii=False,
            )
        )


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
                    "work_mode": "sandbox",
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


def make_state(
    *,
    phase: str = "DONE",
    verification_ok: bool = True,
    verification_status: str = "PASS",
    patch_path: str | None = "patch.diff",
):
    return SimpleNamespace(
        phase=SimpleNamespace(
            value=phase,
        ),
        plan_id=42,
        global_goal="Исправить ошибку",
        completion="Готово",
        patch_path=patch_path,
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
            ok=verification_ok,
            status=verification_status,
            reason=(
                "all criteria passed"
                if verification_ok
                else "verification failed"
            ),
            criterion_results=[
                SimpleNamespace(
                    criterion="tests pass",
                    status=verification_status,
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


def test_state_to_result_is_json_safe() -> None:
    state = make_state()

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

    assert (
        result["verification"]["status"]
        == "PASS"
    )

    assert result["changed_files"] == [
        "app/test.py",
    ]

    json.dumps(
        result,
        ensure_ascii=False,
    )


class FakeRuntime:
    def __init__(
        self,
        state,
    ) -> None:
        self.state = state

        self.last_run_id = 1
        self.last_patch_path = (
            state.patch_path
        )

        self.apply_calls = 0

        self.apply_result = {
            "applied": [
                "app/test.py",
            ],
            "reason": "applied",
        }

    def run(
        self,
        request: str,
    ):
        assert request

        return self.state

    def apply_patch(
        self,
        *,
        confirmed: bool = False,
    ):
        assert confirmed is True

        self.apply_calls += 1

        return self.apply_result


def session_with_runtime(
    runtime: FakeRuntime,
    monkeypatch,
) -> BackendSession:
    session = BackendSession()

    monkeypatch.setattr(
        session,
        "_get_runtime",
        lambda project_root: runtime,
    )

    return session


def test_sandbox_mode_never_auto_applies(
    monkeypatch,
) -> None:
    runtime = FakeRuntime(
        make_state()
    )

    session = session_with_runtime(
        runtime,
        monkeypatch,
    )

    result = session.execute(
        "Исправь баг",
        r"F:\Demo",
        "sandbox",
    )

    assert runtime.apply_calls == 0

    assert result["work_mode"] == "sandbox"

    assert result["auto_apply"] == {
        "attempted": False,
        "applied": False,
        "files": [],
        "reason": None,
    }


def test_auto_apply_runs_after_done_and_pass(
    monkeypatch,
) -> None:
    runtime = FakeRuntime(
        make_state(
            phase="DONE",
            verification_ok=True,
            verification_status="PASS",
            patch_path="patch.diff",
        )
    )

    session = session_with_runtime(
        runtime,
        monkeypatch,
    )

    result = session.execute(
        "Исправь баг",
        r"F:\Demo",
        "auto_apply",
    )

    assert runtime.apply_calls == 1

    assert (
        result["work_mode"]
        == "auto_apply"
    )

    assert result["auto_apply"] == {
        "attempted": True,
        "applied": True,
        "files": [
            "app/test.py",
        ],
        "reason": "applied",
    }


def test_auto_apply_never_runs_when_phase_failed(
    monkeypatch,
) -> None:
    runtime = FakeRuntime(
        make_state(
            phase="FAILED",
            verification_ok=False,
            verification_status="FAIL",
            patch_path=None,
        )
    )

    session = session_with_runtime(
        runtime,
        monkeypatch,
    )

    result = session.execute(
        "Исправь баг",
        r"F:\Demo",
        "auto_apply",
    )

    assert runtime.apply_calls == 0

    assert result["auto_apply"]["attempted"] is True
    assert result["auto_apply"]["applied"] is False

    assert "DONE" in str(
        result["auto_apply"]["reason"]
    )


def test_auto_apply_never_runs_when_verification_blocked(
    monkeypatch,
) -> None:
    runtime = FakeRuntime(
        make_state(
            phase="DONE",
            verification_ok=False,
            verification_status="BLOCKED",
            patch_path="patch.diff",
        )
    )

    session = session_with_runtime(
        runtime,
        monkeypatch,
    )

    result = session.execute(
        "Исправь баг",
        r"F:\Demo",
        "auto_apply",
    )

    assert runtime.apply_calls == 0

    assert result["auto_apply"]["attempted"] is True
    assert result["auto_apply"]["applied"] is False

    assert "verification" in str(
        result["auto_apply"]["reason"]
    ).casefold()


class FakeSession:
    def execute(
        self,
        request: str,
        project_root: str,
        work_mode: str,
    ) -> dict[str, object]:
        assert request == "Исправь баг"
        assert project_root == r"F:\Demo"
        assert work_mode == "sandbox"

        return {
            "phase": "DONE",
            "plan_id": 1,
            "patch_path": "patch.diff",
            "work_mode": work_mode,
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
                        "work_mode": "sandbox",
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
        for line in stdout
        .getvalue()
        .splitlines()
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

    assert (
        responses[3]["type"]
        == "run_completed"
    )

    assert (
        responses[3]["request_id"]
        == "run-1"
    )

    assert (
        responses[3]["result"]["phase"]
        == "DONE"
    )

    assert (
        responses[3]["result"]["work_mode"]
        == "sandbox"
    )

    assert (
        responses[4]["type"]
        == "shutdown_complete"
    )