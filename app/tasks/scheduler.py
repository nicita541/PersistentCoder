from __future__ import annotations

from app.tasks.models import (
    TaskRecord,
    TaskStatus,
)
from app.tasks.store import PlanStore


class TaskScheduler:
    """
    Dependency Manager + Task Selector.

    Отвечает за:

    - PENDING -> READY
    - READY -> PENDING
    - PENDING/READY -> BLOCKED
    - BLOCKED -> READY/PENDING
    - выбор следующей READY Task
    - правило "только одна активная Task"
    """

    ACTIVE_STATUSES = {
        TaskStatus.IN_PROGRESS,
        TaskStatus.VERIFYING,
    }

    TERMINAL_STATUSES = {
        TaskStatus.DONE,
        TaskStatus.FAILED,
        TaskStatus.SUPERSEDED,
    }

    BLOCKING_DEPENDENCY_STATUSES = {
        TaskStatus.FAILED,
        TaskStatus.BLOCKED,
        TaskStatus.SUPERSEDED,
    }

    def __init__(
        self,
        store: PlanStore,
    ) -> None:
        self.store = store

    def refresh(
        self,
        plan_id: int,
    ) -> None:
        """
        Пересчитывает состояние всех Task,
        которыми может управлять Dependency Manager.

        Не трогает:

        - IN_PROGRESS
        - VERIFYING
        - DONE
        - FAILED
        - SUPERSEDED
        """

        tasks = self.store.get_tasks(
            plan_id
        )

        tasks_by_id = {
            task.id: task
            for task in tasks
        }

        for task in tasks:
            if task.status in (
                self.ACTIVE_STATUSES
                | self.TERMINAL_STATUSES
            ):
                continue

            new_status = (
                self._calculate_status(
                    task=task,
                    tasks_by_id=tasks_by_id,
                )
            )

            if new_status is task.status:
                continue

            self.store.update_task_status(
                task.id,
                new_status,
            )

    def _calculate_status(
        self,
        *,
        task: TaskRecord,
        tasks_by_id: dict[
            int,
            TaskRecord,
        ],
    ) -> TaskStatus:
        """
        Правила:

        Нет dependencies
            -> READY

        Есть FAILED/BLOCKED/SUPERSEDED dependency
            -> BLOCKED

        Все dependencies DONE
            -> READY

        Иначе
            -> PENDING
        """

        if not task.depends_on:
            return TaskStatus.READY

        dependency_tasks: list[
            TaskRecord
        ] = []

        for dependency_id in (
            task.depends_on
        ):
            dependency = tasks_by_id.get(
                dependency_id
            )

            if dependency is None:
                # В нормальной БД такого быть
                # не должно из-за foreign key.
                # Но запускать Task при повреждённом
                # графе нельзя.
                return TaskStatus.BLOCKED

            dependency_tasks.append(
                dependency
            )

        if any(
            dependency.status
            in self.BLOCKING_DEPENDENCY_STATUSES
            for dependency
            in dependency_tasks
        ):
            return TaskStatus.BLOCKED

        if all(
            dependency.status
            is TaskStatus.DONE
            for dependency
            in dependency_tasks
        ):
            return TaskStatus.READY

        return TaskStatus.PENDING

    def get_active_task(
        self,
        plan_id: int,
    ) -> TaskRecord | None:
        """
        Возвращает текущую активную Task.

        В v1 разрешена только одна активная Task.
        """

        tasks = self.store.get_tasks(
            plan_id
        )

        active = [
            task
            for task in tasks
            if task.status
            in self.ACTIVE_STATUSES
        ]

        if not active:
            return None

        # Если БД каким-то образом уже содержит
        # несколько активных Task, ничего здесь
        # автоматически не исправляем.
        # Возвращаем самую старую.
        return min(
            active,
            key=lambda task: task.id,
        )

    def select_next(
        self,
        plan_id: int,
    ) -> TaskRecord | None:
        """
        Выбирает следующую READY Task.

        Приоритет:

        1. больше priority;
        2. при одинаковом priority —
           меньший SQLite id.

        Если уже есть IN_PROGRESS/VERIFYING,
        новую Task не выбираем.
        """

        if (
            self.get_active_task(
                plan_id
            )
            is not None
        ):
            return None

        tasks = self.store.get_tasks(
            plan_id
        )

        ready_tasks = [
            task
            for task in tasks
            if task.status
            is TaskStatus.READY
        ]

        if not ready_tasks:
            return None

        ready_tasks.sort(
            key=lambda task: (
                -task.priority,
                task.id,
            )
        )

        return ready_tasks[0]

    def start_next(
        self,
        plan_id: int,
    ) -> TaskRecord | None:
        """
        Пересчитывает dependencies,
        выбирает одну READY Task
        и переводит её в IN_PROGRESS.
        """

        self.refresh(
            plan_id
        )

        selected = self.select_next(
            plan_id
        )

        if selected is None:
            return None

        self.store.update_task_status(
            selected.id,
            TaskStatus.IN_PROGRESS,
        )

        # Возвращаем уже актуальную запись
        # после изменения SQLite.
        tasks = self.store.get_tasks(
            plan_id
        )

        for task in tasks:
            if task.id == selected.id:
                return task

        return None