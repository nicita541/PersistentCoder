from __future__ import annotations

import argparse
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown

from app.agent.runtime import AgentRuntime
from app.memory.manager import MemoryCandidate
from app.policy.guard import (
    is_policy_disclosure_request,
    policy_refusal,
)
from app.sandbox.paths import configure_project_env


console = Console()


def show_commands() -> None:
    console.print()
    console.print("[dim]Команды:[/dim]")
    console.print(
        "[dim]/status — текущий run / план / task / step "
        "/ attempt / sandbox[/dim]"
    )
    console.print(
        "[dim]/plan — план, задачи, шаги, зависимости[/dim]"
    )
    console.print(
        "[dim]/memory — активная долговременная память[/dim]"
    )
    console.print(
        "[dim]/patch — путь patch, изменённые файлы, "
        "верификация[/dim]"
    )
    console.print(
        "[dim]/timeline — длительности стадий последнего "
        "прогона[/dim]"
    )
    console.print(
        "[dim]/apply — применить verified patch "
        "(нужно подтверждение)[/dim]"
    )
    console.print(
        "[dim]/discard — удалить неприменённые изменения "
        "и начать с чистого source (нужно подтверждение)[/dim]"
    )
    console.print(
        "[dim]/remember текст — сохранить правило "
        "вручную[/dim]"
    )
    console.print(
        "[dim]/clear — очистить временный контекст[/dim]"
    )
    console.print("[dim]/exit — выход[/dim]")


def show_memory(runtime) -> None:
    overview = runtime.memory_overview()

    if not overview["memories"]:
        console.print(
            "[yellow]Постоянная память пуста.[/yellow]"
        )
        return

    counts = ", ".join(
        f"{name}: {count}"
        for name, count
        in overview["counts"].items()
    )

    console.print()
    console.print(
        "[bold cyan]Persistent Memory:"
        f"[/bold cyan] {overview['total']} активных"
        f" ({counts})"
    )

    limit = 30

    for memory in overview["memories"][:limit]:
        line = (
            f"[{memory['id']}] "
            f"{memory['type']} | "
            f"{memory['content']}"
        )

        if memory.get("why"):
            line += f" | WHY: {memory['why']}"

        console.print(line)

    remaining = (
        len(overview["memories"]) - limit
    )

    if remaining > 0:
        console.print(
            f"[dim]... ещё {remaining} записей[/dim]"
        )


def render_plan(runtime) -> None:
    console.print()
    console.print(
        "[bold cyan]PLAN[/bold cyan]"
    )

    console.print(
        runtime.describe_plan()
    )


def render_status(runtime) -> None:
    status = runtime.run_status()

    console.print()
    console.print(
        "[bold cyan]STATUS[/bold cyan]"
    )

    for key in (
        "run_id",
        "run_status",
        "phase",
        "plan_id",
        "task_id",
        "step_id",
        "attempt_id",
        "active_plan",
        "sandbox_session_id",
        "session_status",
        "workspace",
        "patch",
        "interrupted_runs",
    ):
        console.print(
            f"{key}: {status.get(key)}"
        )

    console.print(status.get("sandbox"))

    dependencies = status.get("dependencies")

    if dependencies:
        console.print(
            "dependencies: "
            f"{dependencies['status']} — "
            f"{dependencies['image']} "
            f"({dependencies['reason']})"
        )

    for record in status.get("recovery") or []:
        console.print(
            "[yellow]recovery:[/yellow] "
            f"run #{record['run_id']} — "
            f"{record['note']}"
        )

    for record in status.get("apply_recovery") or []:
        console.print(
            "[yellow]apply recovery:[/yellow] "
            f"{record['status']} — {record.get('reason') or 'ok'}"
        )


def render_patch(runtime) -> None:
    preview = runtime.patch_preview()

    console.print()
    console.print(
        "[bold cyan]PATCH[/bold cyan]"
    )

    if not preview.get("manifest_id") and not preview.get("patch"):
        console.print(
            "[yellow]Проверенный результат ещё не создан. "
            "Сначала выполните задачу.[/yellow]"
        )
        return

    if preview.get("manifest_id"):
        console.print(f"manifest: {preview['manifest_id']}")
    if preview.get("patch"):
        console.print(f"review diff: {preview['patch']}")
        console.print(f"diff exists: {preview['patch_exists']}")
    console.print("changes:")

    changed = preview["changed_files"] or []

    if not changed:
        console.print("  (нет)")

    entries = preview.get("entries") or []
    if entries:
        for entry in entries:
            suffix = ""
            if not entry.get("apply_safe"):
                suffix = " — BLOCKED: " + ", ".join(entry.get("reasons") or [])
            console.print(
                f"  - {entry.get('operation')} {entry.get('path')}{suffix}"
            )
    else:
        for path in changed:
            console.print(f"  - {path}")

    verification = preview.get(
        "verification"
    )

    if verification:
        console.print(
            "verification: "
            f"{verification['status']} — "
            f"{verification['reason']}"
        )

    console.print(
        "[dim]Changes NOT applied automatically.[/dim]"
    )


def render_timeline(runtime) -> None:
    """
    Stage durations of the last run: where the time actually went.
    """

    timeline = runtime.timeline()

    console.print()
    console.print(
        "[bold cyan]TIMELINE[/bold cyan]"
    )

    if not timeline:
        console.print(
            "[yellow]Нет данных о прогоне.[/yellow]"
        )
        return

    for entry in timeline:
        delta = entry.get("since_previous_ms")

        delta_text = (
            f"{delta} ms"
            if delta is not None
            else "-"
        )

        console.print(
            f"{entry['event']:<24} "
            f"+{delta_text:<10} "
            f"(task={entry.get('task_id')} "
            f"step={entry.get('step_id')} "
            f"attempt={entry.get('attempt_id')})"
        )


def apply_patch_with_confirmation(runtime) -> None:
    preview = runtime.patch_preview()

    if not preview.get("manifest_id") and not preview.get("patch"):
        console.print(
            "[yellow]Проверенный результат не создан: "
            "нечего применять.[/yellow]"
        )
        return

    verification = (
        preview.get("verification") or {}
    )

    if (
        not preview.get("manifest_id")
        and (
            preview.get("phase") != "DONE"
            or not verification.get("ok")
        )
    ):
        console.print(
            "[bold red]Apply запрещён: "
            "run != DONE или verification != PASS."
            "[/bold red]"
        )
        console.print(
            "[dim]Изменения не применены.[/dim]"
        )
        return

    if preview.get("manifest_id") and not preview.get("can_apply"):
        console.print(
            "[bold red]Apply запрещён: "
            f"{preview.get('manifest_reason') or 'manifest содержит заблокированные пути'}."
            "[/bold red]"
        )
        return

    render_patch(runtime)

    answer = console.input(
        "[bold yellow]Apply verified patch? "
        "[y/N]:[/bold yellow] "
    ).strip().casefold()

    if answer not in {"y", "yes", "д", "да"}:
        console.print(
            "[yellow]Apply отменён "
            "(по умолчанию — нет).[/yellow]"
        )
        return

    result = runtime.apply_patch(confirmed=True)

    applied = result.get("applied") or []

    if not applied:
        console.print(
            "[bold red]Не применено: "
            f"{result.get('reason')}[/bold red]"
        )
        return

    console.print(
        "[green]Применено "
        f"{len(applied)} файл(ов):[/green]"
    )

    for path in applied:
        console.print(f"  - {path}")


def discard_session_with_confirmation(runtime) -> None:
    session = getattr(runtime, "session", None)
    status = getattr(getattr(session, "status", None), "value", None)
    if status not in {"DIRTY_VERIFIED", "DIRTY_FAILED"}:
        console.print("[yellow]Нет неприменённых изменений.[/yellow]")
        return

    answer = console.input(
        "[bold yellow]Discard all sandbox changes? "
        "[y/N]:[/bold yellow] "
    ).strip().casefold()
    if answer not in {"y", "yes", "д", "да"}:
        console.print("[yellow]Discard отменён.[/yellow]")
        return

    runtime.discard_session()
    console.print(
        "[green]Неприменённые изменения удалены; "
        "сессия снова CLEAN.[/green]"
    )


def render_result(state, runtime) -> str:
    """
    Compact user-facing result: plan progress + verified result.

    Internal noise (raw prompts, JSON envelopes) is never shown
    unless the run actually failed.
    """

    lines: list[str] = [
        f"**Фаза:** {state.phase.value}"
    ]

    if state.plan_id is not None:
        lines.append(
            f"**План:** #{state.plan_id}"
        )

    if state.global_goal:
        lines.append(
            f"**Цель:** {state.global_goal}"
        )

    execution = state.execution

    lines.append("")
    lines.append("**RESULT**")

    if execution is not None:
        lines.append(
            "✓ прочитано файлов: "
            f"{len(execution.read_files)}"
        )

        changed = [
            artifact
            for artifact in execution.artifacts
        ]

        lines.append(
            f"✓ изменено файлов: {len(changed)}"
        )

        for artifact in changed[:20]:
            lines.append(f"  - {artifact}")

        for command in execution.commands:
            marker = (
                "✓"
                if command.returncode == 0
                else "✗"
            )

            lines.append(
                f"{marker} {command.command} "
                f"(rc={command.returncode})"
            )

    verification = state.verification

    if verification is not None:
        for criterion in (
            verification.criterion_results
        ):
            marker = (
                "✓"
                if criterion.status == "PASS"
                else "✗"
            )

            lines.append(
                f"{marker} {criterion.criterion} "
                f"[{criterion.check}]"
            )

        lines.append(
            "✓ верификация: "
            f"{verification.status} — "
            f"{verification.reason}"
        )

    if state.repair.required:
        lines.append(
            f"**Repair:** {state.repair.action} "
            f"({state.repair.scope}) — "
            f"{state.repair.reason}"
        )

    if state.patch_path:
        lines.append("")
        lines.append(
            f"**Patch:** {state.patch_path}"
        )
        lines.append(
            "Changes NOT applied. "
            "Use /patch to inspect, /apply to apply "
            "after confirmation."
        )

    lines.append("")
    lines.append(
        "**Результат:** "
        f"{state.completion or state.phase.value}"
    )

    return "\n\n".join(lines)


def render_failure_hint(state) -> str:
    """
    Extra hint block shown only when a run did not finish DONE.
    """

    if state.phase.value == "DONE":
        return ""

    lines: list[str] = []

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

    if not lines:
        return ""

    return "\n\n".join(lines)


def make_progress_reporter(console):
    """
    Live stage progress for the interactive user.

    A CPU-only local model can spend minutes in one tool iteration:
    silence is unacceptable, so every stage reports itself.
    """

    def report(event) -> None:
        payload = event.payload or {}

        if event.name == "llm_tool_iteration":
            console.print(
                "[dim]llm iteration "
                f"{payload.get('iteration')}: "
                f"{payload.get('duration_ms')} ms, "
                f"{payload.get('answer_chars')} chars"
                "[/dim]"
            )
            return

        if event.name == "context_selection":
            console.print(
                "[dim]context selection: "
                f"{payload.get('duration_ms')} ms, "
                f"prompt {payload.get('prompt_chars')} chars"
                "[/dim]"
            )
            return

        if event.name == "docker_command":
            console.print(
                "[dim]docker command (rc="
                f"{payload.get('returncode')}, "
                f"{payload.get('duration_ms')} ms): "
                f"{payload.get('command')}[/dim]"
            )
            return

        if event.name in {
            "plan",
            "task_selected",
            "attempt_start",
            "attempt_finish",
            "verify",
            "repair",
            "step_done",
        }:
            console.print(
                f"[dim]{event.name} "
                f"{payload.get('status') or ''}"
                f"{payload.get('reason') or ''}"
                "[/dim]"
            )

    return report


def main(project_root: str | Path | None = None) -> None:
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
    # PROJECT-LOCAL STORAGE (no external writes)
    # ==========================================

    configure_project_env()

    # ==========================================
    # SINGLE DEPENDENCY GRAPH
    # ==========================================

    try:
        target_project = Path(project_root or Path.cwd()).resolve()
        runtime = AgentRuntime(project_root=target_project)

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

    console.print(
        "[green]Проект:[/green] "
        f"{runtime.source_project_root}"
    )

    console.print(
        "[dim]Sandbox: "
        f"{runtime.sandbox_status()}[/dim]"
    )

    show_commands()

    # Live stage progress (a 1.5B model on CPU is slow; silence is not
    # an option for an interactive user).
    runtime.events.subscribe(
        make_progress_reporter(console)
    )

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

        if normalized in {"help", "/help", "?"}:
            show_commands()
            continue

        if normalized == "/status":
            render_status(runtime)
            continue

        if normalized == "/plan":
            render_plan(runtime)
            continue

        if normalized in {
            "/memory",
            "/memories",
        }:
            show_memory(runtime)
            continue

        if normalized == "/patch":
            render_patch(runtime)
            continue

        if normalized == "/timeline":
            render_timeline(runtime)
            continue

        if normalized == "/apply":
            apply_patch_with_confirmation(
                runtime
            )
            continue

        if normalized == "/discard":
            discard_session_with_confirmation(runtime)
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

        answer = render_result(state, runtime)

        hint = render_failure_hint(state)

        console.print()
        console.print(
            "[bold cyan]PersistentCoder:"
            "[/bold cyan]"
        )
        console.print(Markdown(answer))

        if hint:
            console.print(Markdown(hint))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run PersistentCoder for one source project",
    )
    parser.add_argument(
        "project_root",
        nargs="?",
        default=str(Path.cwd()),
        help="source project directory (defaults to current directory)",
    )
    arguments = parser.parse_args()
    main(arguments.project_root)

