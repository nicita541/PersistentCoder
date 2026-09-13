from __future__ import annotations

import json
import os
import sys

from contextlib import redirect_stdout
from pathlib import Path
from typing import TextIO

from app.agent.runtime import AgentRuntime
from app.sandbox.paths import configure_project_env


PROTOCOL_VERSION = 2

WORK_MODE_SANDBOX = "sandbox"
WORK_MODE_AUTO_APPLY = "auto_apply"

ALLOWED_WORK_MODES = frozenset(
    {
        WORK_MODE_SANDBOX,
        WORK_MODE_AUTO_APPLY,
    }
)


# stdout принадлежит JSONL protocol.
# Любые обычные print() внутри runtime уходят в stderr.
_PROTOCOL_STDOUT = sys.stdout


class ProtocolError(ValueError):
    pass


def _require_string(
    data: dict[str, object],
    key: str,
) -> str:
    value = data.get(key)

    if not isinstance(value, str):
        raise ProtocolError(
            f"{key} must be a string"
        )

    value = value.strip()

    if not value:
        raise ProtocolError(
            f"{key} is required"
        )

    return value


def _require_work_mode(
    data: dict[str, object],
) -> str:
    value = _require_string(
        data,
        "work_mode",
    )

    if value not in ALLOWED_WORK_MODES:
        raise ProtocolError(
            "work_mode must be one of: "
            "sandbox, auto_apply"
        )

    return value


def parse_client_message(
    line: str,
) -> dict[str, object]:
    try:
        value = json.loads(line)

    except json.JSONDecodeError as error:
        raise ProtocolError(
            "invalid JSON"
        ) from error

    if not isinstance(value, dict):
        raise ProtocolError(
            "message must be a JSON object"
        )

    message_type = value.get("type")

    if not isinstance(
        message_type,
        str,
    ):
        raise ProtocolError(
            "message type is required"
        )

    if message_type == "ping":
        return {
            "type": "ping",
            "request_id": _require_string(
                value,
                "request_id",
            ),
        }

    if message_type == "shutdown":
        return {
            "type": "shutdown",
        }

    if message_type == "run":
        return {
            "type": "run",

            "request_id": _require_string(
                value,
                "request_id",
            ),

            "request": _require_string(
                value,
                "request",
            ),

            "project_root": _require_string(
                value,
                "project_root",
            ),

            "work_mode": _require_work_mode(
                value
            ),
        }

    raise ProtocolError(
        f"unknown message type: {message_type}"
    )


def _phase_value(
    state,
) -> str:
    phase = getattr(
        state,
        "phase",
        None,
    )

    if phase is None:
        return "UNKNOWN"

    return str(
        getattr(
            phase,
            "value",
            phase,
        )
    )


def state_to_result(
    state,
    runtime,
) -> dict[str, object]:
    execution = getattr(
        state,
        "execution",
        None,
    )

    verification = getattr(
        state,
        "verification",
        None,
    )

    repair = getattr(
        state,
        "repair",
        None,
    )

    commands: list[
        dict[str, object]
    ] = []

    read_files: list[str] = []
    changed_files: list[str] = []

    if execution is not None:
        read_files = [
            str(path)
            for path in (
                getattr(
                    execution,
                    "read_files",
                    [],
                )
                or []
            )
        ]

        changed_files = [
            str(path)
            for path in (
                getattr(
                    execution,
                    "artifacts",
                    [],
                )
                or []
            )
        ]

        for command in (
            getattr(
                execution,
                "commands",
                [],
            )
            or []
        ):
            commands.append(
                {
                    "command": str(
                        getattr(
                            command,
                            "command",
                            "",
                        )
                    ),

                    "returncode": int(
                        getattr(
                            command,
                            "returncode",
                            -1,
                        )
                    ),
                }
            )

    verification_data: (
        dict[str, object]
        | None
    ) = None

    if verification is not None:
        criteria: list[
            dict[str, object]
        ] = []

        for criterion in (
            getattr(
                verification,
                "criterion_results",
                [],
            )
            or []
        ):
            criteria.append(
                {
                    "criterion": str(
                        getattr(
                            criterion,
                            "criterion",
                            "",
                        )
                    ),

                    "status": str(
                        getattr(
                            criterion,
                            "status",
                            "",
                        )
                    ),

                    "check": str(
                        getattr(
                            criterion,
                            "check",
                            "",
                        )
                    ),

                    "reason": str(
                        getattr(
                            criterion,
                            "reason",
                            "",
                        )
                    ),

                    "evidence": [
                        str(item)
                        for item in (
                            getattr(
                                criterion,
                                "evidence",
                                [],
                            )
                            or []
                        )
                    ],
                }
            )

        verification_data = {
            "ok": bool(
                getattr(
                    verification,
                    "ok",
                    False,
                )
            ),

            "status": str(
                getattr(
                    verification,
                    "status",
                    "",
                )
            ),

            "reason": str(
                getattr(
                    verification,
                    "reason",
                    "",
                )
            ),

            "criteria": criteria,
        }

    repair_data: (
        dict[str, object]
        | None
    ) = None

    if repair is not None:
        repair_data = {
            "required": bool(
                getattr(
                    repair,
                    "required",
                    False,
                )
            ),

            "action": getattr(
                repair,
                "action",
                None,
            ),

            "scope": getattr(
                repair,
                "scope",
                None,
            ),

            "reason": getattr(
                repair,
                "reason",
                None,
            ),
        }

    patch_path = (
        getattr(
            state,
            "patch_path",
            None,
        )
        or getattr(
            runtime,
            "last_patch_path",
            None,
        )
    )

    return {
        "run_id": getattr(
            runtime,
            "last_run_id",
            None,
        ),

        "phase": _phase_value(
            state
        ),

        "plan_id": getattr(
            state,
            "plan_id",
            None,
        ),

        "global_goal": getattr(
            state,
            "global_goal",
            None,
        ),

        "completion": getattr(
            state,
            "completion",
            None,
        ),

        "patch_path": (
            str(patch_path)
            if patch_path
            else None
        ),

        "read_files": read_files,
        "changed_files": changed_files,
        "commands": commands,

        "verification": (
            verification_data
        ),

        "repair": (
            repair_data
        ),
    }


def _empty_auto_apply_result() -> dict[str, object]:
    return {
        "attempted": False,
        "applied": False,
        "files": [],
        "reason": None,
    }


class BackendSession:
    """
    Один локальный Python backend процесс.

    Runtime создаётся лениво при первом RUN.

    Backend process привязан ровно к одному source project.
    Если VS Code переключился на другой workspace,
    backend должен быть перезапущен.
    """

    def __init__(self) -> None:
        self.runtime: (
            AgentRuntime
            | None
        ) = None

        self.project_root: (
            Path
            | None
        ) = None

    @staticmethod
    def _resolve_project_root(
        raw: str,
    ) -> Path:
        root = Path(
            raw
        ).expanduser().resolve()

        if not root.exists():
            raise RuntimeError(
                "project_root does not exist: "
                f"{root}"
            )

        if not root.is_dir():
            raise RuntimeError(
                "project_root is not a directory: "
                f"{root}"
            )

        return root

    def _get_runtime(
        self,
        project_root: str,
    ) -> AgentRuntime:
        root = (
            self._resolve_project_root(
                project_root
            )
        )

        if self.runtime is None:
            self.project_root = root

            with redirect_stdout(
                sys.stderr
            ):
                self.runtime = AgentRuntime(
                    project_root=root,
                )

            return self.runtime

        if (
            self.project_root is None
            or root != self.project_root
        ):
            raise RuntimeError(
                "VS Code workspace changed while "
                "PersistentCoder backend is running. "
                "Restart the extension host."
            )

        return self.runtime

    def execute(
        self,
        request: str,
        project_root: str,
        work_mode: str,
    ) -> dict[str, object]:
        if work_mode not in ALLOWED_WORK_MODES:
            raise RuntimeError(
                "unsupported work mode"
            )

        runtime = self._get_runtime(
            project_root
        )

        with redirect_stdout(
            sys.stderr
        ):
            state = runtime.run(
                request
            )

        result = state_to_result(
            state,
            runtime,
        )

        result["work_mode"] = (
            work_mode
        )

        auto_apply = (
            _empty_auto_apply_result()
        )

        result["auto_apply"] = (
            auto_apply
        )

        if (
            work_mode
            != WORK_MODE_AUTO_APPLY
        ):
            return result

        # Direct mode in the UI means:
        #
        # sandbox
        # -> verification
        # -> patch
        # -> framework-controlled auto apply
        #
        # It NEVER means direct LLM host writes.
        auto_apply["attempted"] = True

        if result["phase"] != "DONE":
            auto_apply["reason"] = (
                "run is not DONE; "
                "auto-apply blocked"
            )

            return result

        verification = result.get(
            "verification"
        )

        if (
            not isinstance(
                verification,
                dict,
            )
            or verification.get("ok")
            is not True
        ):
            auto_apply["reason"] = (
                "verification did not PASS; "
                "auto-apply blocked"
            )

            return result

        if not result.get(
            "patch_path"
        ):
            auto_apply["reason"] = (
                "no patch was produced; "
                "auto-apply blocked"
            )

            return result

        try:
            with redirect_stdout(
                sys.stderr
            ):
                apply_result = (
                    runtime.apply_patch(
                        confirmed=True
                    )
                )

        except Exception as error:
            auto_apply["reason"] = (
                "apply failed: "
                f"{error}"
            )

            return result

        if not isinstance(
            apply_result,
            dict,
        ):
            auto_apply["reason"] = (
                "invalid apply result"
            )

            return result

        raw_files = apply_result.get(
            "applied",
            [],
        )

        files = (
            [
                str(path)
                for path in raw_files
            ]
            if isinstance(
                raw_files,
                list,
            )
            else []
        )

        auto_apply["files"] = files
        auto_apply["applied"] = bool(
            files
        )

        auto_apply["reason"] = str(
            apply_result.get(
                "reason",
                "",
            )
            or ""
        )

        return result


def _send(
    stream: TextIO,
    message: dict[str, object],
) -> None:
    stream.write(
        json.dumps(
            message,
            ensure_ascii=False,
            separators=(
                ",",
                ":",
            ),
        )
    )

    stream.write("\n")
    stream.flush()


def serve(
    *,
    stdin: TextIO,
    stdout: TextIO,
    session=None,
) -> None:
    backend = (
        session
        if session is not None
        else BackendSession()
    )

    _send(
        stdout,
        {
            "type": "ready",

            "protocol_version": (
                PROTOCOL_VERSION
            ),

            "pid": os.getpid(),
        },
    )

    for raw_line in stdin:
        line = raw_line.strip()

        if not line:
            continue

        request_id: (
            str
            | None
        ) = None

        try:
            message = (
                parse_client_message(
                    line
                )
            )

            request_id_value = (
                message.get(
                    "request_id"
                )
            )

            if isinstance(
                request_id_value,
                str,
            ):
                request_id = (
                    request_id_value
                )

            message_type = (
                message["type"]
            )

            if message_type == "ping":
                _send(
                    stdout,
                    {
                        "type": "pong",

                        "request_id": (
                            request_id
                        ),
                    },
                )

                continue

            if message_type == "shutdown":
                _send(
                    stdout,
                    {
                        "type":
                            "shutdown_complete",
                    },
                )

                return

            if message_type == "run":
                assert request_id is not None

                _send(
                    stdout,
                    {
                        "type":
                            "run_started",

                        "request_id":
                            request_id,
                    },
                )

                try:
                    result = backend.execute(
                        str(
                            message[
                                "request"
                            ]
                        ),

                        str(
                            message[
                                "project_root"
                            ]
                        ),

                        str(
                            message[
                                "work_mode"
                            ]
                        ),
                    )

                except Exception as error:
                    _send(
                        stdout,
                        {
                            "type":
                                "run_failed",

                            "request_id":
                                request_id,

                            "error": str(
                                error
                            ),
                        },
                    )

                    continue

                _send(
                    stdout,
                    {
                        "type":
                            "run_completed",

                        "request_id":
                            request_id,

                        "result":
                            result,
                    },
                )

        except ProtocolError as error:
            _send(
                stdout,
                {
                    "type":
                        "protocol_error",

                    "request_id":
                        request_id,

                    "error":
                        str(error),
                },
            )


def main() -> int:
    configure_project_env()

    serve(
        stdin=sys.stdin,
        stdout=_PROTOCOL_STDOUT,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )