import sqlite3

from app.memory.store import MemoryStore


def test_old_database_is_migrated_without_losing_memory(
    tmp_path,
):
    database_path = (
        tmp_path
        / "old_memory.db"
    )

    connection = sqlite3.connect(
        database_path
    )

    connection.execute(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT NOT NULL,
            importance INTEGER NOT NULL DEFAULT 50,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            created_at DATETIME
                NOT NULL
                DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    connection.execute(
        """
        INSERT INTO memories (
            type,
            content,
            source,
            importance
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            "USER_RULE",
            "Не использовать numpy.",
            "USER",
            95,
        ),
    )

    connection.commit()
    connection.close()

    store = MemoryStore(
        database_path=database_path
    )

    memories = (
        store.get_active_memories()
    )

    assert len(memories) == 1

    assert memories[0]["content"] == (
        "Не использовать numpy."
    )

    assert "why" in memories[0]
    assert "confidence" in memories[0]
    assert "updated_at" in memories[0]

def test_memory_can_be_marked_as_superseded(
    tmp_path,
):
    database_path = (
        tmp_path
        / "memory.db"
    )

    store = MemoryStore(
        database_path=database_path
    )

    old_id = store.add_memory(
        memory_type="FACT",
        content=(
            "В проекте используется PostgreSQL."
        ),
        source="USER",
        importance=75,
    )

    new_id = store.add_memory(
        memory_type="DECISION",
        content="Использовать SQLite.",
        source="USER",
        importance=90,
    )

    store.supersede_memories(
        memory_ids=[old_id],
        superseded_by=new_id,
    )

    active = (
        store.get_active_memories()
    )

    active_ids = {
        memory["id"]
        for memory in active
    }

    assert old_id not in active_ids
    assert new_id in active_ids

    connection = sqlite3.connect(
        database_path
    )

    connection.row_factory = (
        sqlite3.Row
    )

    row = connection.execute(
        """
        SELECT
            status,
            superseded_by
        FROM memories
        WHERE id = ?
        """,
        (old_id,),
    ).fetchone()

    connection.close()

    assert row["status"] == "SUPERSEDED"
    assert row["superseded_by"] == new_id