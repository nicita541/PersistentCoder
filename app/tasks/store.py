from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from app.tasks.dependencies import (
    validate_task_graph,
)
from app.tasks.models import (
    PlanDraft,
    PlanRecord,
    PlanStatus,
    TaskDraft,
    TaskRecord,
    TaskStatus,
)
from app.tasks.store_context import (
    StoreContext,
    owns_task,
    split_store_binding,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_DATABASE_PATH = (
    PROJECT_ROOT
    / "data"
    / "persistent_coder.db"
)


class PlanStore:
    def __init__(
        self,
        database_path: StoreContext | str | Path | None = None,
    ) -> None:
        self.database_path, self.context = split_store_binding(
            database_path,
            default_database_path=DEFAULT_DATABASE_PATH,
        )

        self.database_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._initialize_database()

        self._ensure_external_dependencies_column()
        self._migrate_database()

    # ==========================================
    # CONNECTION
    # ==========================================

    def _connect(
        self,
    ) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path
        )

        connection.row_factory = (
            sqlite3.Row
        )

        connection.execute(
            "PRAGMA foreign_keys = ON"
        )

        return connection

    # ==========================================
    # DATABASE INITIALIZATION
    # ==========================================

    def _initialize_database(
        self,
    ) -> None:
        """
        Создаёт актуальную Planner schema
        для новой базы.

        Существующие таблицы memories
        не изменяются.
        """

        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER
                        PRIMARY KEY AUTOINCREMENT,

                    version INTEGER
                        NOT NULL
                        DEFAULT 1,

                    project_id TEXT,
                    canonical_source_root TEXT,

                    user_request TEXT
                        NOT NULL,

                    global_goal TEXT
                        NOT NULL,

                    status TEXT
                        NOT NULL
                        DEFAULT 'ACTIVE',

                    created_at DATETIME
                        NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,

                    updated_at DATETIME
                        NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER
                        PRIMARY KEY AUTOINCREMENT,

                    plan_id INTEGER
                        NOT NULL,

                    parent_id INTEGER,

                    task_key TEXT,

                    title TEXT
                        NOT NULL,

                    description TEXT
                        NOT NULL,

                    status TEXT
                        NOT NULL
                        DEFAULT 'PENDING',

                    priority INTEGER
                        NOT NULL
                        DEFAULT 50,

                    requires_json TEXT
                        NOT NULL
                        DEFAULT '[]',

                    produces_json TEXT
                        NOT NULL
                        DEFAULT '[]',

                    success_criteria_json TEXT
                        NOT NULL
                        DEFAULT '[]',

                    current_step INTEGER,

                    attempt_count INTEGER
                        NOT NULL
                        DEFAULT 0,

                    result_summary TEXT,

                    result_artifacts_json TEXT
                        NOT NULL
                        DEFAULT '[]',

                    verification_status TEXT,

                    verification_evidence_json TEXT
                        NOT NULL
                        DEFAULT '[]',

                    created_at DATETIME
                        NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,

                    started_at DATETIME,

                    finished_at DATETIME,

                    updated_at DATETIME
                        NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,

                    FOREIGN KEY (plan_id)
                        REFERENCES plans(id)
                )
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS
                    task_dependencies (
                        task_id INTEGER NOT NULL,

                        depends_on_task_id
                            INTEGER NOT NULL,

                        PRIMARY KEY (
                            task_id,
                            depends_on_task_id
                        ),

                        FOREIGN KEY (task_id)
                            REFERENCES tasks(id)
                            ON DELETE CASCADE,

                        FOREIGN KEY (
                            depends_on_task_id
                        )
                            REFERENCES tasks(id)
                            ON DELETE CASCADE,

                        CHECK (
                            task_id
                            != depends_on_task_id
                        )
                    )
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_tasks_plan_id
                ON tasks(plan_id)
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_tasks_status
                ON tasks(status)
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_dependencies_task
                ON task_dependencies(task_id)
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_dependencies_parent
                ON task_dependencies(
                    depends_on_task_id
                )
                """
            )

    def get_task(
            self,
            task_id: int,
    ) -> TaskRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT plan_id
                FROM tasks
                WHERE id = ?
                """,
                (task_id,),
            ).fetchone()

        if row is None:
            return None

        plan_id = int(
            row["plan_id"]
        )

        tasks = self.get_tasks(
            plan_id
        )

        for task in tasks:
            if task.id == task_id:
                return task

        return None

    def set_task_verification(
            self,
            task_id: int,
            *,
            verification_status: str,
            verification_evidence: list[str],
    ) -> None:
        if self.get_task(task_id) is None:
            raise ValueError(
                f"Unknown task for current project: {task_id}"
            )

        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE tasks

                SET
                    verification_status = ?,
                    verification_evidence_json = ?,
                    updated_at =
                        CURRENT_TIMESTAMP

                WHERE id = ?
                """,
                (
                    verification_status,
                    self._dump_list(
                        verification_evidence
                    ),
                    task_id,
                ),
            )

            if result.rowcount == 0:
                raise ValueError(
                    f"Unknown task: {task_id}"
                )

    # ==========================================
    # DATABASE MIGRATION
    # ==========================================

    def _get_columns(
        self,
        connection: sqlite3.Connection,
        table_name: str,
    ) -> set[str]:
        rows = connection.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()

        return {
            str(row["name"])
            for row in rows
        }

    def _migrate_database(
        self,
    ) -> None:
        """
        Planner v0.1 -> Planner v0.2.

        Старая таблица tasks сохраняется.

        Добавляется:
        - tasks.task_key
        - task_dependencies

        Старым Task автоматически выдаётся
        стабильный legacy key.
        """

        with self._connect() as connection:
            task_columns = self._get_columns(
                connection,
                "tasks",
            )

            if "task_key" not in task_columns:
                connection.execute(
                    """
                    ALTER TABLE tasks
                    ADD COLUMN task_key TEXT
                    """
                )

            # ----------------------------------
            # Старые Task v0.1 не имели key.
            #
            # Даём им стабильный ключ:
            # legacy_1, legacy_2 ...
            # ----------------------------------

            connection.execute(
                """
                UPDATE tasks

                SET task_key =
                    'legacy_' || id

                WHERE
                    task_key IS NULL
                    OR TRIM(task_key) = ''
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS
                    task_dependencies (
                        task_id INTEGER NOT NULL,

                        depends_on_task_id
                            INTEGER NOT NULL,

                        PRIMARY KEY (
                            task_id,
                            depends_on_task_id
                        ),

                        FOREIGN KEY (task_id)
                            REFERENCES tasks(id)
                            ON DELETE CASCADE,

                        FOREIGN KEY (
                            depends_on_task_id
                        )
                            REFERENCES tasks(id)
                            ON DELETE CASCADE,

                        CHECK (
                            task_id
                            != depends_on_task_id
                        )
                    )
                """
            )

            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_tasks_plan_key

                ON tasks(
                    plan_id,
                    task_key
                )
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_dependencies_task

                ON task_dependencies(task_id)
                """
            )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_dependencies_parent

                ON task_dependencies(
                    depends_on_task_id
                )
                """
            )

            # ----------------------------------
            # PLAN REVISIONS (global replan).
            #
            # History is never destroyed: a replan creates a NEW
            # plan row and the previous one becomes SUPERSEDED.
            # ----------------------------------

            plan_columns = self._get_columns(
                connection,
                "plans",
            )

            if "project_id" not in plan_columns:
                connection.execute(
                    "ALTER TABLE plans ADD COLUMN project_id TEXT"
                )

            if "canonical_source_root" not in plan_columns:
                connection.execute(
                    "ALTER TABLE plans "
                    "ADD COLUMN canonical_source_root TEXT"
                )

            if "replan_reason" not in plan_columns:
                connection.execute(
                    """
                    ALTER TABLE plans
                    ADD COLUMN replan_reason TEXT
                    """
                )

            if "replaces_plan_id" not in plan_columns:
                connection.execute(
                    """
                    ALTER TABLE plans
                    ADD COLUMN replaces_plan_id INTEGER
                    """
                )

            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_plans_project_status
                ON plans(project_id, status)
                """
            )

    def _plan_scope(
        self,
    ) -> tuple[str, tuple[object, ...]]:
        if self.context is None:
            return "project_id IS NULL", ()

        return (
            "project_id = ? AND canonical_source_root = ?",
            (
                self.context.project_id,
                self.context.canonical_source_root,
            ),
        )

    # ==========================================
    # JSON HELPERS
    # ==========================================

    @staticmethod
    def _dump_list(
        value: list[str],
    ) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
        )

    @staticmethod
    def _load_list(
        value: str | None,
    ) -> list[str]:
        if not value:
            return []

        data = json.loads(
            value
        )

        if not isinstance(
            data,
            list,
        ):
            raise ValueError(
                "Ожидался JSON-массив."
            )

        return [
            str(item)
            for item in data
        ]

    # ==========================================
    # TASK KEY HELPERS
    # ==========================================

    @staticmethod
    def _normalize_key(
        value: str,
    ) -> str:
        return value.strip().casefold()

    def _prepare_tasks(
        self,
        tasks: list[TaskDraft],
    ) -> list[TaskDraft]:
        """
        Planner v0.2 использует stable task keys.

        Старые вызовы v0.1 могли создавать TaskDraft
        без key. Для совместимости таким задачам
        автоматически выдаются task_1, task_2 ...

        Исходные TaskDraft не изменяются.
        """

        used_keys: set[str] = set()

        # Сначала резервируем явно заданные key.
        for task in tasks:
            if (
                task.key is None
                or not task.key.strip()
            ):
                continue

            normalized = self._normalize_key(
                task.key
            )

            used_keys.add(
                normalized
            )

        prepared: list[TaskDraft] = []

        generated_number = 1

        for task in tasks:
            if (
                task.key is not None
                and task.key.strip()
            ):
                resolved_key = (
                    task.key.strip()
                )

            else:
                while True:
                    candidate = (
                        f"task_{generated_number}"
                    )

                    generated_number += 1

                    if (
                        self._normalize_key(
                            candidate
                        )
                        not in used_keys
                    ):
                        resolved_key = (
                            candidate
                        )
                        break

            used_keys.add(
                self._normalize_key(
                    resolved_key
                )
            )

            prepared.append(
                replace(
                    task,
                    key=resolved_key,
                )
            )

        return prepared

    # ==========================================
    # CREATE PLAN
    # ==========================================

    def _ensure_external_dependencies_column(
        self,
    ) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                "PRAGMA table_info(tasks)"
            ).fetchall()

            columns = {
                str(row["name"])
                for row in rows
            }

            if (
                "external_dependencies_json"
                not in columns
            ):
                connection.execute(
                    """
                    ALTER TABLE tasks
                    ADD COLUMN
                        external_dependencies_json
                        TEXT
                        NOT NULL
                        DEFAULT '[]'
                    """
                )

    def create_plan(
        self,
        plan,
    ):
        plan_id = (
            self._create_plan_without_external_dependencies(
                plan
            )
        )

        draft_tasks = list(
            getattr(
                plan,
                "tasks",
                [],
            )
        )

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id
                FROM tasks
                WHERE plan_id = ?
                ORDER BY id ASC
                """,
                (plan_id,),
            ).fetchall()

            if len(rows) != len(draft_tasks):
                raise RuntimeError(
                    "Task count mismatch while saving "
                    "external_dependencies"
                )

            for row, task in zip(
                rows,
                draft_tasks,
            ):
                value = list(
                    getattr(
                        task,
                        "external_dependencies",
                        [],
                    )
                )

                connection.execute(
                    """
                    UPDATE tasks
                    SET
                        external_dependencies_json = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        self._dump_list(value),
                        int(row["id"]),
                    ),
                )

        return plan_id

    # ==========================================
    # PLAN REVISIONS / GLOBAL REPLAN
    # ==========================================

    def set_plan_status(
        self,
        plan_id: int,
        status: PlanStatus,
    ) -> None:
        if self.get_plan(plan_id) is None:
            raise ValueError(
                f"Unknown plan for current project: {plan_id}"
            )

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE plans
                SET
                    status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status.value, plan_id),
            )

    def set_plan_replan_metadata(
        self,
        plan_id: int,
        *,
        version: int,
        replaces_plan_id: int,
        reason: str,
    ) -> None:
        if self.get_plan(plan_id) is None:
            raise ValueError(
                f"Unknown plan for current project: {plan_id}"
            )
        if self.get_plan(replaces_plan_id) is None:
            raise ValueError(
                "Unknown replaced plan for current project: "
                f"{replaces_plan_id}"
            )

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE plans
                SET
                    version = ?,
                    replaces_plan_id = ?,
                    replan_reason = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    int(version),
                    int(replaces_plan_id),
                    reason,
                    plan_id,
                ),
            )

    def get_plan_revision(
        self,
        plan_id: int,
    ) -> dict[str, object] | None:
        """
        version + replan_reason + replaces_plan_id of a plan.
        """

        if self.get_plan(plan_id) is None:
            return None

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    version,
                    status,
                    replaces_plan_id,
                    replan_reason

                FROM plans
                WHERE id = ?
                """,
                (plan_id,),
            ).fetchone()

        return dict(row) if row is not None else None

    def create_replan(
        self,
        previous_plan_id: int,
        plan: PlanDraft,
        *,
        reason: str,
        invalidate_task_keys: tuple[str, ...] = (),
    ) -> int:
        """
        Create a NEW plan revision without destroying history.

        - the previous plan and its tasks stay in SQLite and become
          SUPERSEDED;
        - the new plan gets version = previous.version + 1, remembers
          WHY it exists and which plan it replaces;
        - tasks already DONE are carried over (never re-executed);
        - explicitly invalidated task keys are marked SUPERSEDED.
        """

        previous = self.get_plan(previous_plan_id)

        if previous is None:
            raise ValueError(
                f"unknown plan: {previous_plan_id}"
            )

        done_by_key: dict[str, TaskRecord] = {}

        for task in self.get_tasks(previous_plan_id):
            if (
                task.status is TaskStatus.DONE
                and task.key
            ):
                done_by_key[task.key] = task

        new_plan_id = self.create_plan(plan)

        self.set_plan_status(
            previous_plan_id,
            PlanStatus.SUPERSEDED,
        )

        self.set_plan_replan_metadata(
            new_plan_id,
            version=int(previous.version) + 1,
            replaces_plan_id=previous_plan_id,
            reason=reason,
        )

        invalidated = set(invalidate_task_keys)

        for task in self.get_tasks(new_plan_id):
            key = task.key or ""

            if key in invalidated:
                self.update_task_status(
                    task.id,
                    TaskStatus.SUPERSEDED,
                )

                continue

            carried = done_by_key.get(key)

            if carried is None:
                continue

            self.update_task_status(
                task.id,
                TaskStatus.DONE,
            )

            self.set_task_result(
                task.id,
                result_summary=(
                    carried.result_summary
                    or "carried over from previous plan "
                    "revision"
                ),
                result_artifacts=list(
                    carried.result_artifacts
                ),
                verification_status=(
                    carried.verification_status or "PASS"
                ),
                verification_evidence=list(
                    carried.verification_evidence
                ),
            )

        return new_plan_id

    def _create_plan_without_external_dependencies(
        self,
        plan: PlanDraft,
    ) -> int:
        """
        Создаёт Plan + Task + Dependencies
        одной SQLite transaction.

        Для Planner v0.2 граф сначала
        проверяется Dependency Validator.

        Legacy v0.1 Plan без key/depends_on
        продолжает поддерживаться.
        """

        original_tasks = plan.tasks

        prepared_tasks = self._prepare_tasks(
            original_tasks
        )

        # --------------------------------------
        # Определяем, является ли это уже
        # Planner v0.2 graph.
        #
        # Старые v0.1 тесты не имели key
        # и depends_on, поэтому их пропускаем
        # через compatibility mode.
        # --------------------------------------

        graph_mode = any(
            (
                task.key is not None
                and bool(task.key.strip())
            )
            or bool(task.depends_on)
            for task in original_tasks
        )

        if graph_mode:
            validate_task_graph(
                prepared_tasks
            )

        with self._connect() as connection:
            # ----------------------------------
            # PLAN
            # ----------------------------------

            cursor = connection.execute(
                """
                INSERT INTO plans (
                    version,
                    project_id,
                    canonical_source_root,
                    user_request,
                    global_goal,
                    status
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    (
                        self.context.project_id
                        if self.context is not None
                        else None
                    ),
                    (
                        self.context.canonical_source_root
                        if self.context is not None
                        else None
                    ),
                    plan.user_request,
                    plan.global_goal,
                    PlanStatus.ACTIVE.value,
                ),
            )

            plan_id = int(
                cursor.lastrowid
            )

            # normalized task key
            # →
            # SQLite task id
            key_to_id: dict[
                str,
                int,
            ] = {}

            # ----------------------------------
            # TASKS
            # ----------------------------------

            for task in prepared_tasks:
                assert task.key is not None

                task_cursor = connection.execute(
                    """
                    INSERT INTO tasks (
                        plan_id,
                        parent_id,
                        task_key,
                        title,
                        description,
                        status,
                        priority,
                        requires_json,
                        produces_json,
                        success_criteria_json,
                        current_step,
                        attempt_count,
                        result_artifacts_json,
                        verification_evidence_json
                    )
                    VALUES (
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        ?,
                        NULL,
                        0,
                        '[]',
                        '[]'
                    )
                    """,
                    (
                        plan_id,
                        task.parent_id,
                        task.key,
                        task.title,
                        task.description,
                        TaskStatus.PENDING.value,
                        task.priority,
                        self._dump_list(
                            task.requires
                        ),
                        self._dump_list(
                            task.produces
                        ),
                        self._dump_list(
                            task.success_criteria
                        ),
                    ),
                )

                task_id = int(
                    task_cursor.lastrowid
                )

                key_to_id[
                    self._normalize_key(
                        task.key
                    )
                ] = task_id

            # ----------------------------------
            # DEPENDENCIES
            # ----------------------------------

            for task in prepared_tasks:
                assert task.key is not None

                task_id = key_to_id[
                    self._normalize_key(
                        task.key
                    )
                ]

                seen_dependencies: set[
                    int
                ] = set()

                for dependency_key in (
                    task.depends_on
                ):
                    normalized_dependency = (
                        self._normalize_key(
                            dependency_key
                        )
                    )

                    depends_on_task_id = (
                        key_to_id[
                            normalized_dependency
                        ]
                    )

                    if (
                        depends_on_task_id
                        in seen_dependencies
                    ):
                        continue

                    connection.execute(
                        """
                        INSERT INTO
                            task_dependencies (
                                task_id,
                                depends_on_task_id
                            )
                        VALUES (?, ?)
                        """,
                        (
                            task_id,
                            depends_on_task_id,
                        ),
                    )

                    seen_dependencies.add(
                        depends_on_task_id
                    )

            return plan_id

    # ==========================================
    # READ PLAN
    # ==========================================

    def get_plan(
        self,
        plan_id: int,
    ) -> PlanRecord | None:
        scope_sql, scope_parameters = self._plan_scope()
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT
                    id,
                    version,
                    project_id,
                    canonical_source_root,
                    user_request,
                    global_goal,
                    status,
                    created_at,
                    updated_at

                FROM plans

                WHERE id = ? AND {scope_sql}
                """,
                (plan_id, *scope_parameters),
            ).fetchone()

        if row is None:
            return None

        return PlanRecord(
            id=int(
                row["id"]
            ),
            version=int(
                row["version"]
            ),
            project_id=(
                str(row["project_id"])
                if row["project_id"] is not None
                else None
            ),
            canonical_source_root=(
                str(row["canonical_source_root"])
                if row["canonical_source_root"] is not None
                else None
            ),
            user_request=str(
                row["user_request"]
            ),
            global_goal=str(
                row["global_goal"]
            ),
            status=PlanStatus(
                row["status"]
            ),
            created_at=str(
                row["created_at"]
            ),
            updated_at=str(
                row["updated_at"]
            ),
        )

    def get_active_plan(
        self,
    ) -> PlanRecord | None:
        scope_sql, scope_parameters = self._plan_scope()
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT
                    id,
                    version,
                    project_id,
                    canonical_source_root,
                    user_request,
                    global_goal,
                    status,
                    created_at,
                    updated_at

                FROM plans

                WHERE status = 'ACTIVE' AND {scope_sql}

                ORDER BY id DESC

                LIMIT 1
                """,
                scope_parameters,
            ).fetchone()

        if row is None:
            return None

        return PlanRecord(
            id=int(
                row["id"]
            ),
            version=int(
                row["version"]
            ),
            project_id=(
                str(row["project_id"])
                if row["project_id"] is not None
                else None
            ),
            canonical_source_root=(
                str(row["canonical_source_root"])
                if row["canonical_source_root"] is not None
                else None
            ),
            user_request=str(
                row["user_request"]
            ),
            global_goal=str(
                row["global_goal"]
            ),
            status=PlanStatus(
                row["status"]
            ),
            created_at=str(
                row["created_at"]
            ),
            updated_at=str(
                row["updated_at"]
            ),
        )

    # ==========================================
    # DEPENDENCIES
    # ==========================================

    def get_task_dependencies(
        self,
        task_id: int,
    ) -> list[int]:
        with self._connect() as connection:
            if not owns_task(connection, self.context, task_id):
                return []

            rows = connection.execute(
                """
                SELECT
                    depends_on_task_id

                FROM task_dependencies

                WHERE task_id = ?

                ORDER BY
                    depends_on_task_id ASC
                """,
                (task_id,),
            ).fetchall()

        return [
            int(
                row["depends_on_task_id"]
            )
            for row in rows
        ]

    # ==========================================
    # READ TASKS
    # ==========================================

    def get_tasks(
        self,
        plan_id: int,
    ):
        if self.get_plan(plan_id) is None:
            return []

        tasks = (
            self._get_tasks_without_external_dependencies(
                plan_id
            )
        )

        if not tasks:
            return tasks

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    external_dependencies_json
                FROM tasks
                WHERE plan_id = ?
                """,
                (plan_id,),
            ).fetchall()

        external_by_id = {
            int(row["id"]): self._load_list(
                row[
                    "external_dependencies_json"
                ]
            )
            for row in rows
        }

        for task in tasks:
            task.external_dependencies = (
                external_by_id.get(
                    task.id,
                    [],
                )
            )

        return tasks

    def _get_tasks_without_external_dependencies(
        self,
        plan_id: int,
    ) -> list[TaskRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    plan_id,
                    parent_id,
                    task_key,
                    title,
                    description,
                    status,
                    priority,
                    requires_json,
                    produces_json,
                    success_criteria_json,
                    current_step,
                    attempt_count,
                    result_summary,
                    result_artifacts_json,
                    verification_status,
                    verification_evidence_json,
                    created_at,
                    started_at,
                    finished_at,
                    updated_at

                FROM tasks

                WHERE plan_id = ?

                ORDER BY id ASC
                """,
                (plan_id,),
            ).fetchall()

        result: list[
            TaskRecord
        ] = []

        for row in rows:
            task_id = int(
                row["id"]
            )

            parent_id = (
                int(row["parent_id"])
                if row["parent_id"]
                is not None
                else None
            )

            current_step = (
                int(row["current_step"])
                if row["current_step"]
                is not None
                else None
            )

            result.append(
                TaskRecord(
                    id=task_id,
                    plan_id=int(
                        row["plan_id"]
                    ),
                    parent_id=parent_id,
                    title=str(
                        row["title"]
                    ),
                    description=str(
                        row["description"]
                    ),
                    status=TaskStatus(
                        row["status"]
                    ),
                    priority=int(
                        row["priority"]
                    ),
                    requires=self._load_list(
                        row["requires_json"]
                    ),
                    produces=self._load_list(
                        row["produces_json"]
                    ),
                    success_criteria=(
                        self._load_list(
                            row[
                                "success_criteria_json"
                            ]
                        )
                    ),
                    current_step=current_step,
                    attempt_count=int(
                        row["attempt_count"]
                    ),
                    result_summary=(
                        str(
                            row[
                                "result_summary"
                            ]
                        )
                        if row[
                            "result_summary"
                        ] is not None
                        else None
                    ),
                    result_artifacts=(
                        self._load_list(
                            row[
                                "result_artifacts_json"
                            ]
                        )
                    ),
                    verification_status=(
                        str(
                            row[
                                "verification_status"
                            ]
                        )
                        if row[
                            "verification_status"
                        ] is not None
                        else None
                    ),
                    verification_evidence=(
                        self._load_list(
                            row[
                                "verification_evidence_json"
                            ]
                        )
                    ),
                    created_at=str(
                        row["created_at"]
                    ),
                    started_at=(
                        str(
                            row[
                                "started_at"
                            ]
                        )
                        if row[
                            "started_at"
                        ] is not None
                        else None
                    ),
                    finished_at=(
                        str(
                            row[
                                "finished_at"
                            ]
                        )
                        if row[
                            "finished_at"
                        ] is not None
                        else None
                    ),
                    updated_at=str(
                        row["updated_at"]
                    ),
                    key=(
                        str(
                            row["task_key"]
                        )
                        if row["task_key"]
                        is not None
                        else None
                    ),
                    depends_on=(
                        self.get_task_dependencies(
                            task_id
                        )
                    ),
                )
            )

        return result

    # ==========================================
    # UPDATE TASK STATUS
    # ==========================================

    def update_task_status(
        self,
        task_id: int,
        status: TaskStatus,
    ) -> None:
        """
        Простое изменение статуса.

        Полная state-machine Task OS
        будет добавлена в Planner v0.3+.
        """

        if self.get_task(task_id) is None:
            raise ValueError(
                f"Unknown task for current project: {task_id}"
            )

        with self._connect() as connection:
            if (
                status
                is TaskStatus.IN_PROGRESS
            ):
                connection.execute(
                    """
                    UPDATE tasks

                    SET
                        status = ?,
                        started_at =
                            COALESCE(
                                started_at,
                                CURRENT_TIMESTAMP
                            ),
                        updated_at =
                            CURRENT_TIMESTAMP

                    WHERE id = ?
                    """,
                    (
                        status.value,
                        task_id,
                    ),
                )

                return

            if status in {
                TaskStatus.DONE,
                TaskStatus.FAILED,
                TaskStatus.SUPERSEDED,
            }:
                connection.execute(
                    """
                    UPDATE tasks

                    SET
                        status = ?,
                        finished_at =
                            CURRENT_TIMESTAMP,
                        updated_at =
                            CURRENT_TIMESTAMP

                    WHERE id = ?
                    """,
                    (
                        status.value,
                        task_id,
                    ),
                )

                return

            connection.execute(
                """
                UPDATE tasks

                SET
                    status = ?,
                    updated_at =
                        CURRENT_TIMESTAMP

                WHERE id = ?
                """,
                (
                    status.value,
                    task_id,
                ),
            )

    # ==========================================
    # TASK RESULT
    # ==========================================

    def set_task_result(
        self,
        task_id: int,
        *,
        result_summary: str,
        result_artifacts: list[str],
        verification_status: str,
        verification_evidence: list[str],
    ) -> None:
        if self.get_task(task_id) is None:
            raise ValueError(
                f"Unknown task for current project: {task_id}"
            )

        with self._connect() as connection:
            connection.execute(
                """
                UPDATE tasks

                SET
                    result_summary = ?,
                    result_artifacts_json = ?,
                    verification_status = ?,
                    verification_evidence_json = ?,
                    updated_at =
                        CURRENT_TIMESTAMP

                WHERE id = ?
                """,
                (
                    result_summary,
                    self._dump_list(
                        result_artifacts
                    ),
                    verification_status,
                    self._dump_list(
                        verification_evidence
                    ),
                    task_id,
                ),
            )
