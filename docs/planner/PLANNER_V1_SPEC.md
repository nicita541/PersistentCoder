# PersistentCoder Planner / Task OS v1

## Цель

Planner превращает большую цель пользователя в управляемый граф задач,
отслеживает состояние каждой задачи и не позволяет выполнять задачу,
пока не завершены её зависимости.

## Иерархия

GLOBAL GOAL
→ TASK
→ STEP
→ ACTION

ACTION хранится в Event Log.
TASK и STEP являются постоянными сущностями Task OS.

## Task

Каждая задача содержит:

- id
- plan_id
- parent_id
- title
- description
- status
- priority
- requires
- produces
- depends_on
- success_criteria
- current_step
- attempt_count
- blockers
- result_summary
- result_artifacts
- verification_status
- verification_evidence
- created_at
- started_at
- finished_at
- updated_at

## Статусы Task

- PENDING
- READY
- IN_PROGRESS
- VERIFYING
- DONE
- BLOCKED
- FAILED
- SUPERSEDED

DONE может установить только Verifier.

## Dependencies

Задача может перейти PENDING → READY только когда все задачи
из depends_on имеют статус DONE.

Используется DAG.

Запрещены:

- зависимости задачи от самой себя;
- ссылки на несуществующие задачи;
- циклические зависимости.

Planner предлагает dependencies.

Dependency Validator проверяет их независимо от Planner.

## Requires / Produces

Каждая задача описывает:

REQUIRES:
что должно существовать до её начала.

PRODUCES:
какой результат задача создаёт.

Dependency Validator сопоставляет requires и produces.

## Success Criteria

У каждой TASK и STEP обязательны критерии завершения.

Executor не имеет права самостоятельно установить DONE.

Поток:

IN_PROGRESS
→ VERIFYING
→ Verifier
→ PASS → DONE
→ FAIL → IN_PROGRESS / REPLANNER

## Results

После успешной проверки сохраняются:

- result_summary
- result_artifacts
- verification_evidence

Результаты DONE dependencies могут передаваться следующей задаче
через Context Builder.

## Steps

TASK разбивается Local Step Planner на 3–7 STEP.

Один STEP:

- имеет одну основную цель;
- обычно затрагивает 1–3 связанных файла;
- создаёт один проверяемый результат;
- имеет собственные success criteria.

STEP не является ACTION.

Открытие файла, запуск команды и редактирование файла являются ACTION.

## Attempts

Каждая попытка хранится отдельно.

По умолчанию:

max_step_attempts = 3

После превышения лимита запускается Replanner.

## Replanner

Replanner изменяет минимально необходимый уровень:

1. ACTION
2. STEP
3. TASK
4. GLOBAL PLAN

DONE-записи не переписываются.

Если старое решение больше не подходит, создаётся новая задача,
а старая история остаётся.

## Context Builder

Перед выполнением STEP модель получает только:

- System Policy
- краткий Global Goal
- Current Task
- Current Step
- success criteria
- requires
- результаты необходимых DONE dependencies
- релевантную Persistent Memory
- релевантный код
- предыдущие неудачные попытки данного STEP
- разрешённые Tools

Полная история разговора и весь проект автоматически не передаются.

## Planner pipeline

USER REQUEST
→ Goal Analyzer
→ Task Decomposer
→ Dependency Builder
→ Dependency Validator
→ Task OS
→ Task Selector
→ Local Step Planner

## Verifier

Три уровня:

- Step Verifier
- Task Verifier
- Final Goal Verifier

Предпочтение отдаётся детерминированным проверкам:

- tests
- build
- import
- exit code
- file existence
- HTTP
- schema checks

Semantic Review является дополнительной проверкой.

Verifier может создавать isolated verification tests,
но не имеет права изменять production-код для прохождения проверки.

## Ограничения v1

- одна активная задача одновременно;
- без parallel execution;
- только hard dependencies;
- примерно 5–10 TASK глобального плана;
- примерно 3–7 STEP локального плана;
- история не удаляется.