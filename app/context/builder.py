from __future__ import annotations


def build_user_memory_context(
    memories: list[dict[str, object]],
) -> str:
    if not memories:
        return (
            "USER MEMORY:\n"
            "Сохранённых пользовательских правил нет."
        )

    lines = [
        "USER MEMORY:",
        "",
        "Ниже находятся сохранённые сведения "
        "и правила пользователя.",
        "",
    ]

    for memory in memories:
        memory_type = memory.get(
            "type",
            "UNKNOWN",
        )

        content = memory.get(
            "content",
            "",
        )

        lines.append(
            f"- [{memory_type}] {content}"
        )

    return "\n".join(lines)


def build_current_user_message(
    *,
    user_input: str,
    memories: list[dict[str, object]],
) -> str:
    memory_context = build_user_memory_context(
        memories
    )

    return (
        f"{memory_context}\n\n"
        "ТЕКУЩИЙ ЗАПРОС ПОЛЬЗОВАТЕЛЯ:\n"
        f"{user_input}"
    )