from __future__ import annotations

import re

from app.memory.manager import MemoryCandidate


def _normalize(
    text: str,
) -> str:
    text = text.casefold()

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def find_memories_to_supersede(
    candidate: MemoryCandidate,
    memories: list[dict[str, object]],
) -> list[int]:
    """
    Консервативный Conflict Resolver.

    Старая память заменяется только тогда,
    когда MemoryCandidate явно содержит replaces.

    USER_RULE здесь никогда автоматически
    не отменяется.
    """

    if not candidate.replaces:
        return []

    replacement_target = _normalize(
        candidate.replaces
    )

    if not replacement_target:
        return []

    result: list[int] = []

    for memory in memories:
        if (
            memory.get("status")
            != "ACTIVE"
        ):
            continue

        memory_type = str(
            memory.get(
                "type",
                "",
            )
        )

        # Пользовательские правила требуют
        # отдельного механизма отмены.
        if memory_type not in {
            "FACT",
            "DECISION",
        }:
            continue

        content = str(
            memory.get(
                "content",
                "",
            )
        )

        why = str(
            memory.get(
                "why",
                "",
            )
            or ""
        )

        searchable_text = _normalize(
            f"{content} {why}"
        )

        if replacement_target in searchable_text:
            memory_id = memory.get(
                "id"
            )

            if memory_id is not None:
                result.append(
                    int(memory_id)
                )

    return result