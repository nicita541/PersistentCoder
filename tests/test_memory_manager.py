from app.memory.manager import (
    MemoryCandidate,
    extract_memory_candidate,
    is_duplicate_memory,
)


def test_detects_user_rule():
    candidate = extract_memory_candidate(
        "В этом проекте никогда не меняй public API."
    )

    assert candidate is not None
    assert candidate.memory_type == "USER_RULE"
    assert candidate.content == (
        "В этом проекте никогда не меняй public API."
    )
    assert candidate.why is None


def test_normal_task_is_not_memory():
    candidate = extract_memory_candidate(
        "Напиши функцию factorial на Python."
    )

    assert candidate is None


def test_question_is_not_memory():
    candidate = extract_memory_candidate(
        "Можно ли использовать PostgreSQL?"
    )

    assert candidate is None


def test_detects_project_fact():
    candidate = extract_memory_candidate(
        "В проекте используется PostgreSQL."
    )

    assert candidate is not None
    assert candidate.memory_type == "FACT"
    assert candidate.content == (
        "В проекте используется PostgreSQL."
    )
    assert candidate.why is None


def test_detects_decision_with_reason():
    candidate = extract_memory_candidate(
        "Решили использовать SQLite, "
        "потому что приложение должно работать "
        "без отдельного сервера базы данных."
    )

    assert candidate is not None
    assert candidate.memory_type == "DECISION"

    assert candidate.content == (
        "Использовать SQLite."
    )

    assert candidate.why == (
        "Приложение должно работать "
        "без отдельного сервера базы данных."
    )


def test_uncertain_statement_is_not_memory():
    candidate = extract_memory_candidate(
        "Наверное, можно будет использовать Redis."
    )

    assert candidate is None


def test_duplicate_rule_is_detected():
    candidate = MemoryCandidate(
        memory_type="USER_RULE",
        content="Никогда не используй numpy.",
        why=None,
        importance=95,
        confidence=1.0,
    )

    memories = [
        {
            "type": "USER_RULE",
            "content": "Никогда не используй numpy.",
            "why": None,
            "status": "ACTIVE",
        }
    ]

    assert is_duplicate_memory(
        candidate,
        memories,
    )


def test_same_text_with_different_type_is_not_duplicate():
    candidate = MemoryCandidate(
        memory_type="FACT",
        content="Используется PostgreSQL.",
        why=None,
        importance=75,
        confidence=0.95,
    )

    memories = [
        {
            "type": "USER_RULE",
            "content": "Используется PostgreSQL.",
            "why": None,
            "status": "ACTIVE",
        }
    ]

    assert not is_duplicate_memory(
        candidate,
        memories,
    )

def test_detects_explicit_replacement_decision():
    candidate = extract_memory_candidate(
        "Теперь вместо PostgreSQL используем SQLite."
    )

    assert candidate is not None
    assert candidate.memory_type == "DECISION"
    assert candidate.content == "Использовать SQLite."
    assert candidate.replaces == "PostgreSQL"


def test_detects_replacement_with_reason():
    candidate = extract_memory_candidate(
        "Теперь вместо PostgreSQL используем SQLite, "
        "потому что отдельный сервер базы данных не нужен."
    )

    assert candidate is not None
    assert candidate.memory_type == "DECISION"
    assert candidate.content == "Использовать SQLite."
    assert candidate.replaces == "PostgreSQL"
    assert candidate.why == (
        "Отдельный сервер базы данных не нужен."
    )


def test_normal_decision_does_not_replace_old_memory():
    candidate = extract_memory_candidate(
        "Решили использовать SQLite, "
        "потому что он подходит для локального хранения."
    )

    assert candidate is not None
    assert candidate.replaces is None