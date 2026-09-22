from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.agent.planner.contract_builder import ContractBuilder
from app.agent.planner.decomposer import (
    GoalAnalyzer,
    PlannerError,
    TaskDecomposer,
    build_repair_messages,
)
from app.agent.planner.dependency_builder import DependencyBuilder
from app.agent.planner.step_validator import StepValidator
from app.agent.planner.task_builder import TaskBuilder
from app.agent.coder.protocol import ActionEnvelopeDecoder
from app.agent.coder.tool_protocol import decode_tool_calls
from app.agent.repair.agent import build_debugger_messages
from app.context.builder import ACTION_PROTOCOL, ContextBuilder
from app.tasks.change_scope import AllowedChangeSet
from app.tasks.models import StepDraft, TaskDraft


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "training" / "planner-v1"


@dataclass(frozen=True)
class FileChange:
    key: str
    path: str
    purpose: str
    requires: tuple[str, ...] = ()
    test: bool = False
    verification: str | None = None


@dataclass(frozen=True)
class TrainingCase:
    name: str
    request: str
    changes: tuple[FileChange, ...]


@dataclass(frozen=True)
class CoderCase:
    name: str
    path: str
    purpose: str
    content: str
    existing: bool = False
    delete: bool = False


class CaptureLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.messages: list[dict[str, str]] = []

    def chat(
        self,
        messages: list[dict[str, str]],
        max_new_tokens: int = 512,
    ) -> str:
        del max_new_tokens
        self.messages = list(messages)
        return self.response


def _case(
    name: str,
    request: str,
    files: list[tuple[str, str, str, tuple[str, ...], bool]],
) -> TrainingCase:
    return TrainingCase(
        name=name,
        request=request,
        changes=tuple(FileChange(*item) for item in files),
    )


TRAIN_CASES = (
    _case(
        "slugify",
        "Add a typed Python slugify helper with Unicode-safe normalization.",
        [("slugify", "src/text/slugify.py", "Implement the slugify helper", (), False)],
    ),
    _case(
        "config_loader",
        "Implement a standard-library JSON configuration loader with clear errors.",
        [("config_loader", "src/config/loader.py", "Implement JSON configuration loading", (), False)],
    ),
    _case(
        "retry_policy",
        "Create a bounded retry policy and pytest coverage for backoff decisions.",
        [
            ("retry_policy", "src/runtime/retry.py", "Implement bounded retry decisions", (), False),
            ("retry_tests", "tests/test_retry.py", "Test retry and backoff behavior", ("src/runtime/retry.py",), True),
        ],
    ),
    _case(
        "csv_export",
        "Add a CSV exporter for dataclass records and focused unit tests.",
        [
            ("csv_export", "src/export/csv_writer.py", "Implement deterministic CSV export", (), False),
            ("csv_tests", "tests/test_csv_writer.py", "Test headers, quoting, and rows", ("src/export/csv_writer.py",), True),
        ],
    ),
    _case(
        "markdown_links",
        "Build a Markdown link extractor with tests for malformed and nested text.",
        [
            ("link_parser", "src/markdown/links.py", "Implement Markdown link extraction", (), False),
            ("link_tests", "tests/test_links.py", "Test valid and malformed links", ("src/markdown/links.py",), True),
        ],
    ),
    _case(
        "notes_service",
        "Add a small notes domain model, JSON repository, command service, and tests.",
        [
            ("note_model", "src/notes/model.py", "Define the typed note model", (), False),
            ("note_store", "src/notes/store.py", "Implement atomic JSON note persistence", ("src/notes/model.py",), False),
            ("note_service", "src/notes/service.py", "Implement note commands", ("src/notes/store.py",), False),
            ("note_tests", "tests/test_notes.py", "Test the notes workflow", ("src/notes/service.py",), True),
        ],
    ),
    _case(
        "weather_cache",
        "Implement a weather response cache, a client wrapper, and pytest coverage.",
        [
            ("weather_cache", "src/weather/cache.py", "Implement timestamped cache entries", (), False),
            ("weather_client", "src/weather/client.py", "Use the cache in the client wrapper", ("src/weather/cache.py",), False),
            ("weather_tests", "tests/test_weather_client.py", "Test cache hits and expiration", ("src/weather/client.py",), True),
        ],
    ),
    _case(
        "inventory_report",
        "Create an inventory model, low-stock report, and unit tests without third-party runtime packages.",
        [
            ("inventory_model", "src/inventory/model.py", "Define inventory records", (), False),
            ("stock_report", "src/inventory/report.py", "Build the low-stock report", ("src/inventory/model.py",), False),
            ("stock_tests", "tests/test_inventory_report.py", "Test stock thresholds and ordering", ("src/inventory/report.py",), True),
        ],
    ),
    _case(
        "log_parser",
        "Add a structured log parser, a summary command module, and tests.",
        [
            ("log_parser", "src/logs/parser.py", "Parse structured log lines", (), False),
            ("log_summary", "src/logs/summary.py", "Summarize parsed log records", ("src/logs/parser.py",), False),
            ("log_tests", "tests/test_log_summary.py", "Test parsing and summaries", ("src/logs/summary.py",), True),
        ],
    ),
    _case(
        "token_auth",
        "Implement signed token helpers, an authentication adapter, and tests.",
        [
            ("token_codec", "src/auth/tokens.py", "Implement token signing and validation", (), False),
            ("auth_adapter", "src/auth/adapter.py", "Use tokens in the authentication adapter", ("src/auth/tokens.py",), False),
            ("auth_tests", "tests/test_auth_adapter.py", "Test valid and invalid authentication", ("src/auth/adapter.py",), True),
        ],
    ),
    _case(
        "settings_command",
        "Add typed application settings, a command dispatcher, and command tests.",
        [
            ("settings", "src/commands/settings.py", "Define and load application settings", (), False),
            ("dispatcher", "src/commands/dispatcher.py", "Dispatch commands with settings", ("src/commands/settings.py",), False),
            ("dispatcher_tests", "tests/test_dispatcher.py", "Test command dispatch", ("src/commands/dispatcher.py",), True),
        ],
    ),
    _case(
        "sqlite_backup",
        "Create a safe SQLite backup helper and tests for successful and failed backups.",
        [
            ("sqlite_backup", "src/database/backup.py", "Implement safe SQLite backups", (), False),
            ("backup_tests", "tests/test_backup.py", "Test backup behavior", ("src/database/backup.py",), True),
        ],
    ),
    _case(
        "json_migration",
        "Implement a versioned JSON migration function and regression tests.",
        [
            ("json_migration", "src/migrations/json_v2.py", "Implement versioned JSON migration", (), False),
            ("migration_tests", "tests/test_json_v2.py", "Test old, current, and invalid records", ("src/migrations/json_v2.py",), True),
        ],
    ),
    _case(
        "plugin_registry",
        "Add a deterministic plugin registry and tests for duplicate registrations.",
        [
            ("plugin_registry", "src/plugins/registry.py", "Implement plugin registration", (), False),
            ("registry_tests", "tests/test_plugin_registry.py", "Test ordering and duplicates", ("src/plugins/registry.py",), True),
        ],
    ),
    _case(
        "rate_limiter",
        "Build an in-memory fixed-window rate limiter with deterministic tests.",
        [
            ("rate_limiter", "src/runtime/rate_limit.py", "Implement fixed-window limiting", (), False),
            ("rate_tests", "tests/test_rate_limit.py", "Test boundaries with a fake clock", ("src/runtime/rate_limit.py",), True),
        ],
    ),
    _case(
        "file_indexer",
        "Implement a file metadata indexer and tests for filtering and stable ordering.",
        [
            ("file_index", "src/index/files.py", "Index file metadata", (), False),
            ("index_tests", "tests/test_file_index.py", "Test filtering and ordering", ("src/index/files.py",), True),
        ],
    ),
    _case(
        "invoice_total",
        "Add invoice total calculations with Decimal and comprehensive unit tests.",
        [
            ("invoice_total", "src/billing/totals.py", "Calculate invoice totals", (), False),
            ("invoice_tests", "tests/test_totals.py", "Test taxes, discounts, and rounding", ("src/billing/totals.py",), True),
        ],
    ),
    _case(
        "feature_flags",
        "Create a feature flag evaluator and tests for defaults and overrides.",
        [
            ("feature_flags", "src/features/flags.py", "Evaluate feature flags", (), False),
            ("flag_tests", "tests/test_flags.py", "Test defaults and overrides", ("src/features/flags.py",), True),
        ],
    ),
    _case(
        "bookmark_cli",
        "Build a local bookmark manager with a model, atomic JSON store, service, module CLI, tests and README; remove the obsolete legacy module.",
        [
            ("bookmark_model", "src/bookmarks/model.py", "Define bookmark records", (), False),
            ("bookmark_store", "src/bookmarks/store.py", "Implement atomic bookmark persistence", ("src/bookmarks/model.py",), False),
            ("bookmark_service", "src/bookmarks/service.py", "Implement bookmark commands", ("src/bookmarks/store.py",), False),
            ("bookmark_cli", "src/bookmarks/__main__.py", "Implement the module CLI", ("src/bookmarks/service.py",), False),
            ("bookmark_tests", "tests/test_bookmarks.py", "Test the bookmark workflow", ("src/bookmarks/service.py",), True),
            ("bookmark_readme", "README.md", "Document bookmark CLI examples", ("src/bookmarks/__main__.py",), False, "FILE_EXISTS"),
            ("remove_legacy", "src/bookmarks/legacy.py", "Delete the obsolete legacy module", (), False, "FILE_ABSENT"),
        ],
    ),
    _case(
        "recipe_cli",
        "Create a standard-library recipe collection with JSON storage, a python -m recipes entry point, tests and usage documentation.",
        [
            ("recipe_model", "src/recipes/model.py", "Define recipe records", (), False),
            ("recipe_store", "src/recipes/store.py", "Persist recipes atomically", ("src/recipes/model.py",), False),
            ("recipe_service", "src/recipes/service.py", "Implement recipe operations", ("src/recipes/store.py",), False),
            ("recipe_entrypoint", "src/recipes/__main__.py", "Implement the recipe CLI entry point", ("src/recipes/service.py",), False),
            ("recipe_tests", "tests/test_recipes.py", "Test recipe operations", ("src/recipes/service.py",), True),
            ("recipe_docs", "README.md", "Add recipe CLI examples", ("src/recipes/__main__.py",), False, "FILE_EXISTS"),
        ],
    ),
    _case(
        "contacts_cli",
        "Build a local contacts manager with typed records, atomic JSON persistence, service commands, python -m contacts CLI, tests, README examples, and removal of the old module.",
        [
            ("contact_model", "src/contacts/model.py", "Define typed contact records", (), False),
            ("contact_store", "src/contacts/store.py", "Persist contacts atomically", ("src/contacts/model.py",), False),
            ("contact_service", "src/contacts/service.py", "Implement contact commands", ("src/contacts/store.py",), False),
            ("contact_cli", "src/contacts/__main__.py", "Implement the contacts CLI", ("src/contacts/service.py",), False),
            ("contact_tests", "tests/test_contacts.py", "Test the contacts workflow", ("src/contacts/service.py",), True),
            ("contact_docs", "README.md", "Document contacts CLI examples", ("src/contacts/__main__.py",), False, "FILE_EXISTS"),
            ("remove_old_contacts", "src/contacts/old.py", "Delete the old contacts module", (), False, "FILE_ABSENT"),
        ],
    ),
    _case(
        "expenses_cli",
        "Create a local expense tracker with a typed model, JSON repository, service, module CLI, tests, documentation, and obsolete-module deletion.",
        [
            ("expense_model", "src/expenses/model.py", "Define typed expense records", (), False),
            ("expense_repo", "src/expenses/repository.py", "Implement atomic expense persistence", ("src/expenses/model.py",), False),
            ("expense_service", "src/expenses/service.py", "Implement expense operations", ("src/expenses/repository.py",), False),
            ("expense_cli", "src/expenses/__main__.py", "Implement the expense CLI", ("src/expenses/service.py",), False),
            ("expense_tests", "tests/test_expenses.py", "Test expense operations", ("src/expenses/service.py",), True),
            ("expense_docs", "README.md", "Document expense CLI examples", ("src/expenses/__main__.py",), False, "FILE_EXISTS"),
            ("remove_expense_legacy", "src/expenses/legacy.py", "Delete the obsolete expense module", (), False, "FILE_ABSENT"),
        ],
    ),
    _case(
        "habits_cli",
        "Implement a standard-library habit tracker with domain records, atomic storage, service operations, python -m habits CLI, pytest tests, README usage, and legacy cleanup.",
        [
            ("habit_model", "src/habits/model.py", "Define typed habit records", (), False),
            ("habit_store", "src/habits/store.py", "Implement atomic habit storage", ("src/habits/model.py",), False),
            ("habit_service", "src/habits/service.py", "Implement habit operations", ("src/habits/store.py",), False),
            ("habit_cli", "src/habits/__main__.py", "Implement the habit CLI", ("src/habits/service.py",), False),
            ("habit_tests", "tests/test_habits.py", "Test habit operations", ("src/habits/service.py",), True),
            ("habit_docs", "README.md", "Document habit CLI usage", ("src/habits/__main__.py",), False, "FILE_EXISTS"),
            ("remove_habit_legacy", "src/habits/legacy.py", "Delete the obsolete habit module", (), False, "FILE_ABSENT"),
        ],
    ),
)


CODER_CASES = (
    CoderCase(
        "new_slugify",
        "src/text/slugify.py",
        "Implement a compact slugify helper",
        "import re\n\n\ndef slugify(value: str) -> str:\n    return re.sub(r\"[^a-z0-9]+\", \"-\", value.casefold()).strip(\"-\")\n",
    ),
    CoderCase(
        "new_model",
        "src/catalog/model.py",
        "Define a typed catalog item",
        "from dataclasses import dataclass\n\n\n@dataclass(frozen=True, slots=True)\nclass Item:\n    id: int\n    name: str\n",
    ),
    CoderCase(
        "new_repository",
        "src/catalog/repo.py",
        "Implement a JSON repository",
        "import json\nfrom pathlib import Path\n\n\nclass Repository:\n    def __init__(self, path: Path) -> None:\n        self.path = path\n\n    def load(self) -> list[dict[str, object]]:\n        if not self.path.exists():\n            return []\n        return json.loads(self.path.read_text(encoding=\"utf-8\"))\n",
    ),
    CoderCase(
        "new_service",
        "src/catalog/service.py",
        "Implement catalog service operations",
        "from .repo import Repository\n\n\nclass Service:\n    def __init__(self, repository: Repository) -> None:\n        self.repository = repository\n\n    def list_items(self) -> list[dict[str, object]]:\n        return self.repository.load()\n",
    ),
    CoderCase(
        "new_entrypoint",
        "src/catalog/__main__.py",
        "Implement the module command entry point",
        "def main() -> int:\n    print(\"catalog\")\n    return 0\n\n\nif __name__ == \"__main__\":\n    raise SystemExit(main())\n",
    ),
    CoderCase(
        "new_test",
        "tests/test_catalog.py",
        "Add focused catalog tests",
        "from catalog.model import Item\n\n\ndef test_item_keeps_fields() -> None:\n    assert Item(1, \"book\").name == \"book\"\n",
    ),
    CoderCase(
        "update_existing_model",
        "src/catalog/model.py",
        "Update an existing typed catalog item",
        "from dataclasses import dataclass\n\n\n@dataclass(frozen=True, slots=True)\nclass Item:\n    id: int\n    name: str\n    active: bool = True\n",
        existing=True,
    ),
    CoderCase(
        "update_readme",
        "README.md",
        "Add a short command example",
        "# Catalog\n\nRun with `python -m catalog`.\n",
        existing=True,
    ),
    CoderCase(
        "remove_legacy",
        "src/catalog/legacy.py",
        "Remove the obsolete legacy module",
        "",
        existing=True,
        delete=True,
    ),
)


CODER_VALIDATION_CASES = (
    CoderCase(
        "new_expiry_policy",
        "src/cache/expiry.py",
        "Implement cache expiration checks",
        "from datetime import datetime\n\n\ndef is_expired(deadline: datetime, now: datetime) -> bool:\n    return now >= deadline\n",
    ),
    CoderCase(
        "new_route_result",
        "src/router/result.py",
        "Define a typed route result",
        "from dataclasses import dataclass\n\n\n@dataclass(frozen=True, slots=True)\nclass RouteResult:\n    status: int\n    body: str\n",
    ),
    CoderCase(
        "update_usage_docs",
        "README.md",
        "Update the usage example",
        "# Router\n\nRun the focused router command.\n",
        existing=True,
    ),
    CoderCase(
        "update_existing_policy",
        "src/cache/policy.py",
        "Update an existing cache policy",
        "def should_refresh(age_seconds: int, ttl_seconds: int) -> bool:\n    return age_seconds >= ttl_seconds\n",
        existing=True,
    ),
    CoderCase(
        "remove_old_adapter",
        "src/router/old_adapter.py",
        "Delete the obsolete adapter",
        "",
        existing=True,
        delete=True,
    ),
)


VALIDATION_CASES = (
    _case(
        "email_validator",
        "Add a conservative email address validator and parameterized tests.",
        [
            ("email_validator", "src/validation/email.py", "Validate email addresses", (), False),
            ("email_tests", "tests/test_email.py", "Test accepted and rejected addresses", ("src/validation/email.py",), True),
        ],
    ),
    _case(
        "cache_pruner",
        "Implement a cache pruning policy, a pruning service, and tests.",
        [
            ("prune_policy", "src/cache/policy.py", "Choose expired cache entries", (), False),
            ("prune_service", "src/cache/prune.py", "Apply the pruning policy", ("src/cache/policy.py",), False),
            ("prune_tests", "tests/test_cache_prune.py", "Test pruning decisions", ("src/cache/prune.py",), True),
        ],
    ),
    _case(
        "command_router",
        "Build a command router with typed results and tests for unknown commands.",
        [
            ("command_router", "src/router/commands.py", "Route named commands", (), False),
            ("router_tests", "tests/test_commands.py", "Test known and unknown commands", ("src/router/commands.py",), True),
        ],
    ),
)


def _goal(case: TrainingCase) -> dict[str, object]:
    return {
        "global_goal": case.request.rstrip("."),
        "constraints": [
            "Keep changes limited to the declared project files",
            "Return verifiable implementation work",
        ],
        "assumptions": ["The existing project layout is authoritative"],
        "success_criteria": [
            "Every changed Python file compiles",
            "Focused tests pass when tests are requested",
        ],
    }


def _tasks(case: TrainingCase) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for index, change in enumerate(case.changes):
        verification = change.verification or (
            "PYTEST" if change.test else "PY_COMPILE"
        )
        result.append(
            {
                "key": change.key,
                "title": change.purpose,
                "description": f"{change.purpose} in {change.path}.",
                "priority": max(50, 90 - index * 5),
                "requires": list(change.requires),
                "produces": [] if verification == "FILE_ABSENT" else [change.path],
                "change_paths": [change.path],
                "verification_specs": [
                    {"kind": verification, "target": change.path}
                ],
                "external_dependencies": ["pytest"] if change.test else [],
                "success_criteria": [
                    (
                        f"pytest passes for {change.path}"
                        if verification == "PYTEST"
                        else (
                            f"{change.path} is absent"
                            if verification == "FILE_ABSENT"
                            else (
                                f"{change.path} exists"
                                if verification == "FILE_EXISTS"
                                else f"py_compile passes for {change.path}"
                            )
                        )
                    )
                ],
            }
        )
    return result


def _dependencies(tasks: list[dict[str, object]]) -> dict[str, list[str]]:
    producers = {
        product: str(task["key"])
        for task in tasks
        for product in task["produces"]
    }
    return {
        str(task["key"]): [producers[item] for item in task["requires"]]
        for task in tasks
    }


def _repo_context(case: TrainingCase) -> str:
    paths = "\n".join(f"- {change.path}" for change in case.changes)
    return f"RELEVANT PROJECT PATHS:\n{paths}\n- README.md"


def _capture(call: Callable[[], object], llm: CaptureLLM) -> list[dict[str, str]]:
    call()
    if not llm.messages:
        raise RuntimeError("planner stage did not call the capture LLM")
    return llm.messages


def build_records(case: TrainingCase) -> list[dict[str, object]]:
    goal = _goal(case)
    tasks = _tasks(case)
    goal_response = json.dumps(goal, ensure_ascii=False, separators=(",", ":"))
    task_response = json.dumps({"tasks": tasks}, ensure_ascii=False, separators=(",", ":"))

    goal_llm = CaptureLLM(goal_response)
    goal_messages = _capture(lambda: GoalAnalyzer(goal_llm).analyze(case.request), goal_llm)

    task_llm = CaptureLLM(task_response)
    task_messages = _capture(
        lambda: TaskDecomposer(task_llm).decompose(
            user_request=case.request,
            goal=goal,
            repo_context=_repo_context(case),
        ),
        task_llm,
    )

    builder = TaskBuilder()
    drafts = builder.build(tasks)
    steps = builder.build_steps(tasks)
    errors = StepValidator().validate_all(drafts, steps)
    if errors:
        raise RuntimeError(f"invalid generated case {case.name}: {errors}")
    ContractBuilder().validate_contracts(drafts)

    dependency_response = json.dumps(
        {"dependencies": _dependencies(tasks)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    dependency_llm = CaptureLLM(dependency_response)
    dependency_messages = _capture(
        lambda: DependencyBuilder(dependency_llm).build(
            user_request=case.request,
            goal=goal,
            tasks=drafts,
        ),
        dependency_llm,
    )

    return [
        {
            "id": f"{case.name}.goal",
            "stage": "GOAL_ANALYZER",
            "messages": goal_messages,
            "response": goal_response,
        },
        {
            "id": f"{case.name}.tasks",
            "stage": "TASK_DECOMPOSER",
            "messages": task_messages,
            "response": task_response,
        },
        {
            "id": f"{case.name}.dependencies",
            "stage": "DEPENDENCY_BUILDER",
            "messages": dependency_messages,
            "response": dependency_response,
        },
    ]


def build_coder_records(case: CoderCase) -> list[dict[str, object]]:
    criterion = f"{case.path} is absent" if case.delete else f"{case.path} exists"
    task = TaskDraft(
        key=case.name,
        title=case.purpose,
        description=f"{case.purpose} in {case.path}.",
        success_criteria=[criterion],
        change_paths=[case.path],
    )
    step = StepDraft(
        title=f"Change {case.path}",
        description=task.description,
        success_criteria=[criterion],
        change_paths=[case.path],
    )
    state = "exists and must be read before editing" if case.existing else "does not exist yet"
    project_context = f"BOUNDED REPOSITORY CONTEXT:\n- {case.path}: {state}"
    messages = ContextBuilder().build_task_messages(
        task=task,
        step=step,
        project_context=project_context,
    )
    if case.delete:
        edit = {
            "action": "edit",
            "files": [{"path": case.path, "operation": "delete"}],
            "tools": [],
        }
    else:
        tools = (
            [{"tool": "pytest", "targets": [case.path], "options": ["-q"]}]
            if case.path.startswith("tests/")
            else (
                [{"tool": "py_compile", "paths": [case.path]}]
                if case.path.endswith(".py")
                else []
            )
        )
        edit = {
            "action": "edit",
            "files": [{"path": case.path, "content": case.content}],
            "tools": tools,
        }
    edit_response = json.dumps(edit, ensure_ascii=False, separators=(",", ":"))
    allowed = AllowedChangeSet([case.path], [case.path])
    decoded_edit = ActionEnvelopeDecoder().decode(
        edit_response,
        allowed_changes=allowed,
    )
    decode_tool_calls(decoded_edit.get("tools"), allowed_changes=allowed)

    records: list[dict[str, object]] = []
    if case.existing:
        read_response = json.dumps(
            {"action": "read", "path": case.path},
            separators=(",", ":"),
        )
        records.append(
            {
                "id": f"coder.{case.name}.read",
                "stage": "CODER_ACTION",
                "messages": messages,
                "response": read_response,
            }
        )
        observation = (
            f"OBSERVATIONS:\nREAD {case.path}:\n"
            + ("legacy compatibility code\n" if case.delete else case.content)
        )
    else:
        records.append(
            {
                "id": f"coder.{case.name}.direct_edit",
                "stage": "CODER_ACTION",
                "messages": messages,
                "response": edit_response,
            }
        )
        observation = f"OBSERVATIONS:\nLIST pattern {case.path}:\n(no matches)"

        missing_read_observation = (
            f"OBSERVATIONS:\nREAD ERROR {case.path}: file not found\n\n"
            "SYSTEM FEEDBACK: this observation failed. Do not repeat the same "
            "observation. If the allowed target does not exist, create it now "
            "with an edit action containing complete file content."
        )
        records.append(
            {
                "id": f"coder.{case.name}.after_missing_read",
                "stage": "CODER_REPAIR",
                "messages": messages
                + [{"role": "user", "content": missing_read_observation}],
                "response": edit_response,
            }
        )

    records.append(
        {
            "id": f"coder.{case.name}.after_observation",
            "stage": "CODER_ACTION",
            "messages": messages + [{"role": "user", "content": observation}],
            "response": edit_response,
        }
    )
    if not case.existing and case.path.endswith(".py"):
        bad_command = (
            f"pytest -b {case.path}"
            if case.path.startswith("tests/")
            else f"py_compile -b {case.path}"
        )
        repair_messages = ContextBuilder().build_task_messages(
            task=task,
            step=step,
            project_context=project_context,
            feedback=(
                "scope: STEP\n"
                f"root_cause: command '{bad_command}' returned 127\n"
                "previous_approach: emitted an invalid executable name\n"
                "required_different_approach: use a typed tool; arbitrary command "
                "strings are forbidden"
            ),
        )
        records.append(
            {
                "id": f"coder.{case.name}.command_repair",
                "stage": "COMMAND_REPAIR",
                "messages": repair_messages,
                "response": edit_response,
            }
        )
    if not case.existing:
        records.append(
            {
                "id": f"coder.{case.name}.after_protocol_error",
                "stage": "CODER_REPAIR",
                "messages": messages
                + [
                    {
                        "role": "user",
                        "content": (
                            "SYSTEM FEEDBACK: the previous reply did not "
                            "contain a usable action.\nPREVIOUS REPLY:\n"
                            '{"path":"' + case.path + '"}\n' + ACTION_PROTOCOL
                        ),
                    }
                ],
                "response": edit_response,
            }
        )
    return records


def build_planner_repair_records(case: TrainingCase) -> list[dict[str, object]]:
    goal = _goal(case)
    tasks = _tasks(case)
    goal_response = json.dumps(goal, ensure_ascii=False, separators=(",", ":"))
    task_response = json.dumps({"tasks": tasks}, ensure_ascii=False, separators=(",", ":"))
    dependency_response = json.dumps(
        {"dependencies": _dependencies(tasks)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    context = f"USER REQUEST:\n{case.request}\n\nGOAL ANALYSIS:\n{goal_response}"
    broken_goal = json.dumps(
        {
            "global_goal": goal["global_goal"],
            "constraints": "standard library only",
            "assumptions": [],
            "success_criteria": "tests pass",
        },
        ensure_ascii=False,
    )
    broken_tasks = json.dumps(
        {"task": tasks[0]},
        ensure_ascii=False,
    )
    broken_dependencies = json.dumps(
        {"dependencies": []},
        ensure_ascii=False,
    )
    return [
        {
            "id": f"repair.{case.name}.goal_lists",
            "stage": "PLANNER_REPAIR",
            "messages": build_repair_messages(
                stage_name="GOAL_ANALYZER",
                broken_response=broken_goal,
                error=PlannerError("'constraints' must be a list"),
                repair_context=f"USER REQUEST:\n{case.request}",
            ),
            "response": goal_response,
        },
        {
            "id": f"repair.{case.name}.tasks_wrapper",
            "stage": "PLANNER_REPAIR",
            "messages": build_repair_messages(
                stage_name="TASK_DECOMPOSER",
                broken_response=broken_tasks,
                error=PlannerError("tasks must be a list"),
                repair_context=context,
            ),
            "response": task_response,
        },
        {
            "id": f"repair.{case.name}.dependency_object",
            "stage": "PLANNER_REPAIR",
            "messages": build_repair_messages(
                stage_name="DEPENDENCY_BUILDER",
                broken_response=broken_dependencies,
                error=PlannerError("dependencies must be a JSON object"),
                repair_context=json.dumps(
                    {
                        "allowed_task_keys": [task["key"] for task in tasks],
                        "tasks": tasks,
                    },
                    ensure_ascii=False,
                ),
            ),
            "response": dependency_response,
        },
    ]


def build_repair_advice_records() -> list[dict[str, object]]:
    examples = (
        (
            "compile_error",
            "Implement the configuration loader",
            "SyntaxError in src/config/loader.py line 18",
            "Read the failing file around line 18, correct only the malformed expression, then rerun py_compile for that file.",
        ),
        (
            "test_failure",
            "Implement retry backoff",
            "tests/test_retry.py::test_cap failed: expected 8, got 16",
            "Inspect the cap calculation and the focused failing test, clamp the delay before returning it, then rerun only tests/test_retry.py.",
        ),
        (
            "missing_import",
            "Implement catalog service",
            "ModuleNotFoundError for catalog.repo",
            "Check the declared package paths and imports, fix the service import to match the produced repository module, then run py_compile on the service.",
        ),
        (
            "tool_loop",
            "Create a new JSON repository module",
            "max tool iterations exceeded after repeatedly listing a missing target",
            "Treat the confirmed missing path as a new file, stop listing it, emit one complete edit action for the allowed path, and verify it with py_compile.",
        ),
        (
            "scope_rejection",
            "Update the command router",
            "action envelope rejected because README.md is outside the active change scope",
            "Restrict the change to the exact allowed command-router path and return a complete replacement only for that file.",
        ),
    )
    records: list[dict[str, object]] = []
    for name, title, failure, response in examples:
        records.append(
            {
                "id": f"repair_advice.{name}",
                "stage": "REPAIR_ADVICE",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are the Repair Agent. Reply with a short "
                            "alternative implementation approach. No code, no JSON."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"TASK:\n{title}\n\nFAILURE:\n{failure}",
                    },
                ],
                "response": response,
            }
        )
    return records


def build_debugger_records() -> list[dict[str, object]]:
    examples = (
        (
            "repeated_missing_read",
            "Implement a JSON repository",
            "REPEATED_OBSERVATION",
            "read for src/app/repository.py was repeated 8 times without producing progress",
            ["tool_loop_budget", "repeated_observation:read:src/app/repository.py:8"],
            {
                "root_cause": "The target file is absent and repeated reads cannot reveal new content.",
                "do_not_repeat": "Do not read the same absent path again.",
                "next_action": "Create src/app/repository.py with complete content and run py_compile.",
            },
        ),
        (
            "pytest_assertion",
            "Implement cache expiration checks",
            "VERIFICATION_FAIL",
            "focused pytest returned rc=1",
            ["tests/test_cache.py::test_expired expected True but got False"],
            {
                "root_cause": "The boundary comparison excludes an item exactly at its deadline.",
                "do_not_repeat": "Do not rerun the unchanged test without changing the comparison.",
                "next_action": "Inspect the deadline comparison, handle equality, then rerun only tests/test_cache.py.",
            },
        ),
        (
            "syntax_error",
            "Implement the route result model",
            "VERIFICATION_FAIL",
            "py_compile returned rc=1",
            ["SyntaxError at src/router/result.py line 8"],
            {
                "root_cause": "The generated Python file is syntactically incomplete at line 8.",
                "do_not_repeat": "Do not retry the same truncated file content.",
                "next_action": "Read the file, replace it with a complete small definition, and run py_compile once.",
            },
        ),
        (
            "blocked_dependency",
            "Run focused parser tests",
            "DEPENDENCY_BLOCKED",
            "pytest is unavailable in the prepared image",
            ["blocked_dependency", "dependency plan status BLOCKED"],
            {
                "root_cause": "The required test dependency is not present in the prepared sandbox image.",
                "do_not_repeat": "Do not retry the same command in the unchanged environment.",
                "next_action": "Report the blocked dependency and request framework-controlled environment preparation.",
            },
        ),
        (
            "scope_violation",
            "Update the command router",
            "VERIFICATION_FAIL",
            "action envelope rejected",
            ["write is outside the exact Step change scope: README.md"],
            {
                "root_cause": "The proposed edit targeted README.md outside the active step scope.",
                "do_not_repeat": "Do not include files outside the exact allowed paths.",
                "next_action": "Edit only the declared command-router file and run its focused check.",
            },
        ),
    )
    records: list[dict[str, object]] = []
    for name, title, failure_class, reason, evidence, response in examples:
        records.append(
            {
                "id": f"debugger.{name}",
                "stage": "DEBUG_DIAGNOSIS",
                "messages": build_debugger_messages(
                    task_title=title,
                    failure_class=failure_class,
                    reason=reason,
                    evidence=evidence,
                ),
                "response": json.dumps(response, ensure_ascii=False, separators=(",", ":")),
            }
        )
    return records


def _write(path: Path, records: list[dict[str, object]]) -> str:
    payload = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )
    path.write_text(payload, encoding="utf-8", newline="\n")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_stage_files(output: Path, split: str, records: list[dict[str, object]]) -> dict[str, int]:
    stage_dir = output / split
    stage_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for stage in sorted({str(record["stage"]) for record in records}):
        selected = [record for record in records if record["stage"] == stage]
        _write(stage_dir / f"{stage.casefold()}.jsonl", selected)
        counts[stage] = len(selected)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    output = args.output.resolve()
    if not output.is_relative_to(PROJECT_ROOT):
        raise SystemExit("output must stay inside the project")
    output.mkdir(parents=True, exist_ok=True)

    train = [record for case in TRAIN_CASES for record in build_records(case)]
    train.extend(record for case in CODER_CASES for record in build_coder_records(case))
    train.extend(
        record for case in TRAIN_CASES for record in build_planner_repair_records(case)
    )
    train.extend(build_repair_advice_records())
    debugger = build_debugger_records()
    train.extend(debugger[:4])
    validation = [
        record for case in VALIDATION_CASES for record in build_records(case)
    ]
    validation.extend(
        record
        for case in VALIDATION_CASES
        for record in build_planner_repair_records(case)
    )
    validation.extend(debugger[4:])
    validation.extend(
        record
        for case in CODER_VALIDATION_CASES
        for record in build_coder_records(case)
    )
    train_hash = _write(output / "train.jsonl", train)
    validation_hash = _write(output / "validation.jsonl", validation)
    recovery = [
        record
        for record in train
        if record["stage"]
        in {
            "TASK_DECOMPOSER",
            "DEPENDENCY_BUILDER",
            "CODER_ACTION",
            "CODER_REPAIR",
            "COMMAND_REPAIR",
        }
        or (
            record["stage"] == "PLANNER_REPAIR"
            and str(record["id"]).endswith("tasks_wrapper")
        )
    ]
    recovery_hash = _write(output / "recovery.jsonl", recovery)
    tool_recovery = [
        record
        for record in train
        if record["stage"] in {"CODER_ACTION", "CODER_REPAIR", "COMMAND_REPAIR"}
    ]
    tool_recovery_hash = _write(output / "tool_recovery.jsonl", tool_recovery)
    debugger_hash = _write(output / "debugger.jsonl", debugger)
    role_recovery = [*tool_recovery, *debugger]
    role_recovery_hash = _write(output / "role_recovery.jsonl", role_recovery)
    stage_counts = {
        "train": _write_stage_files(output, "train", train),
        "validation": _write_stage_files(output, "validation", validation),
    }
    manifest = {
        "version": 1,
        "generator": str(Path(__file__).relative_to(PROJECT_ROOT)),
        "train_cases": len(TRAIN_CASES),
        "validation_cases": len(VALIDATION_CASES),
        "train_records": len(train),
        "validation_records": len(validation),
        "train_sha256": train_hash,
        "validation_sha256": validation_hash,
        "recovery_records": len(recovery),
        "recovery_sha256": recovery_hash,
        "tool_recovery_records": len(tool_recovery),
        "tool_recovery_sha256": tool_recovery_hash,
        "debugger_records": len(debugger),
        "debugger_sha256": debugger_hash,
        "role_recovery_records": len(role_recovery),
        "role_recovery_sha256": role_recovery_hash,
        "coder_cases": len(CODER_CASES),
        "coder_validation_cases": len(CODER_VALIDATION_CASES),
        "stages": [
            "GOAL_ANALYZER",
            "TASK_DECOMPOSER",
            "DEPENDENCY_BUILDER",
            "CODER_ACTION",
            "CODER_REPAIR",
            "COMMAND_REPAIR",
            "PLANNER_REPAIR",
            "REPAIR_ADVICE",
            "DEBUG_DIAGNOSIS",
        ],
        "stage_counts": stage_counts,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
