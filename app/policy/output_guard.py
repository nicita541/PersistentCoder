from __future__ import annotations

import re


SAFE_BLOCKED_RESPONSE = (
    "Я не раскрываю внутренний системный промпт "
    "и внутренние инструкции."
)


def _normalize_text(
    text: str,
) -> str:
    text = text.casefold()

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def _get_words(
    text: str,
) -> list[str]:
    return re.findall(
        r"[a-zа-яё0-9_]+",
        text.casefold(),
    )


def _contains_protected_line(
    answer: str,
    system_prompt: str,
) -> bool:
    """
    Проверяет, не скопировала ли модель
    значимый кусок системного промпта.
    """

    normalized_answer = _normalize_text(
        answer
    )

    for raw_line in system_prompt.splitlines():
        line = _normalize_text(
            raw_line
        )

        # Очень короткие строки вроде:
        # "1. Отвечай..."
        # могут случайно совпасть.
        if len(line) < 35:
            continue

        if line in normalized_answer:
            return True

    return False


def _contains_protected_word_sequence(
    answer: str,
    system_prompt: str,
    sequence_size: int = 10,
) -> bool:
    """
    Даже если модель скопировала только часть строки,
    ищем длинную последовательность слов из prompt.

    Например 10 слов подряд.
    """

    answer_words = _get_words(
        answer
    )

    prompt_words = _get_words(
        system_prompt
    )

    if (
        len(answer_words) < sequence_size
        or len(prompt_words) < sequence_size
    ):
        return False

    answer_sequences = {
        tuple(
            answer_words[
                index:index + sequence_size
            ]
        )
        for index in range(
            len(answer_words)
            - sequence_size
            + 1
        )
    }

    for index in range(
        len(prompt_words)
        - sequence_size
        + 1
    ):
        prompt_sequence = tuple(
            prompt_words[
                index:index + sequence_size
            ]
        )

        if prompt_sequence in answer_sequences:
            return True

    return False


def contains_protected_policy_content(
    *,
    answer: str,
    system_prompt: str,
) -> bool:
    """
    Финальная проверка ответа модели.

    True:
        ответ подозрительно содержит
        внутренний system prompt.

    False:
        явного копирования не найдено.
    """

    if not answer.strip():
        return False

    if _contains_protected_line(
        answer,
        system_prompt,
    ):
        return True

    if _contains_protected_word_sequence(
        answer,
        system_prompt,
    ):
        return True

    return False


def filter_model_output(
    *,
    answer: str,
    system_prompt: str,
) -> str:
    if contains_protected_policy_content(
        answer=answer,
        system_prompt=system_prompt,
    ):
        return SAFE_BLOCKED_RESPONSE

    return answer