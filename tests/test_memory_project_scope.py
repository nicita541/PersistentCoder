from __future__ import annotations

from pathlib import Path

import pytest

from app.memory.manager import MemoryManager, MemoryScopeError
from app.memory.store import MemoryScope, MemoryStore


def _manager(
    project_database: Path,
    project_id: str,
    global_database: Path,
) -> MemoryManager:
    return MemoryManager(
        project_store=MemoryStore(
            project_database,
            scope=MemoryScope.PROJECT,
            project_id=project_id,
        ),
        global_store=MemoryStore(
            global_database,
            scope=MemoryScope.GLOBAL,
        ),
    )


def test_project_memory_never_crosses_project_boundary(
    tmp_path: Path,
) -> None:
    project_database = tmp_path / "projects.db"
    global_database = tmp_path / "global.db"
    manager_a = _manager(project_database, "a" * 64, global_database)
    manager_b = _manager(project_database, "b" * 64, global_database)

    manager_a.remember("В проекте используется SQLite.")

    assert any(
        "SQLite" in str(memory["content"])
        for memory in manager_a.get_active_memories()
    )
    assert all(
        "SQLite" not in str(memory["content"])
        for memory in manager_b.get_active_memories()
    )


def test_only_explicit_user_rule_becomes_global(
    tmp_path: Path,
) -> None:
    project_database = tmp_path / "projects.db"
    global_database = tmp_path / "global.db"
    manager_a = _manager(project_database, "a" * 64, global_database)
    manager_b = _manager(project_database, "b" * 64, global_database)

    manager_a.remember(
        "Всегда запускай тесты.",
        global_rule=True,
    )

    assert any(
        memory["type"] == "USER_RULE"
        and memory["scope"] == "GLOBAL"
        for memory in manager_b.get_active_memories()
    )

    with pytest.raises(MemoryScopeError):
        manager_a.remember(
            "В проекте используется SQLite.",
            global_rule=True,
        )


def test_global_store_rejects_non_rule_record_directly(
    tmp_path: Path,
) -> None:
    store = MemoryStore(
        tmp_path / "global.db",
        scope=MemoryScope.GLOBAL,
    )

    with pytest.raises(ValueError):
        store.add_memory(
            memory_type="FACT",
            content="Must not become global",
        )


def test_bound_memory_store_does_not_adopt_legacy_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "memory.db"
    legacy = MemoryStore(database)
    legacy.add_memory(
        memory_type="FACT",
        content="Legacy project fact",
    )

    bound = MemoryStore(
        database,
        scope=MemoryScope.PROJECT,
        project_id="a" * 64,
    )

    assert bound.get_active_memories() == []
    assert len(legacy.get_active_memories()) == 1


def test_experience_is_project_scoped_and_typed(
    tmp_path: Path,
) -> None:
    manager = _manager(
        tmp_path / "project.db",
        "a" * 64,
        tmp_path / "global.db",
    )

    manager.record_experience(
        request="Create module",
        plan_id=7,
        outcome="DONE",
    )

    memories = manager.get_active_memories()
    assert len(memories) == 1
    assert memories[0]["type"] == "EXPERIENCE"
    assert memories[0]["scope"] == "PROJECT"
    assert memories[0]["project_id"] == "a" * 64
