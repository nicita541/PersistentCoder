from __future__ import annotations

import time

from app.agent.state import (
    CommandExecution,
    ExecutionResult,
)
from app.agent.coder.protocol import ActionEnvelopeDecoder, ProtocolError
from app.agent.coder.tool_protocol import ToolProtocolError, decode_tool_calls
from app.sandbox.limits import (
    DEFAULT_LIMITS,
)
from app.sandbox.policy import (
    PolicyViolation,
)
from app.context.builder import ACTION_PROTOCOL
from app.sandbox.project_path import ProjectPath, ProjectPathError
from app.sandbox.project_tool_runner import ProjectToolRunner
from app.tasks.change_scope import AllowedChangeSet, ChangeScopeError


class CodeExecutorError(RuntimeError):
    pass


# Bounded corrective re-prompts for a malformed action envelope.
# Nothing is written during these retries: they only spend tool-loop
# iterations teaching the model the protocol.
MAX_ENVELOPE_RETRIES = 2


class CodeExecutor:
    """
    Реальный исполнитель CodingAgent.

    Поток:

        Task/Step
          -> ContextBuilder
          -> LLM (один общий client)
          -> FileTools (sandbox workspace) + SandboxCommandRunner (Docker)
          -> ExecutionResult с реальными evidence

    Запрещено возвращать успех без реальной работы:
    если модель не вернула валидный action envelope
    или ничего не было применено — ok=False.
    """

    def __init__(
        self,
        *,
        workspace,
        llm,
        context=None,
        system_prompt: str | None = None,
        max_new_tokens: int = 1024,
        run_commands: bool = True,
        command_runner: ProjectToolRunner | None = None,
        limits=DEFAULT_LIMITS,
        known_files=None,
        on_event=None,
        decoder: ActionEnvelopeDecoder | None = None,
    ) -> None:
        self.workspace = workspace
        self.llm = llm
        self.context = context
        self.system_prompt = system_prompt
        self.max_new_tokens = max_new_tokens
        self.run_commands = run_commands
        self.command_runner = command_runner
        self.limits = limits

        # Optional progress/timing sink (Agent Layer event bus).
        self.on_event = on_event
        self.decoder = decoder or ActionEnvelopeDecoder()

        # Kept as shared runtime bookkeeping for compatibility and
        # reporting only. It never bypasses current-attempt reads.
        self.known_files = (
            known_files
            if known_files is not None
            else set()
        )

    def _notify(
        self,
        name: str,
        payload: dict[str, object],
    ) -> None:
        """
        Report stage progress/timing. Never breaks the tool loop.
        """

        if self.on_event is None:
            return

        try:
            self.on_event(name, payload)

        except Exception:
            pass

    def _build_messages(
        self,
        task,
        step,
        feedback,
    ) -> list[dict[str, str]]:
        if (
            self.context is not None
            and hasattr(
                self.context,
                "build_task_messages",
            )
        ):
            project_context = ""

            # When the Context layer owns relevance selection, the
            # CodingAgent must NOT receive a whole-project dump: the
            # snapshot stays only as a fallback for direct/test wiring.
            if (
                getattr(
                    self.context,
                    "selector",
                    None,
                )
                is None
                and self.workspace is not None
                and getattr(
                    self.workspace,
                    "project",
                    None,
                )
                is not None
            ):
                project_context = (
                    self.workspace.project.snapshot()
                )

            return self.context.build_task_messages(
                task=task,
                step=step,
                project_context=project_context,
                feedback=feedback,
            )

        system_prompt = (
            self.system_prompt
            or "You are the Coding Agent."
        )

        return [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": (
                    f"Task: {task}\n"
                    "Return JSON with files/tools."
                ),
            },
        ]

    @staticmethod
    def _apply_output_policy(
        answer: str,
        system_prompt: str | None,
    ) -> str:
        if not system_prompt:
            return answer

        from app.policy.output_guard import (
            filter_model_output,
        )

        return filter_model_output(
            answer=answer,
            system_prompt=system_prompt,
        )

    def execute(
        self,
        task,
        *,
        step=None,
        feedback: str | None = None,
        allowed_changes: AllowedChangeSet | None = None,
        cancellation_token=None,
    ) -> ExecutionResult:
        observed: dict[str, str] = {}
        transcript: list[str] = []
        envelope_errors = 0
        observation_bytes = 0
        observation_attempts: dict[str, int] = {}
        observation_labels: dict[str, str] = {}

        for iteration in range(
            1,
            self.limits.max_tool_iterations + 1,
        ):
            if (
                cancellation_token is not None
                and cancellation_token.cancelled
            ):
                return self._fail(
                    "execution cancelled",
                    "execution cancelled",
                    ["cancelled"],
                    observed,
                )

            context_started = time.time()

            messages = self._build_messages(
                task,
                step,
                feedback,
            )

            self._notify(
                "context_selection",
                {
                    "iteration": iteration,
                    "duration_ms": int(
                        (
                            time.time()
                            - context_started
                        )
                        * 1000
                    ),
                    "prompt_chars": sum(
                        len(
                            str(
                                message.get(
                                    "content",
                                    "",
                                )
                            )
                        )
                        for message in messages
                    ),
                },
            )

            if transcript:
                messages = messages + [
                    {
                        "role": "user",
                        "content": (
                            "OBSERVATIONS:\n"
                            + "\n\n".join(transcript)
                        ),
                    }
                ]

            try:
                llm_started = time.time()

                raw_answer = self.llm.chat(
                    messages,
                    max_new_tokens=self.max_new_tokens,
                )

                self._notify(
                    "llm_tool_iteration",
                    {
                        "iteration": iteration,
                        "duration_ms": int(
                            (
                                time.time()
                                - llm_started
                            )
                            * 1000
                        ),
                        "answer_chars": len(
                            raw_answer or ""
                        ),
                    },
                )

            except Exception as exc:
                return self._fail(
                    "LLM call failed",
                    str(exc),
                    [f"llm_error:{type(exc).__name__}"],
                    observed,
                )

            answer = self._apply_output_policy(
                raw_answer,
                self.system_prompt,
            )

            try:
                proposal = self.decoder.decode(
                    answer,
                    allowed_changes=allowed_changes,
                )

            except ProtocolError:
                envelope_errors += 1

                if envelope_errors > MAX_ENVELOPE_RETRIES:
                    return self._fail(
                        "model did not return a valid "
                        "action envelope",
                        "invalid model response",
                        [
                            "model_response:"
                            + answer[:200]
                        ],
                        observed,
                    )

                transcript.append(
                    "SYSTEM FEEDBACK: the previous reply was "
                    "not a valid JSON object. It was probably cut "
                    "off before the closing brace because the file "
                    "was too long.\n"
                    "Reply again with a SMALLER file (only what "
                    "the current step needs) as ONE complete JSON "
                    "object.\n"
                    + ACTION_PROTOCOL
                )

                continue

            action = proposal.get("action")

            if action in ("list", "read", "search"):
                _ok, text = self._observe(
                    proposal,
                    observed,
                )

                observation_key = repr(
                    (
                        action,
                        proposal.get("path"),
                        proposal.get("pattern"),
                        proposal.get("query") or proposal.get("text"),
                    )
                )
                observation_attempts[observation_key] = (
                    observation_attempts.get(observation_key, 0) + 1
                )
                observation_labels[observation_key] = (
                    f"{action}:"
                    f"{proposal.get('path') or proposal.get('pattern') or proposal.get('query') or proposal.get('text') or ''}"
                )

                observation_bytes += len(text.encode("utf-8"))
                if observation_bytes > self.limits.max_observe_bytes:
                    return self._fail(
                        "observation limit exceeded",
                        "observation budget exceeded",
                        ["max_observe_bytes"],
                        observed,
                    )

                if action == "read":
                    path = str(proposal.get("path", ""))
                    key = ProjectPath.parse(path).comparison_key
                    self._notify(
                        "file_read",
                        {
                            "path": ProjectPath.parse(path).value,
                            "byte_count": len(
                                observed.get(key, "").encode("utf-8")
                            ),
                            "ok": _ok,
                            "duration_ms": 0,
                        },
                    )
                elif action == "list":
                    self._notify(
                        "file_list",
                        {
                            "pattern": str(proposal.get("pattern") or "**/*")[:200],
                            "result_count": max(0, len(text.splitlines()) - 1),
                            "ok": _ok,
                            "duration_ms": 0,
                        },
                    )
                else:
                    self._notify(
                        "repo_search",
                        {
                            "query": str(
                                proposal.get("query") or proposal.get("text") or ""
                            )[:200],
                            "result_count": max(0, len(text.splitlines()) - 1),
                            "ok": _ok,
                            "duration_ms": 0,
                        },
                    )

                transcript.append(text)
                if not _ok:
                    transcript.append(
                        "SYSTEM FEEDBACK: this observation failed. Do not repeat "
                        "the same observation. If the allowed target does not "
                        "exist, create it now with an edit action containing "
                        "complete file content."
                    )
                elif observation_attempts[observation_key] > 1:
                    transcript.append(
                        "SYSTEM FEEDBACK: this observation is already known. "
                        "Do not repeat it; make the requested edit now."
                    )

                continue

            if action == "edit":
                if (
                    "files" not in proposal
                    and "tools" not in proposal
                ):
                    # Valid JSON, but the model described WHAT it
                    # wants instead of providing the payload (very
                    # common with "create"/"file" shapes). Nothing is
                    # written: ask for the complete content instead.
                    envelope_errors += 1

                    if (
                        envelope_errors
                        > MAX_ENVELOPE_RETRIES
                    ):
                        return self._fail(
                            "action envelope has no "
                            "files or tools",
                            "empty action envelope",
                            ["empty_action_envelope"],
                            observed,
                        )

                    transcript.append(
                        "SYSTEM FEEDBACK: the reply described an "
                        "action but contained NO file content. "
                        "Reply again with the COMPLETE content:\n"
                        + ACTION_PROTOCOL
                    )

                    continue

                error = self._validate_envelope(
                    proposal,
                    observed,
                    allowed_changes,
                )

                if error is not None:
                    return self._fail(
                        "action envelope rejected",
                        error,
                        [f"rejected:{error}"],
                        observed,
                    )

                return self._apply_proposal(
                    proposal,
                    read_files=sorted(observed),
                    cancellation_token=cancellation_token,
                )

            # A small model often emits JSON that does not follow the
            # protocol yet. Nothing has been written, so the safe and
            # useful action is a bounded corrective re-prompt instead
            # of failing the whole attempt immediately.
            envelope_errors += 1

            if envelope_errors > MAX_ENVELOPE_RETRIES:
                return self._fail(
                    "unknown action",
                    f"unknown action: {action!r}",
                    ["unknown_action", "model_response:" + answer[:400]],
                    observed,
                )

            transcript.append(
                "SYSTEM FEEDBACK: the previous reply did not "
                "contain a usable action.\n"
                "PREVIOUS REPLY:\n"
                f"{answer[:400]}\n"
                + ACTION_PROTOCOL
            )

            continue

        repeated = [
            f"repeated_observation:{observation_labels[key]}:{count}"
            for key, count in observation_attempts.items()
            if count > 1
        ]
        return self._fail(
            "tool loop budget exceeded",
            "max tool iterations exceeded",
            ["tool_loop_budget", *repeated],
            observed,
        )

    @staticmethod
    def _fail(
        summary: str,
        reason: str,
        evidence: list[str],
        observed: dict[str, str],
    ) -> ExecutionResult:
        return ExecutionResult(
            ok=False,
            summary=summary,
            failure_reason=reason,
            evidence=evidence,
            read_files=sorted(observed),
        )


    def _observe(
        self,
        proposal: dict[str, object],
        observed: dict[str, str],
    ) -> tuple[bool, str]:
        """
        Read-only sandbox observation. Never writes anything.
        """

        action = proposal.get("action")

        if action == "list":
            pattern = proposal.get("pattern") or "**/*"

            try:
                listing = self.workspace.list_files(
                    str(pattern)
                )

            except Exception as exc:
                return False, f"LIST ERROR: {exc}"

            return True, (
                f"LIST {pattern}:\n"
                + "\n".join(listing[:200])
            )

        if action == "read":
            path = proposal.get("path")

            if not isinstance(path, str) or not path.strip():
                return False, "READ ERROR: path is required"

            if len(observed) >= self.limits.max_files_read:
                return False, (
                    "READ ERROR: max_files_read "
                    "budget exceeded"
                )

            try:
                content = self.workspace.read(
                    path.strip()
                )

            except Exception as exc:
                return False, f"READ ERROR {path}: {exc}"

            try:
                key = ProjectPath.parse(path).comparison_key
            except ProjectPathError as error:
                return False, f"READ ERROR {path}: {error}"

            observed[key] = content

            return True, (
                f"FILE {path}:\n"
                + content[: self.limits.max_read_bytes]
            )

        if action == "search":
            query = proposal.get("query") or proposal.get(
                "text"
            )

            project = getattr(
                self.workspace,
                "project",
                None,
            )

            if not isinstance(query, str) or not query:
                return False, "SEARCH ERROR: query is required"

            if project is None:
                return False, "SEARCH ERROR: no project tools"

            try:
                matches = project.search(str(query))

            except Exception as exc:
                return False, f"SEARCH ERROR: {exc}"

            return True, (
                f"SEARCH {query}:\n"
                + "\n".join(matches[:50])
            )

        return False, f"unknown observe action: {action}"

    def _validate_envelope(
        self,
        proposal: dict[str, object],
        observed: dict[str, str],
        allowed_changes: AllowedChangeSet | None,
    ) -> str | None:
        """
        Fully validate the action envelope BEFORE writing anything.

        Returns None when valid, otherwise an error string.
        """

        files = proposal.get("files")
        tools = proposal.get("tools")
        legacy_commands = proposal.get("commands")

        if files is None and tools is None:
            return "action envelope has no files or tools"

        if legacy_commands not in (None, []):
            return "arbitrary commands are forbidden; use typed tools"

        if files is not None:
            if not isinstance(files, list):
                return "'files' must be a list"

            if len(files) > 25:
                return "too many files in one envelope"

            files_tools = getattr(
                self.workspace,
                "files",
                None,
            )

            seen_paths: set[str] = set()

            for entry in files:
                if not isinstance(entry, dict):
                    return "each file entry must be an object"

                path = entry.get("path")

                if (
                    not isinstance(path, str)
                    or not path.strip()
                ):
                    return "file entry requires a path"

                try:
                    project_path = ProjectPath.parse(path)
                except ProjectPathError as error:
                    return str(error)

                if project_path.comparison_key in seen_paths:
                    return f"duplicate file entry: {project_path.value}"
                seen_paths.add(project_path.comparison_key)

                operation = entry.get("operation", "write")
                if operation not in {"write", "delete"}:
                    return "file operation must be 'write' or 'delete'"

                try:
                    if allowed_changes is not None:
                        if operation == "delete":
                            allowed_changes.assert_can_delete(project_path.value)
                        else:
                            allowed_changes.assert_can_write(project_path.value)
                except ChangeScopeError as error:
                    return str(error)

                if operation == "delete":
                    if "content" in entry:
                        return "delete entry must not include content"
                    if files_tools is None or not hasattr(
                        files_tools, "validate_delete"
                    ):
                        return "workspace cannot validate safe deletion"
                    try:
                        files_tools.validate_delete(project_path.value)
                    except PolicyViolation as error:
                        return str(error)
                else:
                    content = entry.get("content", "")
                    if not isinstance(content, str):
                        return "file content must be a string"

                    if (
                        len(content.encode("utf-8"))
                        > self.limits.max_file_bytes
                    ):
                        return (
                            "file exceeds max_file_bytes: "
                            f"{project_path.value}"
                        )

                if files_tools is not None:
                    try:
                        files_tools.policy.resolve(project_path.value)

                    except PolicyViolation as error:
                        return str(error)

                # OBSERVE BEFORE EDIT: an existing file must have been
                # read during this attempt before it is modified.
                if project_path.comparison_key not in observed:
                    try:
                        exists = self.workspace.exists(project_path.value)

                    except Exception as error:
                        return str(error)

                    if exists:
                        return (
                            "observe-before-edit violated: "
                            f"must read {project_path.value} before editing"
                        )

        try:
            decode_tool_calls(
                tools,
                allowed_changes=allowed_changes,
            )
        except ToolProtocolError as error:
            return str(error)

        return None

    def _apply_proposal(
        self,
        proposal: dict[str, object],
        *,
        read_files: list[str] | None = None,
        cancellation_token=None,
    ) -> ExecutionResult:
        files = proposal.get("files")
        tools = proposal.get("tools")

        if files is None and tools is None:
            return ExecutionResult(
                ok=False,
                summary=(
                    "action envelope has no "
                    "files or tools"
                ),
                failure_reason=(
                    "empty action envelope"
                ),
                evidence=["empty_action_envelope"],
            )

        artifacts: list[str] = []
        evidence: list[str] = []
        command_results: list[CommandExecution] = []

        # --------------------------------------
        # FILE CHANGES
        # --------------------------------------

        if files is not None:
            if not isinstance(files, list):
                return ExecutionResult(
                    ok=False,
                    summary="'files' must be a list",
                    failure_reason="invalid files",
                    evidence=["invalid_files"],
                )

            backups: dict[str, tuple[str, bool, str | None]] = {}
            applied: list[str] = []

            for entry in files:
                if not isinstance(entry, dict):
                    return ExecutionResult(
                        ok=False,
                        summary=(
                            "each file entry must be "
                            "an object"
                        ),
                        failure_reason=(
                            "invalid file entry"
                        ),
                        evidence=[
                            "invalid_file_entry"
                        ],
                    )

                path = entry.get("path")
                operation = entry.get("operation", "write")
                content = entry.get("content", "")

                if (
                    not isinstance(path, str)
                    or not path.strip()
                ):
                    return ExecutionResult(
                        ok=False,
                        summary=(
                            "file entry requires path"
                        ),
                        failure_reason="missing path",
                        evidence=[
                            "missing_file_path"
                        ],
                    )

                if operation == "write" and not isinstance(content, str):
                    return ExecutionResult(
                        ok=False,
                        summary=(
                            "file content must be "
                            "a string"
                        ),
                        failure_reason=(
                            "invalid file content"
                        ),
                        evidence=[
                            "invalid_file_content"
                        ],
                    )

                raw_path = ProjectPath.parse(path).value
                key = ProjectPath.parse(raw_path).comparison_key

                if key not in backups:
                    existed = self.workspace.exists(raw_path)
                    old_content = (
                        self.workspace.read(raw_path) if existed else None
                    )
                    backups[key] = (raw_path, existed, old_content)

                try:
                    if operation == "delete":
                        self.workspace.delete(raw_path)
                        relative = raw_path
                        evidence.append(f"deleted {relative}")
                    else:
                        written = self.workspace.write(raw_path, content)
                        relative = self.workspace.relative(written)
                        evidence.append(f"wrote {relative}")
                    applied.append(key)

                except Exception as error:
                    rollback_errors: list[str] = []
                    for applied_key in reversed(applied):
                        backup_path, existed, old_content = backups[applied_key]
                        try:
                            if existed:
                                self.workspace.write(backup_path, old_content or "")
                            elif self.workspace.exists(backup_path):
                                self.workspace.delete(backup_path)
                        except Exception as rollback_error:
                            rollback_errors.append(str(rollback_error))
                    return ExecutionResult(
                        ok=False,
                        summary="file change transaction failed",
                        failure_reason=(
                            str(error)
                            + (
                                "; rollback errors: " + "; ".join(rollback_errors)
                                if rollback_errors
                                else ""
                            )
                        ),
                        evidence=[f"file_transaction_failed:{raw_path}"],
                        read_files=list(read_files or []),
                    )

                artifacts.append(relative)

            written_paths = [
                entry["path"]
                for entry in files
                if isinstance(entry, dict)
                and entry.get("operation", "write") == "write"
            ]
            deleted_paths = [
                entry["path"]
                for entry in files
                if isinstance(entry, dict)
                and entry.get("operation") == "delete"
            ]
            if written_paths:
                self._notify(
                    "edit_applied",
                    {"paths": written_paths, "duration_ms": 0},
                )
            if deleted_paths:
                self._notify(
                    "delete_applied",
                    {"paths": deleted_paths, "duration_ms": 0},
                )


        # --------------------------------------
        # TYPED TOOLS — argv only, never model-provided shell text
        # --------------------------------------

        if tools is not None:
            try:
                tool_calls = decode_tool_calls(tools)
            except ToolProtocolError as error:
                return ExecutionResult(
                    ok=False,
                    summary="invalid typed tool call",
                    failure_reason=str(error),
                    evidence=["invalid_tool_call"],
                )

            if self.run_commands:
                for tool_call in tool_calls:
                    runner = getattr(self.command_runner, "run_argv", None)
                    if runner is None:
                        return ExecutionResult(
                            ok=False,
                            summary="typed tools are disabled",
                            failure_reason=(
                                "no argv-only project runner; refusing to execute"
                            ),
                            evidence=[f"blocked_tool:{tool_call.name}"],
                        )

                    try:
                        command_started = time.time()
                        if cancellation_token is None:
                            result = runner(list(tool_call.argv))
                        else:
                            result = runner(
                                list(tool_call.argv),
                                cancellation_token=cancellation_token,
                            )

                        self._notify(
                            "docker_command",
                            {
                                "tool": tool_call.name,
                                "command": tool_call.label[:200],
                                "returncode": result.returncode,
                                "duration_ms": int(
                                    (time.time() - command_started) * 1000
                                ),
                            },
                        )

                    except PolicyViolation as error:
                        return ExecutionResult(
                            ok=False,
                            summary="typed tool rejected by policy",
                            failure_reason=str(error),
                            evidence=[f"blocked_tool:{tool_call.name}"],
                        )

                    execution = CommandExecution(
                        command=tool_call.label,
                        returncode=result.returncode,
                        stdout=result.stdout,
                        stderr=result.stderr,
                    )
                    command_results.append(execution)
                    evidence.append(execution.as_evidence())

        # --------------------------------------
        # REAL-EXECUTION GUARD
        # --------------------------------------

        if not artifacts and not command_results:
            return ExecutionResult(
                ok=False,
                summary="nothing was executed",
                failure_reason=(
                    "no file changes and no tool calls"
                ),
                evidence=(
                    evidence
                    or ["nothing_executed"]
                ),
            )

        tools_ok = all(
            result.ok
            for result in command_results
        )

        failure_reason = None

        if not tools_ok:
            failure_reason = (
                "one or more tool calls failed"
            )

        summary = (
            f"applied {len(artifacts)} file(s), "
            f"ran {len(command_results)} "
            f"tool call(s)"
        )

        return ExecutionResult(
            ok=tools_ok,
            summary=summary,
            artifacts=artifacts,
            evidence=evidence,
            commands=command_results,
            failure_reason=failure_reason,
            read_files=list(read_files or []),
        )




