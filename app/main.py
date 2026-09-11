from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown

from app.context.builder import (
    build_current_user_message,
)
from app.llm.client import QwenClient
from app.memory.conflicts import (
    find_memories_to_supersede,
)
from app.memory.manager import (
    MemoryCandidate,
    extract_memory_candidate,
    is_duplicate_memory,
)
from app.memory.merge import (
    find_decisions_to_merge,
)
from app.memory.store import MemoryStore
from app.policy.guard import (
    is_policy_disclosure_request,
    policy_refusal,
)
from app.policy.loader import (
    load_system_prompt,
)
from app.policy.output_guard import (
    filter_model_output,
)


console = Console()


def show_commands() -> None:
    console.print()

    console.print(
        "[dim]Команды:[/dim]"
    )

    console.print(
        "[dim]"
        "/remember текст — сохранить правило вручную"
        "[/dim]"
    )

    console.print(
        "[dim]"
        "/memories — показать активную память"
        "[/dim]"
    )

    console.print(
        "[dim]"
        "/clear — очистить временный контекст"
        "[/dim]"
    )

    console.print(
        "[dim]"
        "/exit — выход"
        "[/dim]"
    )


def show_memory(
    memories: list[dict[str, object]],
) -> None:
    if not memories:
        console.print(
            "[yellow]"
            "Постоянная память пуста."
            "[/yellow]"
        )

        return

    console.print()

    console.print(
        "[bold cyan]"
        "Persistent Memory:"
        "[/bold cyan]"
    )

    for memory in memories:
        line = (
            f"[{memory['id']}] "
            f"{memory['type']} | "
            f"{memory['content']}"
        )

        why = memory.get(
            "why"
        )

        if why:
            line += (
                f" | WHY: {why}"
            )

        console.print(
            line
        )


def save_candidate(
    *,
    candidate: MemoryCandidate,
    memory_store: MemoryStore,
    existing_memories: list[
        dict[str, object]
    ],
) -> int | None:
    if is_duplicate_memory(
        candidate,
        existing_memories,
    ):
        console.print(
            "[dim]"
            "Memory: запись уже существует."
            "[/dim]"
        )

        return None

    memory_id = (
        memory_store.add_memory(
            memory_type=(
                candidate.memory_type
            ),
            content=(
                candidate.content
            ),
            why=(
                candidate.why
            ),
            source="USER",
            importance=(
                candidate.importance
            ),
            confidence=(
                candidate.confidence
            ),
        )
    )

    console.print(
        "[green]"
        "Memory: автоматически сохранено "
        f"{candidate.memory_type} "
        f"#{memory_id}"
        "[/green]"
    )

    return memory_id


def main() -> None:
    console.print(
        "[bold cyan]"
        "PersistentCoder v0.9"
        "[/bold cyan]"
    )

    console.print(
        "[dim]"
        "Model: Qwen2.5-Coder-0.5B-Instruct"
        "[/dim]"
    )

    # ==========================================
    # SYSTEM POLICY
    # ==========================================

    try:
        system_prompt = (
            load_system_prompt()
        )

    except Exception as exc:
        console.print(
            "[bold red]"
            f"Ошибка System Policy: {exc}"
            "[/bold red]"
        )

        return

    console.print(
        "[green]"
        "System Policy загружена."
        "[/green]"
    )

    # ==========================================
    # MODEL
    # ==========================================

    console.print(
        "[yellow]"
        "Загрузка модели..."
        "[/yellow]"
    )

    try:
        client = QwenClient()

    except Exception as exc:
        console.print(
            "[bold red]"
            f"Ошибка загрузки модели: {exc}"
            "[/bold red]"
        )

        return

    # ==========================================
    # MEMORY
    # ==========================================

    memory_store = MemoryStore()

    conversation_history: list[
        dict[str, str]
    ] = []

    console.print()

    console.print(
        "[green]"
        "PersistentCoder готов."
        "[/green]"
    )

    show_commands()

    # ==========================================
    # MAIN LOOP
    # ==========================================

    while True:
        console.print()

        user_input = console.input(
            "[bold green]Вы:[/bold green] "
        ).strip()

        if not user_input:
            continue

        normalized = (
            user_input.casefold()
        )

        # ======================================
        # EXIT
        # ======================================

        if normalized in {
            "/exit",
            "exit",
            "/quit",
            "quit",
        }:
            console.print(
                "[yellow]"
                "PersistentCoder остановлен."
                "[/yellow]"
            )

            break

        # ======================================
        # CLEAR
        # ======================================

        if normalized == "/clear":
            conversation_history.clear()

            console.print(
                "[yellow]"
                "Временный контекст очищен. "
                "Постоянная память сохранена."
                "[/yellow]"
            )

            continue

        # ======================================
        # SHOW MEMORY
        # ======================================

        if normalized == "/memories":
            show_memory(
                memory_store
                .get_active_memories()
            )

            continue

        # ======================================
        # MANUAL MEMORY
        # ======================================

        if normalized.startswith(
            "/remember "
        ):
            content = user_input[
                len("/remember "):
            ].strip()

            if not content:
                console.print(
                    "[yellow]"
                    "После /remember "
                    "нужно написать правило."
                    "[/yellow]"
                )

                continue

            candidate = MemoryCandidate(
                memory_type="USER_RULE",
                content=content,
                why=None,
                importance=95,
                confidence=1.0,
                replaces=None,
            )

            save_candidate(
                candidate=candidate,
                memory_store=memory_store,
                existing_memories=(
                    memory_store
                    .get_active_memories()
                ),
            )

            continue

        # ======================================
        # INPUT POLICY
        # ======================================

        if is_policy_disclosure_request(
            user_input
        ):
            console.print()

            console.print(
                "[bold cyan]"
                "PersistentCoder:"
                "[/bold cyan]"
            )

            console.print(
                policy_refusal()
            )

            continue

        # ======================================
        # MEMORY MANAGER
        # ======================================

        existing_memories = (
            memory_store
            .get_active_memories()
        )

        candidate = (
            extract_memory_candidate(
                user_input
            )
        )

        if candidate is not None:
            # ----------------------------------
            # EXPLICIT CONFLICT RESOLVER
            # ----------------------------------

            conflict_ids = (
                find_memories_to_supersede(
                    candidate,
                    existing_memories,
                )
            )

            # ----------------------------------
            # DECISION MERGE RESOLVER
            # ----------------------------------

            merge_ids = (
                find_decisions_to_merge(
                    candidate,
                    existing_memories,
                )
            )

            # Один ID может случайно попасть
            # сразу в обе категории.
            # Убираем дубли.
            memories_to_supersede = sorted(
                set(
                    conflict_ids
                    + merge_ids
                )
            )

            # ----------------------------------
            # SAVE NEW MEMORY
            # ----------------------------------

            new_memory_id = (
                save_candidate(
                    candidate=candidate,
                    memory_store=memory_store,
                    existing_memories=(
                        existing_memories
                    ),
                )
            )

            # ----------------------------------
            # SUPERSEDE OLD MEMORIES
            # ----------------------------------

            if (
                new_memory_id is not None
                and memories_to_supersede
            ):
                memory_store.supersede_memories(
                    memory_ids=(
                        memories_to_supersede
                    ),
                    superseded_by=(
                        new_memory_id
                    ),
                )

                for old_id in (
                    memories_to_supersede
                ):
                    console.print(
                        "[yellow]"
                        f"Memory: #{old_id} "
                        "помечена SUPERSEDED "
                        f"новой записью "
                        f"#{new_memory_id}"
                        "[/yellow]"
                    )

        # ======================================
        # RELOAD ACTIVE MEMORY
        # ======================================

        memories = (
            memory_store
            .get_active_memories()
        )

        # ======================================
        # CONTEXT
        # ======================================

        current_user_message = (
            build_current_user_message(
                user_input=user_input,
                memories=memories,
            )
        )

        model_messages: list[
            dict[str, str]
        ] = [
            {
                "role": "system",
                "content": system_prompt,
            }
        ]

        model_messages.extend(
            conversation_history
        )

        model_messages.append(
            {
                "role": "user",
                "content": (
                    current_user_message
                ),
            }
        )

        # ======================================
        # MODEL
        # ======================================

        try:
            raw_answer = (
                client.chat(
                    model_messages
                )
            )

        except Exception as exc:
            console.print(
                "[bold red]"
                f"Ошибка модели: {exc}"
                "[/bold red]"
            )

            continue

        # ======================================
        # OUTPUT POLICY
        # ======================================

        answer = (
            filter_model_output(
                answer=raw_answer,
                system_prompt=system_prompt,
            )
        )

        # ======================================
        # WORKING CONTEXT
        # ======================================

        conversation_history.append(
            {
                "role": "user",
                "content": user_input,
            }
        )

        conversation_history.append(
            {
                "role": "assistant",
                "content": answer,
            }
        )

        # ======================================
        # OUTPUT
        # ======================================

        console.print()

        console.print(
            "[bold cyan]"
            "PersistentCoder:"
            "[/bold cyan]"
        )

        console.print(
            Markdown(answer)
        )


if __name__ == "__main__":
    main()