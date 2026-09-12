from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown

from app.agent.runtime import AgentRuntime
from app.memory.manager import MemoryCandidate
from app.policy.guard import (
    is_policy_disclosure_request,
    policy_refusal,
)


console = Console()


def show_commands() -> None:
    console.print()
    console.print("[dim]Команды:[/dim]")
    console.print(
        "[dim]/remember текст — сохранить правило "
        "вручную[/dim]"
    )
    console.print(
        "[dim]/memories — показать активную память[/dim]"
    )
    console.print(
        "[dim]/clear — очистить временный контекст[/dim]"
    )
    console.print("[dim]/exit — выход[/dim]")


def show_memory(
    memories: list[dict[str, object]],
) -> None:
    if not memories:
        console.print(
            "[yellow]Постоянная память пуста.[/yellow]"
        )
        return

    console.print()
    console.print(
        "[bold cyan]Persistent Memory:"
        "[/bold cyan]"
    )

    for memory in memories:
        line = (
            f"[{memory['id']}] "
            f"{memory['type']} | "
            f"{memory['content']}"
        )

        if memory.get("why"):
            line += f" | WHY: {memory['why']}"

        console.print(line)


def render_state(state) -> str:
    lines: list[str] = [
        f"**Фаза:** {state.phase.value}"
    ]

    if state.plan_id is not None:
        lines.append(f"**План:** #{state.plan_id}")

    if state.global_goal:
        lines.append(f"**Цель:** {state.global_goal}")

    if state.execution is not None:
        lines.append(
            f"**Выполнение:** {state.execution.summary}"
        )

    if state.verification is not None:
        lines.append(
            "**Верификация:** "
            f"{state.verification.status} — "
            f"{state.verification.reason}"
        )

    if state.repair.required:
        lines.append(
            f"**Repair:** {state.repair.action} "
            f"({state.repair.scope}) — "
            f"{state.repair.reason}"
        )

    lines.append(
        f"**Результат:** "
        f"{state.completion or state.phase.value}"
    )

    return "\n\n".join(lines)


def main() -> None:
    console.print(
        "[bold cyan]PersistentCoder[/bold cyan]"
    )
    console.print(
        "[dim]Model: "
        "Qwen2.5-Coder-1.5B-Instruct[/dim]"
    )
    console.print(
        "[yellow]Загрузка модели...[/yellow]"
    )

    # ==========================================
    # SINGLE DEPENDENCY GRAPH
    # ==========================================

    try:
        runtime = AgentRuntime()

    except Exception as exc:
        console.print(
            "[bold red]"
            f"Ошибка запуска: {exc}"
            "[/bold red]"
        )
        return

    console.print(
        "[green]System Policy загружена.[/green]"
    )
    console.print(
        "[green]PersistentCoder готов.[/green]"
    )

    show_commands()

    while True:
        console.print()

        user_input = console.input(
            "[bold green]Вы:[/bold green] "
        ).strip()

        if not user_input:
            continue

        normalized = user_input.casefold()

        if normalized in {
            "exit",
            "/exit",
            "quit",
            "/quit",
        }:
            console.print(
                "[yellow]"
                "PersistentCoder остановлен."
                "[/yellow]"
            )
            break

        if normalized == "/clear":
            console.print(
                "[yellow]Временный контекст очищен. "
                "Постоянная память сохранена.[/yellow]"
            )
            continue

        if normalized == "/memories":
            show_memory(
                runtime.memory.get_active_memories()
            )
            continue

        if normalized.startswith("/remember "):
            content = user_input[
                len("/remember "):
            ].strip()

            if not content:
                console.print(
                    "[yellow]После /remember "
                    "нужно написать правило.[/yellow]"
                )
                continue

            memory_id = (
                runtime.memory.save_candidate(
                    MemoryCandidate(
                        memory_type="USER_RULE",
                        content=content,
                        why=None,
                        importance=95,
                        confidence=1.0,
                        replaces=None,
                    )
                )
            )

            if memory_id is None:
                console.print(
                    "[dim]Memory: запись "
                    "уже существует.[/dim]"
                )
            else:
                console.print(
                    "[green]Memory: сохранено "
                    f"USER_RULE #{memory_id}[/green]"
                )

            continue

        if is_policy_disclosure_request(
            user_input
        ):
            console.print(
                Markdown(policy_refusal())
            )
            continue

        # ======================================
        # SINGLE AGENT PIPELINE
        # ======================================

        try:
            state = runtime.run(user_input)

        except Exception as exc:
            console.print(
                "[bold red]"
                f"Ошибка выполнения: {exc}"
                "[/bold red]"
            )
            continue

        answer = render_state(state)

        console.print()
        console.print(
            "[bold cyan]PersistentCoder:"
            "[/bold cyan]"
        )
        console.print(Markdown(answer))


if __name__ == "__main__":
    main()

