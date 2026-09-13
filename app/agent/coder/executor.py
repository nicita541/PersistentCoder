from __future__ import annotations

import time

from app.agent.state import (
    CommandExecution,
    ExecutionResult,
    extract_json_object,
)
from app.sandbox.limits import (
    DEFAULT_LIMITS,
    LimitExceeded,
)
from app.sandbox.policy import (
    CommandPolicy,
    PolicyViolation,
    is_absolute_path,
)
from app.context.builder import ACTION_PROTOCOL


class CodeExecutorError(RuntimeError):
    pass


# Keys that unambiguously carry a file's full content when the model
# also names a relative path. Accepting them is normalization, not
# guessing: validation (path policy, limits, observe-before-edit)
# still runs before anything is written.
CONTENT_KEYS = (
    "content",
    "code",
    "file_content",
    "source",
    "body",
    "text",
    "new_content",
)


def strip_markdown_fence(
    answer: str,
) -> str:
    """
    Small models very often wrap an otherwise valid JSON envelope in
    a markdown code fence. Stripping the fence is deterministic
    normalization, not guessing: the object itself is still fully
    validated before anything is written.
    """

    text = (answer or "").strip()

    if not text.startswith("```"):
        return text

    first_newline = text.find("\n")

    if first_newline == -1:
        return text

    body = text[first_newline + 1:]

    stripped = body.rstrip()

    if stripped.endswith("```"):
        stripped = stripped[:-3]

    return stripped.strip()


def _content_key(
    proposal: dict[str, object],
) -> str | None:
    for key in CONTENT_KEYS:
        if isinstance(proposal.get(key), str):
            return key

    return None


def _normalize_files(
    files: object,
) -> object:
    """
    Accept the mapping form {"relative/path": "content"} as well.
    """

    if isinstance(files, dict):
        return [
            {"path": path, "content": content}
            for path, content in files.items()
        ]

    return files


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
        command_runner=None,
        limits=DEFAULT_LIMITS,
        known_files=None,
        on_event=None,
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

        # Paths the agent itself created during this session. These
        # are already "known" and do not require an explicit read
        # before a later attempt may rewrite them.
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
                    "Return JSON with files/commands."
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
    ) -> ExecutionResult:
        observed: dict[str, str] = {}
        transcript: list[str] = []
        envelope_errors = 0

        for iteration in range(
            1,
            self.limits.max_tool_iterations + 1,
        ):
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
                proposal = extract_json_object(
                    strip_markdown_fence(answer)
                )

            except ValueError:
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

            action = self._action_of(proposal)

            if action in ("list", "read", "search"):
                _ok, text = self._observe(
                    proposal,
                    observed,
                )

                transcript.append(text)

                continue

            if action == "edit":
                proposal = self._normalize_proposal(
                    proposal
                )

                if (
                    "files" not in proposal
                    and "commands" not in proposal
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
                            "files or commands",
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
                    ["unknown_action"],
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

        return self._fail(
            "tool loop budget exceeded",
            "max tool iterations exceeded",
            ["tool_loop_budget"],
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


    @staticmethod
    def _action_of(
        proposal: dict[str, object],
    ) -> str | None:
        """
        Deterministic normalization of the model's reply into one of
        the protocol actions. Small models emit synonyms and
        single-file shapes; accepting them is safe because the
        envelope is still fully validated before any write.
        """

        raw = proposal.get("action")

        if isinstance(raw, str) and raw.strip():
            action = raw.strip().casefold()

            if action in {"list", "ls", "list_files", "tree"}:
                return "list"

            if action in {
                "read",
                "read_file",
                "open",
                "cat",
            }:
                return "read"

            if action in {
                "search",
                "grep",
                "find",
                "find_symbol",
            }:
                return "search"

            if action in {
                "edit",
                "write",
                "create",
                "update",
                "apply",
                "patch",
                "modify",
                "save",
            }:
                return "edit"

            return action

        if (
            "files" in proposal
            or "commands" in proposal
        ):
            return "edit"

        # Single-file shapes:
        #   {"path": "...", "content": "..."}
        #   {"path": "...", "code": "..."}
        #   {"file": {"path": "...", "content": "..."}}
        if (
            isinstance(proposal.get("path"), str)
            and _content_key(proposal)
        ):
            return "edit"

        if isinstance(proposal.get("file"), dict):
            return "edit"

        # A dict mapping relative paths to their full content.
        if isinstance(proposal.get("files"), dict):
            return "edit"

        # Search shapes.
        for key in ("query", "needle", "text"):
            if isinstance(proposal.get(key), str):
                return "search"

        return None

    @staticmethod
    def _normalize_proposal(
        proposal: dict[str, object],
    ) -> dict[str, object]:
        """
        Turn accepted synonyms / single-file shapes into the canonical
        envelope. Pure normalization: validation still happens
        afterwards.
        """

        if (
            "files" not in proposal
            and "commands" not in proposal
        ):
            entry = proposal.get("file")

            if isinstance(entry, str):
                # {"action": "create", "file": "a.py",
                #  "content": "..."}
                key = _content_key(proposal)

                if key:
                    entry = {
                        "path": entry,
                        "content": proposal[key],
                    }

                else:
                    entry = None

            if not isinstance(entry, dict):
                key = _content_key(proposal)

                if (
                    isinstance(
                        proposal.get("path"),
                        str,
                    )
                    and key
                ):
                    entry = {
                        "path": proposal["path"],
                        "content": proposal[key],
                    }

                else:
                    entry = None

            if entry is not None:
                normalized = dict(proposal)

                normalized["files"] = [entry]

                return normalized

        normalized = dict(proposal)

        if normalized.get("files") is not None:
            normalized["files"] = _normalize_files(
                normalized["files"]
            )

        return normalized

    def _normalize(
        self,
        path: str,
    ) -> str:
        return (
            path.replace("\\", "/")
            .strip()
            .lstrip("./")
        )

    def _observe(
        self,
        proposal: dict[str, object],
        observed: dict[str, str],
    ) -> tuple[bool, str]:
        """
        Read-only sandbox observation. Never writes anything.
        """

        action = self._action_of(proposal)

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

            observed[self._normalize(path)] = content

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
    ) -> str | None:
        """
        Fully validate the action envelope BEFORE writing anything.

        Returns None when valid, otherwise an error string.
        """

        files = proposal.get("files")
        commands = proposal.get("commands")

        if files is None and commands is None:
            return "action envelope has no files or commands"

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

            for entry in files:
                if not isinstance(entry, dict):
                    return "each file entry must be an object"

                path = entry.get("path")
                content = entry.get("content", "")

                if (
                    not isinstance(path, str)
                    or not path.strip()
                ):
                    return "file entry requires a path"

                if is_absolute_path(path):
                    return (
                        "absolute paths are forbidden: "
                        f"{path}"
                    )

                if not isinstance(content, str):
                    return "file content must be a string"

                if (
                    len(content.encode("utf-8"))
                    > self.limits.max_file_bytes
                ):
                    return (
                        "file exceeds max_file_bytes: "
                        f"{path}"
                    )

                if files_tools is not None:
                    try:
                        files_tools.policy.resolve(path)

                    except PolicyViolation as error:
                        return str(error)

                # OBSERVE BEFORE EDIT: an existing file must have been
                # read during this attempt before it is modified.
                if self._normalize(path) not in observed:
                    try:
                        exists = self.workspace.exists(path)

                    except Exception as error:
                        return str(error)

                    if (
                        exists
                        and self._normalize(path)
                        not in self.known_files
                    ):
                        return (
                            "observe-before-edit violated: "
                            f"must read {path} before editing"
                        )

        if commands is not None:
            if not isinstance(commands, list):
                return "'commands' must be a list"

            policy = getattr(
                self.command_runner,
                "policy",
                None,
            ) or CommandPolicy()

            for command in commands:
                if (
                    not isinstance(command, str)
                    or not command.strip()
                ):
                    return (
                        "each command must be a "
                        "non-empty string"
                    )

                try:
                    policy.validate(command)

                except PolicyViolation as error:
                    return str(error)

        return None

    def _apply_proposal(
        self,
        proposal: dict[str, object],
        *,
        read_files: list[str] | None = None,
    ) -> ExecutionResult:
        files = proposal.get("files")
        commands = proposal.get("commands")

        if files is None and commands is None:
            return ExecutionResult(
                ok=False,
                summary=(
                    "action envelope has no "
                    "files or commands"
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

                if not isinstance(content, str):
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

                raw_path = path.strip()

                if is_absolute_path(raw_path):
                    return ExecutionResult(
                        ok=False,
                        summary=(
                            "absolute workspace paths "
                            "are forbidden"
                        ),
                        failure_reason=(
                            "absolute path in action "
                            "envelope"
                        ),
                        evidence=[
                            f"blocked_path:{raw_path}"
                        ],
                    )

                try:
                    written = self.workspace.write(
                        raw_path,
                        content,
                    )

                except PolicyViolation as error:
                    return ExecutionResult(
                        ok=False,
                        summary=(
                            "path rejected by sandbox "
                            "policy"
                        ),
                        failure_reason=str(error),
                        evidence=[
                            f"blocked_path:{raw_path}"
                        ],
                    )

                relative = self.workspace.relative(
                    written
                )

                self.known_files.add(
                    self._normalize(raw_path)
                )

                artifacts.append(relative)
                evidence.append(
                    f"wrote {relative}"
                )


        # --------------------------------------
        # COMMANDS
        # --------------------------------------

        if commands is not None:
            if not isinstance(commands, list):
                return ExecutionResult(
                    ok=False,
                    summary=(
                        "'commands' must be a list"
                    ),
                    failure_reason="invalid commands",
                    evidence=["invalid_commands"],
                )

            if self.run_commands:
                for command in commands:
                    if (
                        not isinstance(command, str)
                        or not command.strip()
                    ):
                        return ExecutionResult(
                            ok=False,
                            summary=(
                                "each command must be "
                                "a non-empty string"
                            ),
                            failure_reason=(
                                "invalid command"
                            ),
                            evidence=[
                                "invalid_command"
                            ],
                        )

                    if self.command_runner is None:
                        return ExecutionResult(
                            ok=False,
                            summary=(
                                "commands are disabled"
                            ),
                            failure_reason=(
                                "no sandbox command "
                                "runner; refusing to "
                                "execute on host"
                            ),
                            evidence=[
                                f"blocked_command:{command}"
                            ],
                        )

                    try:
                        command_started = time.time()

                        result = (
                            self.command_runner.run(
                                command
                            )
                        )

                        self._notify(
                            "docker_command",
                            {
                                "command": command[:200],
                                "returncode": (
                                    result.returncode
                                ),
                                "duration_ms": int(
                                    (
                                        time.time()
                                        - command_started
                                    )
                                    * 1000
                                ),
                            },
                        )

                    except PolicyViolation as error:
                        return ExecutionResult(
                            ok=False,
                            summary=(
                                "command rejected by "
                                "policy"
                            ),
                            failure_reason=str(error),
                            evidence=[
                                f"blocked_command:{command}"
                            ],
                        )

                    execution = CommandExecution(
                        command=command,
                        returncode=result.returncode,
                        stdout=result.stdout,
                        stderr=result.stderr,
                    )

                    command_results.append(
                        execution
                    )

                    evidence.append(
                        execution.as_evidence()
                    )

        # --------------------------------------
        # REAL-EXECUTION GUARD
        # --------------------------------------

        if not artifacts and not command_results:
            return ExecutionResult(
                ok=False,
                summary="nothing was executed",
                failure_reason=(
                    "no file changes and no commands"
                ),
                evidence=(
                    evidence
                    or ["nothing_executed"]
                ),
            )

        commands_ok = all(
            result.ok
            for result in command_results
        )

        failure_reason = None

        if not commands_ok:
            failure_reason = (
                "one or more commands failed"
            )

        summary = (
            f"applied {len(artifacts)} file(s), "
            f"ran {len(command_results)} "
            f"command(s)"
        )

        return ExecutionResult(
            ok=commands_ok,
            summary=summary,
            artifacts=artifacts,
            evidence=evidence,
            commands=command_results,
            failure_reason=failure_reason,
            read_files=list(read_files or []),
        )




