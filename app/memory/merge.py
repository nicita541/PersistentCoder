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


def find_decisions_to_merge(
    candidate: MemoryCandidate,
    memories: list[dict[str, object]],
) -> list[int]:
    """
    Ищет старые ACTIVE DECISION с тем же WHAT.

    Например:

    старая:
        Использовать SQLite.
        WHY: старая причина

    новая:
        Использовать SQLite.
        WHY: новая причина

    Тогда старая запись должна стать SUPERSEDED,
    а новая — ACTIVE.

    USER_RULE и FACT здесь не затрагиваются.
    """

    if candidate.memory_type != "DECISION":
        return []

    candidate_content = _normalize(
        candidate.content
    )

    if not candidate_content:
        return []

    result: list[int] = []

    for memory in memories:
        if (
            memory.get("status")
            != "ACTIVE"
        ):
            continue

        if (
            memory.get("type")
            != "DECISION"
        ):
            continue

        content = _normalize(
            str(
                memory.get(
                    "content",
                    "",
                )
            )
        )

        if content != candidate_content:
            continue

        memory_id = memory.get(
            "id"
        )

        if memory_id is None:
            continue

        result.append(
            int(memory_id)
        )

    return result