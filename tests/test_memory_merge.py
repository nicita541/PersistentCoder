from app.memory.manager import (
    MemoryCandidate,
)
from app.memory.merge import (
    find_decisions_to_merge,
)


def test_same_decision_with_new_reason_supersedes_old_decision():
    candidate = MemoryCandidate(
        memory_type="DECISION",
        content="Использовать SQLite.",
        why=(
            "Отдельный сервер базы данных "
            "не нужен."
        ),
        importance=90,
        confidence=1.0,
        replaces=None,
    )

    memories = [
        {
            "id": 4,
            "type": "DECISION",
            "content": "Использовать SQLite.",
            "why": (
                "Приложение должно работать "
                "без отдельного сервера базы данных."
            ),
            "status": "ACTIVE",
        }
    ]

    assert find_decisions_to_merge(
        candidate,
        memories,
    ) == [4]


def test_different_decision_is_not_merged():
    candidate = MemoryCandidate(
        memory_type="DECISION",
        content="Использовать SQLite.",
        why="Подходит для локального хранения.",
        importance=90,
        confidence=1.0,
        replaces=None,
    )

    memories = [
        {
            "id": 10,
            "type": "DECISION",
            "content": "Использовать PostgreSQL.",
            "why": "Нужна серверная база.",
            "status": "ACTIVE",
        }
    ]

    assert find_decisions_to_merge(
        candidate,
        memories,
    ) == []


def test_fact_is_not_merged_as_decision():
    candidate = MemoryCandidate(
        memory_type="DECISION",
        content="Использовать SQLite.",
        why="Подходит для локального хранения.",
        importance=90,
        confidence=1.0,
        replaces=None,
    )

    memories = [
        {
            "id": 3,
            "type": "FACT",
            "content": "Использовать SQLite.",
            "why": None,
            "status": "ACTIVE",
        }
    ]

    assert find_decisions_to_merge(
        candidate,
        memories,
    ) == []


def test_user_rule_is_never_merged_as_decision():
    candidate = MemoryCandidate(
        memory_type="DECISION",
        content="Использовать SQLite.",
        why=None,
        importance=90,
        confidence=1.0,
        replaces=None,
    )

    memories = [
        {
            "id": 2,
            "type": "USER_RULE",
            "content": "Использовать SQLite.",
            "why": None,
            "status": "ACTIVE",
        }
    ]

    assert find_decisions_to_merge(
        candidate,
        memories,
    ) == []


def test_superseded_decision_is_ignored():
    candidate = MemoryCandidate(
        memory_type="DECISION",
        content="Использовать SQLite.",
        why="Новая причина.",
        importance=90,
        confidence=1.0,
        replaces=None,
    )

    memories = [
        {
            "id": 4,
            "type": "DECISION",
            "content": "Использовать SQLite.",
            "why": "Старая причина.",
            "status": "SUPERSEDED",
        }
    ]

    assert find_decisions_to_merge(
        candidate,
        memories,
    ) == []


def test_non_decision_candidate_does_not_merge_anything():
    candidate = MemoryCandidate(
        memory_type="FACT",
        content="В проекте используется SQLite.",
        why=None,
        importance=75,
        confidence=0.95,
        replaces=None,
    )

    memories = [
        {
            "id": 4,
            "type": "DECISION",
            "content": "Использовать SQLite.",
            "why": "Причина.",
            "status": "ACTIVE",
        }
    ]

    assert find_decisions_to_merge(
        candidate,
        memories,
    ) == []