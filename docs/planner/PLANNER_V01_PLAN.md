# Planner v0.1 Implementation Plan

## Goal

Создать фундамент Task OS:
Plan + Task модели и постоянное SQLite-хранилище.

## Architecture

Planner v0.1 не вызывает LLM.

Он предоставляет надёжные структуры данных и API хранения,
которые последующие версии Planner будут использовать для записи
сгенерированных планов.

## Files

Create:
- app/tasks/models.py
- app/tasks/store.py
- tests/test_task_models.py
- tests/test_task_store.py

Existing:
- app/tasks/__init__.py

Database:
- data/persistent_coder.db

Используем ту же SQLite-базу, но отдельные таблицы.

## Task 1 — Domain models

Создать:

TaskStatus

PlanStatus

TaskDraft

PlanDraft

TaskRecord

PlanRecord

TaskStatus:

- PENDING
- READY
- IN_PROGRESS
- VERIFYING
- DONE
- BLOCKED
- FAILED
- SUPERSEDED

PlanStatus:

- ACTIVE
- DONE
- SUPERSEDED
- FAILED

TaskDraft содержит:

- title
- description
- priority
- requires
- produces
- success_criteria
- parent_id

На первом этапе depends_on будет добавлен в Planner v0.2,
когда появится Dependency Graph.

## Task 2 — SQLite schema

Добавить таблицу plans.

Поля:

- id
- version
- user_request
- global_goal
- status
- created_at
- updated_at

Добавить таблицу tasks.

Поля:

- id
- plan_id
- parent_id
- title
- description
- status
- priority
- requires_json
- produces_json
- success_criteria_json
- current_step
- attempt_count
- result_summary
- result_artifacts_json
- verification_status
- verification_evidence_json
- created_at
- started_at
- finished_at
- updated_at

Не изменять таблицу memories.

## Task 3 — PlanStore

Реализовать:

create_plan(plan: PlanDraft) -> int

get_plan(plan_id: int) -> PlanRecord | None

get_active_plan() -> PlanRecord | None

get_tasks(plan_id: int) -> list[TaskRecord]

update_task_status(task_id, status)

set_task_result(...)

Создание plan + tasks должно быть одной SQLite transaction.

## Task 4 — Tests

Проверить:

- новый Plan создаётся;
- TASK по умолчанию PENDING;
- requires/produces/success criteria переживают запись и чтение;
- старые memories не удаляются;
- active plan можно восстановить после нового объекта PlanStore;
- status сохраняется после restart;
- result_summary сохраняется.

## TDD order

1. Написать test_task_models.py.
2. Убедиться, что тест падает.
3. Реализовать models.py.
4. Получить PASS.

5. Написать test_task_store.py.
6. Убедиться, что тест падает.
7. Реализовать store.py.
8. Получить PASS.

9. Запустить весь suite:

python -m pytest -q

Существующие Policy и Memory tests также обязаны остаться зелёными.

## Not included in v0.1

Пока НЕ реализуем:

- Qwen Goal Analyzer
- автоматическую декомпозицию
- depends_on
- DAG Validator
- Task Selector
- Steps
- Verifier
- Replanner

Это следующие версии Planner.