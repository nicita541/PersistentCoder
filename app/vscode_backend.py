from __future__ import annotations

import json
import os
import sys

from contextlib import redirect_stdout
from pathlib import Path
from typing import TextIO

from app.agent.runtime import AgentRuntime
from app.sandbox.paths import configure_project_env


PROTOCOL_VERSION = 4

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

    if message_type in {"inspect", "apply", "discard"}:
        return {
            "type": message_type,
            "request_id": _require_string(value, "request_id"),
            "project_root": _require_string(value, "project_root"),
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

    preview = None
    preview_method = getattr(runtime, "patch_preview", None)
    if callable(preview_method):
        try:
            candidate = preview_method()
            if isinstance(candidate, dict):
                preview = candidate
        except Exception:
            preview = None

    if preview is not None:
        preview_changed = preview.get("changed_files")
        if isinstance(preview_changed, list):
            changed_files = [str(path) for path in preview_changed]
        patch_path = preview.get("patch") or patch_path

    phase_value = _phase_value(state)
    manifest_id = preview.get("manifest_id") if preview is not None else None
    can_apply = preview.get("can_apply") if preview is not None else None
    if phase_value != "DONE":
        workflow_status = "failed"
        workflow_message = "Task did not complete successfully"
        next_actions = ["discard"]
    elif verification_data is not None and verification_data.get("ok") is not True:
        workflow_status = "needs_attention"
        workflow_message = str(
            verification_data.get("reason")
            or "Verification did not pass"
        )
        next_actions = ["discard"]
    elif manifest_id is not None and can_apply is True:
        workflow_status = "verified"
        workflow_message = "Verified changes are ready to apply"
        next_actions = ["apply", "discard"]
    elif manifest_id is not None:
        workflow_status = "needs_attention"
        workflow_message = str(
            preview.get("manifest_reason")
            or "Verified result contains blocked changes"
        )
        next_actions = ["discard"]
    else:
        workflow_status = "verified"
        workflow_message = "Task completed with no applicable changes"
        next_actions = []

    return {
        "run_id": getattr(
            runtime,
            "last_run_id",
            None,
        ),

        "phase": phase_value,

        "workflow_status": workflow_status,

        "message": workflow_message,

        "next_actions": next_actions,

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

        "manifest_id": (
            manifest_id
        ),

        "can_apply": (
            can_apply
        ),

        "apply_block_reason": (
            preview.get("manifest_reason")
            if preview is not None
            else None
        ),

        "change_entries": (
            preview.get("entries", [])
            if preview is not None
            else []
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

        if result.get("can_apply") is False:
            auto_apply["reason"] = (
                str(result.get("apply_block_reason") or "manifest is not apply-safe")
                + "; "
                "auto-apply blocked"
            )

            return result

        if (
            result.get("manifest_id") is None
            and not result.get("patch_path")
        ):
            auto_apply["reason"] = (
                "no verified result was produced; "
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

        if auto_apply["applied"]:
            result["workflow_status"] = "applied"
            result["message"] = "Verified changes were applied"
            result["next_actions"] = []
        else:
            result["workflow_status"] = "needs_attention"
            result["message"] = auto_apply["reason"] or "Apply was not completed"

        return result

    def apply_verified(self, project_root: str) -> dict[str, object]:
        runtime = self._get_runtime(project_root)
        with redirect_stdout(sys.stderr):
            applied = runtime.apply_patch(confirmed=True)
        files = [str(path) for path in applied.get("applied", [])]
        status = str(applied.get("status") or "")
        return {
            "action": "apply",
            "ok": status == "COMMITTED",
            "workflow_status": (
                "applied" if status == "COMMITTED" else "needs_attention"
            ),
            "message": str(applied.get("reason") or status or "Apply failed"),
            "files": files,
        }

    def inspect(self, project_root: str) -> dict[str, object]:
        runtime = self._get_runtime(project_root)
        with redirect_stdout(sys.stderr):
            status = runtime.run_status()
            preview = runtime.patch_preview()

        session_status = str(status.get("session_status") or "CLEAN")
        manifest_id = preview.get("manifest_id")
        can_apply = preview.get("can_apply")
        reason = preview.get("manifest_reason")

        if session_status == "DIRTY_VERIFIED" and manifest_id is not None:
            if can_apply is True:
                workflow_status = "verified"
                message = "Recovered verified changes are ready to apply"
                next_actions = ["apply", "discard"]
            else:
                workflow_status = "needs_attention"
                message = str(reason or "Recovered changes cannot be applied")
                next_actions = ["discard"]
        elif session_status == "DIRTY_FAILED":
            workflow_status = "needs_attention"
            message = "Recovered unsuccessful changes are available to discard"
            next_actions = ["discard"]
        else:
            workflow_status = "idle"
            message = "Project is ready"
            next_actions = []

        return {
            "run_id": status.get("run_id"),
            "phase": str(status.get("phase") or "UNKNOWN"),
            "workflow_status": workflow_status,
            "message": message,
            "next_actions": next_actions,
            "plan_id": status.get("plan_id"),
            "global_goal": None,
            "completion": None,
            "patch_path": preview.get("patch"),
            "manifest_id": manifest_id,
            "can_apply": can_apply,
            "apply_block_reason": reason,
            "change_entries": preview.get("entries", []),
            "read_files": [],
            "changed_files": preview.get("changed_files", []),
            "commands": [],
            "verification": None,
            "repair": None,
            "work_mode": WORK_MODE_SANDBOX,
            "auto_apply": _empty_auto_apply_result(),
        }

    def discard(self, project_root: str) -> dict[str, object]:
        runtime = self._get_runtime(project_root)
        with redirect_stdout(sys.stderr):
            runtime.discard_session()
        return {
            "action": "discard",
            "ok": True,
            "workflow_status": "discarded",
            "message": "Sandbox changes were discarded",
            "files": [],
        }


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

                continue

            if message_type == "inspect":
                assert request_id is not None
                try:
                    project_state = backend.inspect(
                        str(message["project_root"])
                    )
                except Exception as error:
                    _send(
                        stdout,
                        {
                            "type": "run_failed",
                            "request_id": request_id,
                            "error": str(error),
                        },
                    )
                    continue
                _send(
                    stdout,
                    {
                        "type": "project_state",
                        "request_id": request_id,
                        "result": project_state,
                    },
                )
                continue

            if message_type in {"apply", "discard"}:
                assert request_id is not None
                _send(
                    stdout,
                    {
                        "type": "action_started",
                        "request_id": request_id,
                        "action": message_type,
                    },
                )
                try:
                    if message_type == "apply":
                        action_result = backend.apply_verified(
                            str(message["project_root"])
                        )
                    else:
                        action_result = backend.discard(
                            str(message["project_root"])
                        )
                except Exception as error:
                    _send(
                        stdout,
                        {
                            "type": "action_failed",
                            "request_id": request_id,
                            "action": message_type,
                            "error": str(error),
                        },
                    )
                    continue
                _send(
                    stdout,
                    {
                        "type": "action_completed",
                        "request_id": request_id,
                        "result": action_result,
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
