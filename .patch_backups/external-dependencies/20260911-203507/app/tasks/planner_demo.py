from __future__ import annotations

from app.tasks.planner_runtime import (
    PlannerRuntime,
)


def main() -> None:
    print()
    print("PersistentCoder AI Planner")
    print("=" * 60)
    print()

    user_request = input(
        "Опиши задачу для Planner: "
    ).strip()

    if not user_request:
        print("Пустая задача.")
        return

    print()
    print("Загрузка Qwen...")
    print()

    runtime = PlannerRuntime()

    print()
    print("Построение плана...")
    print()

    plan_id, plan = runtime.create_plan(
        user_request
    )

    print()
    print("=" * 60)
    print(f"PLAN #{plan_id}")
    print("=" * 60)

    print()
    print("GLOBAL GOAL:")
    print(plan.global_goal)

    print()
    print("TASKS:")

    for index, task in enumerate(
        plan.tasks,
        start=1,
    ):
        print()

        print(
            f"{index}. "
            f"[{task.key}] "
            f"{task.title}"
        )

        print(
            f"   priority: "
            f"{task.priority}"
        )

        print(
            "   depends_on: "
            + (
                ", ".join(
                    task.depends_on
                )
                if task.depends_on
                else "-"
            )
        )

        print(
            "   requires: "
            + (
                ", ".join(
                    task.requires
                )
                if task.requires
                else "-"
            )
        )

        print(
            "   produces: "
            + (
                ", ".join(
                    task.produces
                )
                if task.produces
                else "-"
            )
        )

        print(
            "   success:"
        )

        for criterion in (
            task.success_criteria
        ):
            print(
                f"      - {criterion}"
            )

    print()
    print("План сохранён в SQLite.")


if __name__ == "__main__":
    main()