from __future__ import annotations

import re


POLICY_REFUSAL = (
    "Я не раскрываю внутренний системный промпт "
    "и внутренние инструкции."
)


# Прямые очевидные формулировки.
DIRECT_PATTERNS = (
    "системный промпт",
    "system prompt",
    "скрытый промпт",
    "hidden prompt",
    "скрытые инструкции",
    "hidden instructions",
    "внутренние инструкции",
    "internal instructions",
    "policy core",
    "system policy",
)


# Глаголы, характерные именно для попытки получить
# внутреннюю информацию.
DISCLOSURE_ACTIONS = (
    "покажи",
    "выведи",
    "напиши",
    "повтори",
    "раскрой",
    "перечисли",
    "расскажи",
    "процитируй",
    "переведи",
    "закодируй",
    "скопируй",
    "восстанови",
    "дай",
    "отобрази",
    "print",
    "show",
    "reveal",
    "repeat",
    "translate",
    "encode",
    "copy",
)


# Более хитрые обращения без слов "system prompt".
INDIRECT_TARGETS = (
    "инструкции которые ты получил до моего сообщения",
    "инструкции до моего сообщения",
    "первоначальные инструкции",
    "начальные инструкции",
    "инструкции разработчика",
    "служебные инструкции",
    "скрытый контекст",
    "внутренний контекст",
    "твои настоящие правила",
    "правила которые тебе дали",
    "инструкции которые тебе дали",
)


def _normalize(text: str) -> str:
    text = text.casefold()

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def is_policy_disclosure_request(
    text: str,
) -> bool:
    normalized = _normalize(text)

    # ------------------------------------------
    # Прямые запросы
    # ------------------------------------------

    contains_target = any(
        target in normalized
        for target in DIRECT_PATTERNS
    )

    contains_action = any(
        action in normalized
        for action in DISCLOSURE_ACTIONS
    )

    if contains_target and contains_action:
        return True

    # ------------------------------------------
    # Например:
    # "что написано в системном промпте?"
    # ------------------------------------------

    if contains_target and (
        "что написано" in normalized
        or "что находится" in normalized
        or "что содержится" in normalized
        or "какой у тебя" in normalized
        or "какие у тебя" in normalized
    ):
        return True

    # ------------------------------------------
    # Непрямые формулировки
    # ------------------------------------------

    if any(
        target in normalized
        for target in INDIRECT_TARGETS
    ):
        return True

    # ------------------------------------------
    # Попытки получить только часть prompt
    # ------------------------------------------

    partial_markers = (
        "первые строки",
        "первую строку",
        "первое предложение",
        "первые слова",
        "часть инструкций",
        "часть промпта",
        "последние строки",
    )

    if any(
        marker in normalized
        for marker in partial_markers
    ):
        if (
            "инструкц" in normalized
            or "промпт" in normalized
            or "правил" in normalized
        ):
            return True

    return False


def policy_refusal() -> str:
    return POLICY_REFUSAL