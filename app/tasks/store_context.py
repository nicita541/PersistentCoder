from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


_PROJECT_ID = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class StoreContext:
    database_path: Path
    project_id: str
    canonical_source_root: str

    def __post_init__(self) -> None:
        database_path = Path(self.database_path)
        source_root = self.canonical_source_root.strip()

        if _PROJECT_ID.fullmatch(self.project_id) is None:
            raise ValueError(
                "project_id must be a lowercase SHA-256 digest"
            )

        if not source_root:
            raise ValueError("canonical_source_root must not be empty")

        object.__setattr__(self, "database_path", database_path)
        object.__setattr__(self, "canonical_source_root", source_root)


def split_store_binding(
    value: StoreContext | str | Path | None,
    *,
    default_database_path: Path,
) -> tuple[Path, StoreContext | None]:
    if isinstance(value, StoreContext):
        return value.database_path, value

    if value is None:
        return default_database_path, None

    return Path(value), None


def _plan_scope(
    context: StoreContext | None,
    alias: str,
) -> tuple[str, tuple[object, ...]]:
    if context is None:
        return f"{alias}.project_id IS NULL", ()

    return (
        f"{alias}.project_id = ? "
        f"AND {alias}.canonical_source_root = ?",
        (context.project_id, context.canonical_source_root),
    )


def owns_task(
    connection: sqlite3.Connection,
    context: StoreContext | None,
    task_id: int,
) -> bool:
    scope_sql, parameters = _plan_scope(context, "plans")
    row = connection.execute(
        f"""
        SELECT tasks.id
        FROM tasks
        JOIN plans ON plans.id = tasks.plan_id
        WHERE tasks.id = ? AND {scope_sql}
        """,
        (task_id, *parameters),
    ).fetchone()
    return row is not None


def owns_plan(
    connection: sqlite3.Connection,
    context: StoreContext | None,
    plan_id: int,
) -> bool:
    scope_sql, parameters = _plan_scope(context, "plans")
    row = connection.execute(
        f"SELECT plans.id FROM plans "
        f"WHERE plans.id = ? AND {scope_sql}",
        (plan_id, *parameters),
    ).fetchone()
    return row is not None


def owns_step(
    connection: sqlite3.Connection,
    context: StoreContext | None,
    step_id: int,
) -> bool:
    scope_sql, parameters = _plan_scope(context, "plans")
    row = connection.execute(
        f"""
        SELECT steps.id
        FROM steps
        JOIN tasks ON tasks.id = steps.task_id
        JOIN plans ON plans.id = tasks.plan_id
        WHERE steps.id = ? AND {scope_sql}
        """,
        (step_id, *parameters),
    ).fetchone()
    return row is not None
