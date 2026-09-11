from app.memory.conflicts import (
    find_memories_to_supersede,
)
from app.memory.manager import (
    MemoryCandidate,
)


def test_explicit_replacement_supersedes_matching_fact():
    candidate = MemoryCandidate(
        memory_type="DECISION",
        content="Использовать SQLite.",
        why=None,
        importance=90,
        confidence=1.0,
        replaces="PostgreSQL",
    )

    memories = [
        {
            "id": 3,
            "type": "FACT",
            "content": (
                "В проекте используется PostgreSQL."
            ),
            "why": None,
            "status": "ACTIVE",
        },
        {
            "id": 4,
            "type": "FACT",
            "content": (
                "В проекте используется Python."
            ),
            "why": None,
            "status": "ACTIVE",
        },
    ]

    assert find_memories_to_supersede(
        candidate,
        memories,
    ) == [3]


def test_unrelated_memory_is_not_superseded():
    candidate = MemoryCandidate(
        memory_type="DECISION",
        content="Использовать SQLite.",
        why=None,
        importance=90,
        confidence=1.0,
        replaces="PostgreSQL",
    )

    memories = [
        {
            "id": 10,
            "type": "FACT",
            "content": (
                "В проекте используется Redis."
            ),
            "why": None,
            "status": "ACTIVE",
        }
    ]

    assert find_memories_to_supersede(
        candidate,
        memories,
    ) == []


def test_user_rule_is_not_automatically_superseded():
    candidate = MemoryCandidate(
        memory_type="DECISION",
        content="Использовать SQLite.",
        why=None,
        importance=90,
        confidence=1.0,
        replaces="PostgreSQL",
    )

    memories = [
        {
            "id": 8,
            "type": "USER_RULE",
            "content": (
                "Всегда используй PostgreSQL."
            ),
            "why": None,
            "status": "ACTIVE",
        }
    ]

    assert find_memories_to_supersede(
        candidate,
        memories,
    ) == []