from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryCandidate:
    memory_type: str
    content: str
    why: str | None
    importance: int
    confidence: float

    # Если новая память явно заменяет
    # старую технологию/решение.
    replaces: str | None = None


def _normalize(
    text: str,
) -> str:
    return re.sub(
        r"\s+",
        " ",
        text.casefold().strip(),
    )


def _sentence(
    text: str,
) -> str:
    text = text.strip()

    if not text:
        return text

    text = (
        text[0].upper()
        + text[1:]
    )

    if text[-1] not in ".!?":
        text += "."

    return text


def _looks_like_question(
    text: str,
) -> bool:
    normalized = _normalize(
        text
    )

    if text.strip().endswith("?"):
        return True

    question_starts = (
        "можно ли ",
        "нужно ли ",
        "стоит ли ",
        "почему ",
        "как ",
        "какие ",
        "какое ",
        "какой ",
        "что ",
        "где ",
    )

    return normalized.startswith(
        question_starts
    )


def _looks_like_task(
    text: str,
) -> bool:
    normalized = _normalize(
        text
    )

    task_starts = (
        "напиши ",
        "создай ",
        "сделай ",
        "исправь ",
        "добавь ",
        "удали ",
        "объясни ",
        "проверь ",
        "найди ",
        "покажи ",
        "реализуй ",
        "проанализируй ",
    )

    return normalized.startswith(
        task_starts
    )


def _extract_replacement_decision(
    text: str,
) -> MemoryCandidate | None:
    """
    Явная замена:

    Теперь вместо PostgreSQL используем SQLite.

    Теперь вместо PostgreSQL используем SQLite,
    потому что отдельный сервер больше не нужен.
    """

    pattern = re.compile(
        r"^\s*"
        r"теперь\s+вместо\s+"
        r"(.+?)"
        r"\s+используем\s+"
        r"(.+?)"
        r"(?:"
        r"\s*,\s*"
        r"потому\s+что\s+"
        r"(.+?)"
        r")?"
        r"[.!]?\s*$",
        re.IGNORECASE,
    )

    match = pattern.match(
        text
    )

    if match is None:
        return None

    old_value = (
        match.group(1)
        .strip()
        .rstrip(".,!?")
    )

    new_value = (
        match.group(2)
        .strip()
        .rstrip(".,!?")
    )

    reason_group = (
        match.group(3)
    )

    if (
        not old_value
        or not new_value
    ):
        return None

    reason: str | None = None

    if reason_group:
        reason = _sentence(
            reason_group
            .strip()
            .rstrip(".,!?")
        )

    return MemoryCandidate(
        memory_type="DECISION",
        content=_sentence(
            f"использовать {new_value}"
        ),
        why=reason,
        importance=90,
        confidence=1.0,
        replaces=old_value,
    )


def _extract_decision(
    text: str,
) -> MemoryCandidate | None:
    """
    Обычное явное решение с причиной:

    Решили использовать SQLite,
    потому что приложение должно работать
    без отдельного сервера.
    """

    pattern = re.compile(
        r"^\s*"
        r"(?:мы\s+)?"
        r"(?:решили|выбрали)"
        r"\s+(.+?)"
        r"\s*,?\s*"
        r"потому\s+что\s+"
        r"(.+?)"
        r"[.!]?\s*$",
        re.IGNORECASE,
    )

    match = pattern.match(
        text
    )

    if match is None:
        return None

    decision = (
        match.group(1)
        .strip()
        .rstrip(".,!?")
    )

    reason = (
        match.group(2)
        .strip()
        .rstrip(".,!?")
    )

    if (
        not decision
        or not reason
    ):
        return None

    return MemoryCandidate(
        memory_type="DECISION",
        content=_sentence(
            decision
        ),
        why=_sentence(
            reason
        ),
        importance=85,
        confidence=0.98,
        replaces=None,
    )


def _extract_user_rule(
    text: str,
) -> MemoryCandidate | None:
    normalized = _normalize(
        text
    )

    direct_rule_starts = (
        "никогда ",
        "всегда ",
        "нельзя ",
        "запрещено ",
        "не используй ",
        "не меняй ",
        "не удаляй ",
        "используй только ",
        "предпочитай ",
    )

    if normalized.startswith(
        direct_rule_starts
    ):
        return MemoryCandidate(
            memory_type="USER_RULE",
            content=text.strip(),
            why=None,
            importance=95,
            confidence=1.0,
            replaces=None,
        )

    project_markers = (
        "в этом проекте",
        "в данном проекте",
        "для этого проекта",
    )

    rule_words = (
        "никогда",
        "всегда",
        "нельзя",
        "запрещено",
        "не используй",
        "не меняй",
        "не удаляй",
        "используй только",
        "предпочитай",
    )

    has_project_scope = any(
        marker in normalized
        for marker in project_markers
    )

    has_rule_word = any(
        word in normalized
        for word in rule_words
    )

    if (
        has_project_scope
        and has_rule_word
    ):
        return MemoryCandidate(
            memory_type="USER_RULE",
            content=text.strip(),
            why=None,
            importance=95,
            confidence=1.0,
            replaces=None,
        )

    return None


def _extract_fact(
    text: str,
) -> MemoryCandidate | None:
    normalized = _normalize(
        text
    )

    fact_starts = (
        "в проекте используется ",
        "в этом проекте используется ",
        "в данном проекте используется ",
        "проект использует ",
        "backend использует ",
        "бэкенд использует ",
        "сервер использует ",
        "приложение использует ",
    )

    if not normalized.startswith(
        fact_starts
    ):
        return None

    uncertainty_words = (
        "возможно",
        "наверное",
        "может быть",
        "предположительно",
        "скорее всего",
    )

    if any(
        word in normalized
        for word in uncertainty_words
    ):
        return None

    return MemoryCandidate(
        memory_type="FACT",
        content=text.strip(),
        why=None,
        importance=75,
        confidence=0.95,
        replaces=None,
    )


def extract_memory_candidate(
    text: str,
) -> MemoryCandidate | None:
    """
    Memory Manager v0.3.

    Приоритет:

    1. Явная замена решения
    2. Обычное решение
    3. USER_RULE
    4. FACT

    Неоднозначный текст не сохраняется.
    """

    text = text.strip()

    if not text:
        return None

    if len(text) > 1000:
        return None

    if _looks_like_question(
        text
    ):
        return None

    if _looks_like_task(
        text
    ):
        return None

    replacement = (
        _extract_replacement_decision(
            text
        )
    )

    if replacement is not None:
        return replacement

    decision = _extract_decision(
        text
    )

    if decision is not None:
        return decision

    rule = _extract_user_rule(
        text
    )

    if rule is not None:
        return rule

    fact = _extract_fact(
        text
    )

    if fact is not None:
        return fact

    return None


def is_duplicate_memory(
    candidate: MemoryCandidate,
    memories: list[dict[str, object]],
) -> bool:
    candidate_content = _normalize(
        candidate.content
    )

    candidate_why = _normalize(
        candidate.why or ""
    )

    for memory in memories:
        if (
            memory.get("status")
            != "ACTIVE"
        ):
            continue

        if (
            memory.get("type")
            != candidate.memory_type
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

        why = _normalize(
            str(
                memory.get(
                    "why",
                    "",
                )
                or ""
            )
        )

        if (
            content == candidate_content
            and why == candidate_why
        ):
            return True

    return False


# =============================================
# BACKWARD COMPATIBILITY
# =============================================

def extract_user_rule(
    text: str,
) -> str | None:
    candidate = (
        extract_memory_candidate(
            text
        )
    )

    if (
        candidate is None
        or candidate.memory_type
        != "USER_RULE"
    ):
        return None

    return candidate.content


def is_duplicate_rule(
    rule: str,
    memories: list[dict[str, object]],
) -> bool:
    candidate = MemoryCandidate(
        memory_type="USER_RULE",
        content=rule,
        why=None,
        importance=95,
        confidence=1.0,
        replaces=None,
    )

    return is_duplicate_memory(
        candidate,
        memories,
    )