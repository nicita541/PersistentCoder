from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown

from app.context.builder import (
    build_current_user_message,
)
from app.llm.client import QwenClient
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
        "/remember текст — сохранить пользовательское правило"
        "[/dim]"
    )

    console.print(
        "[dim]"
        "/memories      — показать пользовательскую память"
        "[/dim]"
    )

    console.print(
        "[dim]"
        "/clear         — очистить временный контекст"
        "[/dim]"
    )

    console.print(
        "[dim]"
        "/exit          — выход"
        "[/dim]"
    )


def main() -> None:
    console.print(
        "[bold cyan]"
        "PersistentCoder v0.5"
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

    # Временный Working Context.
    # Он существует только пока работает программа.
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

        normalized = user_input.casefold()

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
        # CLEAR WORKING CONTEXT
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
        # SHOW USER MEMORY
        # ======================================

        if normalized == "/memories":
            memories = (
                memory_store.get_active_memories()
            )

            if not memories:
                console.print(
                    "[yellow]"
                    "Пользовательская память пуста."
                    "[/yellow]"
                )

                continue

            console.print()

            console.print(
                "[bold cyan]"
                "User Persistent Memory:"
                "[/bold cyan]"
            )

            for memory in memories:
                console.print(
                    f"[{memory['id']}] "
                    f"{memory['type']} | "
                    f"{memory['content']} "
                    f"| source={memory['source']} "
                    f"| importance="
                    f"{memory['importance']}"
                )

            continue

        # ======================================
        # MANUAL MEMORY WRITE
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

            memory_id = (
                memory_store.add_memory(
                    memory_type="USER_RULE",
                    content=content,
                    source="USER",
                    importance=95,
                )
            )

            console.print(
                "[green]"
                f"Память сохранена. "
                f"ID={memory_id}"
                "[/green]"
            )

            continue

        # ======================================
        # INPUT POLICY GUARD
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

            # Этот пользовательский запрос
            # вообще НЕ передаётся модели.
            continue

        # ======================================
        # LOAD USER MEMORY
        # ======================================

        memories = (
            memory_store.get_active_memories()
        )

        # ======================================
        # BUILD USER CONTEXT
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

        # Временная история.
        model_messages.extend(
            conversation_history
        )

        # User Memory + текущая задача.
        model_messages.append(
            {
                "role": "user",
                "content": current_user_message,
            }
        )

        # ======================================
        # QWEN
        # ======================================

        try:
            raw_answer = client.chat(
                model_messages
            )

        except Exception as exc:
            console.print(
                "[bold red]"
                f"Ошибка модели: {exc}"
                "[/bold red]"
            )

            continue

        # ======================================
        # OUTPUT POLICY GUARD
        # ======================================

        answer = filter_model_output(
            answer=raw_answer,
            system_prompt=system_prompt,
        )

        # ======================================
        # UPDATE WORKING CONTEXT
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