from __future__ import annotations

from app.agent.state import (
    CommandExecution,
    ExecutionResult,
    extract_json_object,
)


class CodeExecutorError(RuntimeError):
    pass


class CodeExecutor:
    """
    Реальный исполнитель CodingAgent.

    Поток:

        Task/Step
          -> ContextBuilder
          -> LLM (один общий client)
          -> FileTools / TerminalTools
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
    ) -> None:
        self.workspace = workspace
        self.llm = llm
        self.context = context
        self.system_prompt = system_prompt
        self.max_new_tokens = max_new_tokens
        self.run_commands = run_commands

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

            if (
                self.workspace is not None
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
        messages = self._build_messages(
            task,
            step,
            feedback,
        )

        try:
            raw_answer = self.llm.chat(
                messages,
                max_new_tokens=self.max_new_tokens,
            )

        except Exception as exc:
            return ExecutionResult(
                ok=False,
                summary="LLM call failed",
                failure_reason=str(exc),
                evidence=[
                    f"llm_error:{type(exc).__name__}"
                ],
            )

        answer = self._apply_output_policy(
            raw_answer,
            self.system_prompt,
        )

        try:
            proposal = extract_json_object(answer)

        except ValueError:
            return ExecutionResult(
                ok=False,
                summary=(
                    "model did not return "
                    "a valid action envelope"
                ),
                failure_reason=(
                    "invalid model response"
                ),
                evidence=[
                    "model_response:"
                    + answer[:200]
                ],
            )

        return self._apply_proposal(proposal)

    def _apply_proposal(
        self,
        proposal: dict[str, object],
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

                written = self.workspace.write(
                    path.strip(),
                    content,
                )

                relative = self.workspace.relative(
                    written
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

                    result = (
                        self.workspace.terminal.run(
                            command
                        )
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
        )



