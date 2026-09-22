from __future__ import annotations


# ==========================================
# CODING AGENT ACTION PROTOCOL
# ==========================================

# Single source of truth for the CodingAgent tool protocol. It is
# used both in the task prompt and as corrective feedback when a
# small model returns something that is not a valid envelope.
ACTION_PROTOCOL = (
    "ACTION PROTOCOL: reply with EXACTLY ONE JSON object, "
    "no markdown, no explanations, no code fences.\n"
    "Allowed replies:\n"
    '1) list sandbox files: {"action": "list", "pattern": "**/*.py"}\n'
    '2) read one file:      {"action": "read", "path": "relative/file.py"}\n'
    '3) search the repo:    {"action": "search", "query": "def add"}\n'
    "4) create/update files and run typed tools: {\"action\": \"edit\",\n"
    '     "files": [{"path": "relative/file.py", '
    '"content": "COMPLETE new file content"}],\n'
    '     "tools": [{"tool": "py_compile", "paths": ["relative/file.py"]}]}\n'
    "Typed tools (there are NO arbitrary shell commands):\n"
    '- {"tool": "py_compile", "paths": ["src/module.py"]}\n'
    '- {"tool": "pytest", "targets": ["tests/test_module.py"], "options": ["-q"]}\n'
    '- {"tool": "python_file", "path": "scripts/check.py", "args": ["--quick"]}\n'
    '- {"tool": "python_module", "module": "package", "args": ["--help"]}\n'
    "Rules:\n"
    "- paths are always RELATIVE, never absolute;\n"
    "- an existing file must be READ in this task before it is "
    "edited;\n"
    "- if READ reports that an allowed target is missing, do not read it "
    "again; CREATE it directly with complete content;\n"
    '- "content" is the COMPLETE new file content, not a diff;\n'
    "- keep every file SHORT and focused on the current step: only "
    "the requested functions. A reply that is cut off before the "
    "closing brace is an invalid reply;\n"
    "- one file per reply is enough; do the rest in later steps;\n"
    "- typed tools run only through the bounded project runner;\n"
    "- run only the tests related to your change (name the test "
    "files explicitly); never run an unrelated whole-repository "
    "test suite."
)


def build_user_memory_context(
    memories: list[dict[str, object]],
) -> str:
    if not memories:
        return (
            "USER MEMORY:\n"
            "Постоянная память пуста."
        )

    rules: list[str] = []
    facts: list[str] = []
    decisions: list[str] = []
    experiences: list[str] = []

    for memory in memories:
        memory_type = str(
            memory.get(
                "type",
                "",
            )
        )

        content = str(
            memory.get(
                "content",
                "",
            )
        )

        why = memory.get(
            "why"
        )

        if memory_type == "USER_RULE":
            rules.append(
                f"- {content}"
            )

        elif memory_type == "FACT":
            facts.append(
                f"- {content}"
            )

        elif memory_type == "DECISION":
            decision_text = (
                f"- WHAT: {content}"
            )

            if why:
                decision_text += (
                    f"\n  WHY: {why}"
                )

            decisions.append(
                decision_text
            )

        elif memory_type == "EXPERIENCE":
            experience_text = (
                f"- {content}"
            )

            if why:
                experience_text += (
                    f"\n  WHY: {why}"
                )

            experiences.append(
                experience_text
            )

    sections = [
        "USER MEMORY",
        "",
    ]

    sections.append(
        "ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:"
    )

    sections.extend(
        rules
        if rules
        else ["- Нет."]
    )

    sections.append("")
    sections.append(
        "ФАКТЫ О ПРОЕКТЕ:"
    )

    sections.extend(
        facts
        if facts
        else ["- Нет."]
    )

    sections.append("")
    sections.append(
        "ПРИНЯТЫЕ РЕШЕНИЯ:"
    )

    sections.extend(
        decisions
        if decisions
        else ["- Нет."]
    )

    if experiences:
        sections.append("")
        sections.append(
            "ПОДТВЕРЖДЁННЫЙ ОПЫТ (verified DONE):"
        )

        sections.extend(experiences)

    return "\n".join(
        sections
    )


def build_current_user_message(
    *,
    user_input: str,
    memories: list[dict[str, object]],
) -> str:
    memory_context = (
        build_user_memory_context(
            memories
        )
    )

    return (
        f"{memory_context}\n\n"
        "ТЕКУЩИЙ ЗАПРОС ПОЛЬЗОВАТЕЛЯ:\n"
        f"{user_input}"
    )


class ContextBuilder:
    """
    Единый Context Layer.

    Планировщик, кодер и верификатор получают контекст
    только отсюда. Никаких копий внутри app/agent.
    """

    def __init__(
        self,
        *,
        memory=None,
        project=None,
        system_prompt: str | None = None,
        retriever=None,
        selector=None,
    ) -> None:
        self.memory = memory
        self.project = project
        self.system_prompt = system_prompt

        # Optional relevance layers (app/memory/retrieval.py and
        # app/context/selector.py). When they are injected, only
        # relevant memories / files reach the model.
        self.retriever = retriever
        self.selector = selector

    @staticmethod
    def _join_text(
        *parts: object,
    ) -> str:
        return "\n".join(
            str(part)
            for part in parts
            if part
        )

    def _default_memory_query(
        self,
        *,
        task=None,
        step=None,
        feedback: str | None = None,
        user_input: str = "",
    ):
        from app.memory.retrieval import (
            MemoryQuery,
        )

        criteria = (
            getattr(
                task,
                "success_criteria",
                None,
            )
            or []
        )

        return MemoryQuery(
            current_task=self._join_text(
                getattr(task, "title", "") if task else "",
                (
                    getattr(task, "description", "")
                    if task
                    else ""
                ),
                *criteria,
            ),
            current_step=self._join_text(
                getattr(step, "title", "") if step else "",
                (
                    getattr(step, "description", "")
                    if step
                    else ""
                ),
            ),
            failure_context=feedback or "",
            extra_terms=(
                (user_input,)
                if user_input
                else ()
            ),
        )

    def _get_memories(
        self,
        query=None,
    ) -> list[dict[str, object]]:
        if self.memory is None:
            return []

        memories = self.memory.get_active_memories()

        if self.retriever is None:
            return memories

        return self.retriever.select(
            memories,
            query,
        ).memories

    def build_chat_messages(
        self,
        *,
        user_input: str,
        conversation_history: list[
            dict[str, str]
        ] | None = None,
        memory_query=None,
    ) -> list[dict[str, str]]:
        if memory_query is None and self.retriever is not None:
            memory_query = self._default_memory_query(
                user_input=user_input,
            )

        memories = self._get_memories(
            memory_query
        )

        messages: list[dict[str, str]] = []

        if self.system_prompt:
            messages.append(
                {
                    "role": "system",
                    "content": self.system_prompt,
                }
            )

        messages.extend(
            conversation_history or []
        )

        messages.append(
            {
                "role": "user",
                "content": build_current_user_message(
                    user_input=user_input,
                    memories=memories,
                ),
            }
        )

        return messages

    def build_task_messages(
        self,
        *,
        task,
        step=None,
        project_context: str = "",
        feedback: str | None = None,
        memory_query=None,
    ) -> list[dict[str, str]]:
        """
        Контекст для CodingAgent.

        Собирает релевантную память + релевантную часть проекта
        (RepoContextSelector) + описание Task/Step + обратную связь
        от предыдущей неудачной попытки.
        """

        if memory_query is None and self.retriever is not None:
            memory_query = self._default_memory_query(
                task=task,
                step=step,
                feedback=feedback,
            )

        memory_context = build_user_memory_context(
            self._get_memories(memory_query)
        )

        if not project_context and self.selector is not None:
            project_context = self.selector.select(
                task=task,
                step=step,
                feedback=feedback,
            ).text

        criteria = getattr(
            task,
            "success_criteria",
            [],
        )

        criteria_text = "\n".join(
            f"- {criterion}"
            for criterion in criteria
        ) or "- (не заданы)"

        active_change_paths = getattr(
            step if step is not None else task,
            "change_paths",
            [],
        )
        change_paths_text = "\n".join(
            f"- {path}" for path in active_change_paths
        ) or "- (none; persistent file changes are forbidden)"

        step_text = ""

        if step is not None:
            step_text = (
                "\nSTEP:\n"
                f"{getattr(step, 'title', '')}\n"
                f"{getattr(step, 'description', '')}\n"
            )

        feedback_text = ""

        if feedback:
            feedback_text = (
                "\nPREVIOUS FAILURE:\n"
                f"{feedback}\n"
            )

        content = (
            f"{memory_context}\n\n"
            "TASK:\n"
            f"key: {getattr(task, 'key', '')}\n"
            f"title: {getattr(task, 'title', '')}\n"
            f"description: "
            f"{getattr(task, 'description', '')}\n"
            f"{step_text}"
            "\nSUCCESS CRITERIA:\n"
            f"{criteria_text}\n"
            "\nEXACT ALLOWED CHANGE PATHS:\n"
            f"{change_paths_text}\n"
            f"{feedback_text}\n"
            f"{project_context}\n\n"
            f"{ACTION_PROTOCOL}"
        )

        system_prompt = (
            self.system_prompt
            or (
                "You are the Coding Agent. "
                "Return JSON only. "
                "Only describe real changes."
            )
        )

        return [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": content,
            },
        ]
