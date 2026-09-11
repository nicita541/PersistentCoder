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