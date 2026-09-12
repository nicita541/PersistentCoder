from __future__ import annotations

from app.agent.runtime import AgentRuntime
from app.context.builder import ContextBuilder
from app.memory.manager import (
    MemoryCandidate,
)
from app.memory.retrieval import (
    MemoryQuery,
    MemoryRetriever,
)

from helpers import (
    FakeLLM,
    coder_envelope,
    dependencies_response,
    goal_response,
    tasks_response,
)


def _memory(
    memory_id: int,
    memory_type: str,
    content: str,
    *,
    importance: int = 50,
    confidence: float = 1.0,
    why: str | None = None,
    status: str = "ACTIVE",
) -> dict[str, object]:
    return {
        "id": memory_id,
        "type": memory_type,
        "content": content,
        "why": why,
        "importance": importance,
        "confidence": confidence,
        "status": status,
        "source": "USER",
    }


class _MemoryStub:
    def __init__(self, memories) -> None:
        self._memories = memories

    def get_active_memories(self):
        return self._memories


def test_user_rule_is_never_out_ranked():
    memories = [
        _memory(
            1,
            "FACT",
            "The login service lives in app/auth/service.py",
            importance=100,
        ),
        _memory(
            2,
            "USER_RULE",
            "Never change the public API without a request",
            importance=10,
        ),
    ]

    selection = MemoryRetriever(
        max_memories=1
    ).select(
        memories,
        MemoryQuery(
            current_task=(
                "Fix app/auth/service.py login"
            )
        ),
    )

    assert selection.ids() == [2]
    assert selection.rule_count == 1


def test_relevant_memory_beats_irrelevant_one():
    memories = [
        _memory(
            1,
            "DECISION",
            "LoginService.authenticate returns a token object",
            importance=60,
        ),
        _memory(
            2,
            "DECISION",
            "The billing exporter uses CSV formatting",
            importance=60,
        ),
    ]

    selection = MemoryRetriever(
        max_memories=1
    ).select(
        memories,
        MemoryQuery(
            current_task=(
                "Исправь LoginService.authenticate "
                "в app/auth/service.py"
            )
        ),
    )

    assert selection.ids() == [1]


def test_superseded_memories_are_not_selected():
    memories = [
        _memory(
            1,
            "DECISION",
            "Use PostgreSQL for storage",
            status="SUPERSEDED",
        ),
        _memory(
            2,
            "DECISION",
            "Use SQLite for storage",
        ),
    ]

    selection = MemoryRetriever().select(
        memories,
        MemoryQuery(current_task="storage decision"),
    )

    assert selection.ids() == [2]


def test_selection_respects_limits():
    memories = [
        _memory(
            index,
            "FACT",
            f"Fact {index} about the project",
        )
        for index in range(1, 21)
    ]

    selection = MemoryRetriever(
        max_memories=3
    ).select(memories)

    assert len(selection.memories) == 3
    assert selection.truncated is True
    assert selection.considered == 20

    tiny = MemoryRetriever(
        max_memories=10,
        max_memory_chars=40,
    ).select(memories)

    assert tiny.used_chars <= 40 + 32
    assert len(tiny.memories) <= 1


def test_selection_is_deterministic():
    memories = [
        _memory(1, "FACT", "alpha module"),
        _memory(2, "DECISION", "beta module"),
        _memory(3, "EXPERIENCE", "gamma module"),
    ]

    query = MemoryQuery(current_task="alpha")

    first = MemoryRetriever().select(memories, query)
    second = MemoryRetriever().select(memories, query)

    assert first.ids() == second.ids()


def test_context_builder_uses_the_selection():
    memories = [
        _memory(
            1,
            "FACT",
            "LoginService keeps its token in memory",
        ),
        _memory(
            2,
            "FACT",
            "Unrelated exporter uses CSV",
        ),
    ]

    builder = ContextBuilder(
        memory=_MemoryStub(memories),
        retriever=MemoryRetriever(max_memories=1),
    )

    messages = builder.build_chat_messages(
        user_input="Проверь LoginService",
    )

    content = messages[-1]["content"]

    assert "LoginService keeps its token" in content
    assert "Unrelated exporter" not in content


def test_memory_persists_across_runtime_restart_and_is_retrieved(
    tmp_path,
):
    database = tmp_path / "pc.db"

    first = AgentRuntime(
        workspace_root=tmp_path,
        database_path=database,
        llm=FakeLLM(),
        system_prompt="GLOBAL SYSTEM POLICY",
    )

    saved = first.memory.save_candidate(
        MemoryCandidate(
            memory_type="USER_RULE",
            content=(
                "Never change the public API of "
                "UserService without an explicit request"
            ),
            why=None,
            importance=95,
            confidence=1.0,
            replaces=None,
        )
    )

    assert saved is not None

    old_decision = first.memory.save_candidate(
        MemoryCandidate(
            memory_type="DECISION",
            content=(
                "UserService hashes passwords with MD5"
            ),
            why=None,
            importance=70,
            confidence=1.0,
            replaces=None,
        )
    )

    new_decision = first.memory.save_candidate(
        MemoryCandidate(
            memory_type="DECISION",
            content=(
                "UserService hashes passwords with bcrypt"
            ),
            why="MD5 is unsafe",
            importance=80,
            confidence=1.0,
            replaces=None,
        )
    )

    assert old_decision is not None
    assert new_decision is not None

    first.memory.store.supersede_memories(
        memory_ids=[old_decision],
        superseded_by=new_decision,
    )

    # --------------------------------------------------
    # Run #2: brand new runtime on the same SQLite DB.
    # --------------------------------------------------

    second_llm = FakeLLM(
        [
            goal_response(),
            tasks_response(),
            dependencies_response(),
        ],
        default=coder_envelope(),
    )

    second = AgentRuntime(
        workspace_root=tmp_path,
        database_path=database,
        llm=second_llm,
        system_prompt="GLOBAL SYSTEM POLICY",
    )

    loaded = second.memory.get_active_memories()

    assert any(
        memory["content"]
        == (
            "Never change the public API of "
            "UserService without an explicit request"
        )
        for memory in loaded
    )

    state = second.run(
        "Добавь метод в UserService, "
        "сохрани публичный API"
    )

    assert state.phase.value == "DONE"

    joined = "\n".join(
        str(message.get("content", ""))
        for call in second_llm.calls
        for message in call
    )

    # Relevance retrieval put the rule into the real context.
    assert (
        "Never change the public API of UserService"
        in joined
    )

    # The superseded decision is never used again.
    assert "hashes passwords with MD5" not in joined

    # The replacement decision is available instead.
    assert "hashes passwords with bcrypt" in joined
