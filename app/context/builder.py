from __future__ import annotations


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
    ) -> None:
        self.memory = memory
        self.project = project
        self.system_prompt = system_prompt

    def _get_memories(
        self,
    ) -> list[dict[str, object]]:
        if self.memory is None:
            return []

        return self.memory.get_active_memories()

    def build_chat_messages(
        self,
        *,
        user_input: str,
        conversation_history: list[
            dict[str, str]
        ] | None = None,
    ) -> list[dict[str, str]]:
        memories = self._get_memories()

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
    ) -> list[dict[str, str]]:
        """
        Контекст для CodingAgent.

        Собирает память + анализ проекта + описание Task/Step
        + обратную связь от предыдущей неудачной попытки.
        """

        memory_context = build_user_memory_context(
            self._get_memories()
        )

        criteria = getattr(
            task,
            "success_criteria",
            [],
        )

        criteria_text = "\n".join(
            f"- {criterion}"
            for criterion in criteria
        ) or "- (не заданы)"

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
            f"{feedback_text}\n"
            f"{project_context}\n\n"
            "Return exactly one JSON object describing the "
            "change. No markdown. No explanations.\n"
            "{\n"
            '  "files": [\n'
            '    {"path": "relative/path", "content": "..."}\n'
            "  ],\n"
            '  "commands": ["command"]\n'
            "}"
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
