# Project Identity and Agent Session Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Isolate every run, plan, memory query, recovery action, and sandbox session by canonical source project, then give each VS Code chat an explicit durable sandbox lifecycle.

**Architecture:** A pure `ProjectIdentity` and `ProjectStorage` layer derives stable project-owned paths before any store or sandbox opens. Existing SQLite stores persist and filter `project_id`; `AgentSession` owns the state machine and sandbox baseline while `AgentRuntime` remains the composition root. Legacy unscoped records stay intact but are never selected for an external project.

**Tech Stack:** Python 3.12, dataclasses/enums, pathlib, hashlib, SQLite, pytest

**Spec:** `docs/superpowers/specs/2026-09-14-persistentcoder-mvp-design.md`

## Global Constraints

- PersistentCoder-owned project state lives only below `F:\PersistentCoder\data\projects\<project_id>` and `F:\PersistentCoder\.sandbox\projects\<project_id>`.
- Target projects receive no database, cache, sandbox, or `.persistentcoder` files.
- Windows root identity comparison is case-insensitive; display paths preserve their resolved spelling.
- `project_id` is the lowercase SHA-256 hex digest of the canonical comparison root encoded as UTF-8.
- Recovery and active-plan selection fail closed when either `project_id` or canonical source root differs.
- Legacy unscoped rows remain in place and are excluded from external-project prompts and recovery.
- Global memory contains only explicitly global `USER_RULE` records; all default memory writes are project-scoped.
- The LLM never chooses identity, storage location, session state, or recovery eligibility.
- Existing public behavior remains compatible only where it cannot bypass project isolation.

---

## File map

- `app/project_identity.py`: canonical root derivation and immutable Project Identity.
- `app/storage.py`: framework-owned global/project database and sandbox paths.
- `app/tasks/store_context.py`: validated store binding shared by SQLite stores.
- `app/agent/session.py`: session states, transitions, and durable session record.
- `app/tasks/session_store.py`: SQLite persistence for AgentSession.
- `app/sandbox/paths.py`: static framework roots plus project namespace helpers.
- `app/sandbox/workspace.py`: project-bound sandbox creation/open/rebase/discard.
- `app/tasks/runtime_store.py`: project-bound run/event persistence and recovery.
- `app/tasks/store.py`: project-bound plans and active-plan lookup.
- `app/memory/store.py`: scoped memory schema and selection.
- `app/memory/manager.py`: explicit global-rule and current-project retrieval/write policy.
- `app/agent/runtime.py`: identity-first composition, session lifecycle, and recovery wiring.

### Task 1: Canonical Project Identity and storage layout

**Files:**
- Create: `app/project_identity.py`
- Create: `app/storage.py`
- Modify: `app/sandbox/paths.py`
- Test: `tests/test_project_identity.py`

**Interfaces:**
- Consumes: framework `PROJECT_ROOT`, `DATA_ROOT`, and `SANDBOX_ROOT` constants.
- Produces: `ProjectIdentity.from_source_root(source_root: str | Path) -> ProjectIdentity`; `ProjectStorage.for_identity(identity: ProjectIdentity) -> ProjectStorage`; `ProjectStorage.ensure_layout() -> None`.

- [x] **Step 1: Write failing identity and containment tests**

```python
def test_project_identity_is_stable_for_equivalent_roots(tmp_path):
    project = tmp_path / "Project"
    project.mkdir()
    first = ProjectIdentity.from_source_root(project)
    second = ProjectIdentity.from_source_root(project / ".")
    assert first.project_id == second.project_id
    assert first.canonical_source_root == project.resolve()
    assert len(first.project_id) == 64


def test_project_storage_never_writes_under_target(tmp_path):
    project = tmp_path / "target"
    project.mkdir()
    identity = ProjectIdentity.from_source_root(project)
    storage = ProjectStorage.for_identity(identity)
    storage.ensure_layout()
    assert PROJECT_ROOT in storage.database_path.parents
    assert project not in storage.database_path.parents
    assert storage.database_path == DATA_ROOT / "projects" / identity.project_id / "persistent_coder.db"
```

- [x] **Step 2: Run the tests and confirm the missing-module failure**

Run: `python -m pytest tests/test_project_identity.py -q`

Expected: FAIL because `app.project_identity` and `app.storage` do not exist.

- [x] **Step 3: Implement pure identity and storage values**

```python
@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    canonical_source_root: Path
    comparison_root: str
    project_id: str

    @classmethod
    def from_source_root(cls, source_root: str | Path) -> "ProjectIdentity":
        root = Path(source_root).resolve(strict=True)
        if not root.is_dir():
            raise ProjectIdentityError(f"Project root is not a directory: {root}")
        comparison = os.path.normcase(str(root)) if os.name == "nt" else str(root)
        project_id = hashlib.sha256(comparison.encode("utf-8")).hexdigest()
        return cls(root, comparison, project_id)


@dataclass(frozen=True, slots=True)
class ProjectStorage:
    identity: ProjectIdentity
    database_path: Path
    sandbox_root: Path

    @classmethod
    def for_identity(cls, identity: ProjectIdentity) -> "ProjectStorage":
        return cls(
            identity=identity,
            database_path=DATA_ROOT / "projects" / identity.project_id / "persistent_coder.db",
            sandbox_root=SANDBOX_ROOT / "projects" / identity.project_id,
        )
```

`ensure_layout()` must create only the database parent and the project sandbox subdirectories `sessions`, `snapshots`, `patches`, `logs`, and `tmp`. Add explicit `is_relative_to(PROJECT_ROOT)` guards before creating them.

- [x] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_project_identity.py -q`

Expected: PASS.

- [x] **Step 5: Commit**

```powershell
git add app/project_identity.py app/storage.py app/sandbox/paths.py tests/test_project_identity.py
git commit -m "feat: add canonical project identity"
```

### Task 2: Bind SQLite stores to a project

**Files:**
- Create: `app/tasks/store_context.py`
- Modify: `app/tasks/runtime_store.py`
- Modify: `app/tasks/store.py`
- Modify: `app/tasks/attempt_store.py`
- Modify: `app/tasks/step_store.py`
- Modify: `app/tasks/replan_store.py`
- Modify: `app/tasks/verification_store.py`
- Modify: `app/tasks/models.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_project_store_isolation.py`

**Interfaces:**
- Consumes: `ProjectIdentity.project_id`, `ProjectStorage.database_path`.
- Produces: `StoreContext(database_path: Path, project_id: str, canonical_source_root: str)`; all store constructors accept `context: StoreContext`; run and plan records expose `project_id`.

- [x] **Step 1: Write failing store-binding tests**

```python
def test_runtime_store_recovers_only_matching_project(tmp_path):
    database = tmp_path / "shared.db"
    a = StoreContext(database, "a" * 64, str(tmp_path / "a"))
    b = StoreContext(database, "b" * 64, str(tmp_path / "b"))
    run_id = RuntimeStore(a).start_run(request="x", sandbox_session_id="session-a")
    assert RuntimeStore(b).recover_interrupted() == []
    assert RuntimeStore(a).get_run(run_id)["project_id"] == "a" * 64


def test_active_plan_is_project_scoped(tmp_path):
    database = tmp_path / "shared.db"
    store_a = PlanStore(StoreContext(database, "a" * 64, str(tmp_path / "a")))
    store_b = PlanStore(StoreContext(database, "b" * 64, str(tmp_path / "b")))
    plan_id = store_a.create_plan(
        PlanDraft(
            user_request="A",
            global_goal="A",
            tasks=[
                TaskDraft(
                    key="write-a",
                    title="Write A",
                    description="Create a.txt",
                    produces=["a.txt"],
                    success_criteria=["a.txt exists"],
                )
            ],
        )
    )
    assert store_a.get_active_plan().id == plan_id
    assert store_b.get_active_plan() is None
```

- [x] **Step 2: Run the focused tests and confirm constructor/schema failures**

Run: `python -m pytest tests/test_project_store_isolation.py -q`

Expected: FAIL because `StoreContext` and project columns do not exist.

- [x] **Step 3: Add idempotent schema migrations and project filters**

```python
@dataclass(frozen=True, slots=True)
class StoreContext:
    database_path: Path
    project_id: str
    canonical_source_root: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.project_id):
            raise ValueError("project_id must be a lowercase SHA-256 digest")
```

For `agent_runs` and `plans`, add nullable legacy-safe columns:

```sql
ALTER TABLE agent_runs ADD COLUMN project_id TEXT;
ALTER TABLE agent_runs ADD COLUMN canonical_source_root TEXT;
ALTER TABLE plans ADD COLUMN project_id TEXT;
```

New inserts always populate the binding. `get_active_plan`, `get_running_runs`, `recover_interrupted`, event queries, and every lookup used by runtime recovery add `WHERE project_id = ?`. Rows with `NULL project_id` remain untouched and cannot match. Child task/step/attempt/replan/verification rows remain transitively owned by a bound plan or run, and their public lookup methods must validate the parent plan binding before returning data.

- [x] **Step 4: Update test fixtures and run store regressions**

Run: `python -m pytest tests/test_project_store_isolation.py tests/test_task_store.py tests/test_task_attempts.py tests/test_task_steps.py tests/test_plan_versioning.py tests/security/test_crash_recovery.py -q`

Expected: PASS, including migration of old nullable schemas without assigning legacy rows to the current project.

- [x] **Step 5: Commit**

```powershell
git add app/tasks tests/conftest.py tests/test_project_store_isolation.py tests/security/test_crash_recovery.py
git commit -m "feat: scope task state by project"
```

### Task 3: Split global and project memory scopes

**Files:**
- Modify: `app/memory/store.py`
- Modify: `app/memory/manager.py`
- Modify: `app/memory/retrieval.py`
- Modify: `app/context/builder.py`
- Test: `tests/test_memory_project_scope.py`
- Test: `tests/test_memory_store.py`
- Test: `tests/test_memory_retrieval.py`

**Interfaces:**
- Consumes: project `StoreContext`; `DATA_ROOT / "global" / "persistent_coder.db"`.
- Produces: `MemoryScope.GLOBAL`, `MemoryScope.PROJECT`; `MemoryStore(database_path: Path, *, scope: MemoryScope, project_id: str | None = None)`; `MemoryManager.get_active_memories() -> list[dict[str, object]]` returns explicit global rules plus only the current project's records; `remember(..., global_rule: bool = False)`.

- [ ] **Step 1: Write failing scope and migration tests**

```python
def memory_manager(project_db: Path, project_id: str, global_db: Path) -> MemoryManager:
    return MemoryManager(
        project_store=MemoryStore(
            project_db,
            scope=MemoryScope.PROJECT,
            project_id=project_id,
        ),
        global_store=MemoryStore(
            global_db,
            scope=MemoryScope.GLOBAL,
        ),
    )


def test_project_memory_never_crosses_project_boundary(tmp_path):
    global_db = tmp_path / "global.db"
    a = memory_manager(tmp_path / "a.db", "a" * 64, global_db)
    b = memory_manager(tmp_path / "b.db", "b" * 64, global_db)
    a.remember("В проекте используется SQLite.")
    assert any("SQLite" in row["content"] for row in a.get_active_memories())
    assert all("SQLite" not in row["content"] for row in b.get_active_memories())


def test_only_explicit_user_rule_becomes_global(tmp_path):
    a = memory_manager(tmp_path / "a.db", "a" * 64, tmp_path / "global.db")
    b = memory_manager(tmp_path / "b.db", "b" * 64, tmp_path / "global.db")
    a.remember("Всегда запускай тесты.", global_rule=True)
    assert any(row["type"] == "USER_RULE" for row in b.get_active_memories())
    with pytest.raises(MemoryScopeError):
        a.remember("В проекте используется SQLite.", global_rule=True)
```

- [ ] **Step 2: Run the tests and confirm missing scope failures**

Run: `python -m pytest tests/test_memory_project_scope.py -q`

Expected: FAIL because global/project stores are not separated.

- [ ] **Step 3: Implement explicit memory routing**

```python
class MemoryScope(str, Enum):
    GLOBAL = "GLOBAL"
    PROJECT = "PROJECT"


class MemoryManager:
    def __init__(self, project_store: MemoryStore, global_store: MemoryStore) -> None:
        self.project_store = project_store
        self.global_store = global_store

    def get_active_memories(self) -> list[dict[str, object]]:
        global_rules = self.global_store.get_active_memories(memory_type="USER_RULE")
        project_rows = self.project_store.get_active_memories()
        return [*global_rules, *project_rows]
```

Add `scope` and nullable `project_id` columns idempotently. A global insert requires `memory_type == "USER_RULE"`, `scope == "GLOBAL"`, and `project_id IS NULL`; a project insert requires the manager's project ID. Old rows retain `scope = NULL` and are excluded from new retrieval. Duplicate/supersede/merge queries operate within one scope only.

- [ ] **Step 4: Run memory and context regressions**

Run: `python -m pytest tests/test_memory_project_scope.py tests/test_memory_store.py tests/test_memory_manager.py tests/test_memory_retrieval.py tests/test_memory_merge.py tests/test_memory_conflicts.py tests/test_context_selector.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add app/memory app/context/builder.py tests/test_memory_project_scope.py tests/test_memory_store.py tests/test_memory_retrieval.py
git commit -m "feat: isolate project memory"
```

### Task 4: Namespace sandbox sessions by Project Identity

**Files:**
- Modify: `app/sandbox/workspace.py`
- Modify: `app/sandbox/paths.py`
- Test: `tests/security/test_project_sandbox_isolation.py`
- Modify: `tests/security/test_crash_recovery.py`

**Interfaces:**
- Consumes: `ProjectIdentity`, `ProjectStorage.sandbox_root`.
- Produces: `SandboxWorkspace.create(project_root, identity, storage, session_id=None)`; `SandboxWorkspace.open_session(identity, storage, session_id)`; workspace exposes `project_id` and canonical `source_project_root`.

- [ ] **Step 1: Write failing namespace and mismatch tests**

```python
def project_case(tmp_path: Path, name: str):
    root = tmp_path / name
    root.mkdir()
    (root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    identity = ProjectIdentity.from_source_root(root)
    storage = ProjectStorage.for_identity(identity)
    storage.ensure_layout()
    return SimpleNamespace(root=root, identity=identity, storage=storage)


def test_same_session_id_cannot_cross_project_namespace(tmp_path):
    project_a = project_case(tmp_path, "a")
    project_b = project_case(tmp_path, "b")
    workspace_a = SandboxWorkspace.create(
        project_root=project_a.root,
        identity=project_a.identity,
        storage=project_a.storage,
        session_id="fixed",
    )
    assert SandboxWorkspace.open_session(
        project_b.identity, project_b.storage, "fixed"
    ) is None
    assert project_a.identity.project_id in str(workspace_a.workspace_root)


def test_open_session_rejects_tampered_source_identity(tmp_path):
    project_a = project_case(tmp_path, "a")
    project_b = project_case(tmp_path, "b")
    workspace = SandboxWorkspace.create(
        project_root=project_a.root,
        identity=project_a.identity,
        storage=project_a.storage,
    )
    workspace.metadata_path.write_text(
        json.dumps({"project_id": project_b.identity.project_id}), encoding="utf-8"
    )
    with pytest.raises(SandboxIdentityError):
        SandboxWorkspace.open_session(
            project_a.identity, project_a.storage, workspace.session_id
        )
```

- [ ] **Step 2: Run the tests and confirm current global-path behavior fails**

Run: `python -m pytest tests/security/test_project_sandbox_isolation.py -q`

Expected: FAIL because sandbox sessions currently live in global `sessions` and `snapshots` directories without identity metadata.

- [ ] **Step 3: Add project-rooted paths and identity metadata**

Store a UTF-8 JSON metadata file beside each session workspace:

```json
{
  "schema_version": 1,
  "session_id": "...",
  "project_id": "...",
  "canonical_source_root": "..."
}
```

`open_session` must resolve the project namespace first, validate `session_id` with `_safe_label`, load metadata, compare both ID and canonical root, and fail before reading a checkpoint on mismatch. Do not search other project namespaces for a matching session ID.

- [ ] **Step 4: Run sandbox and crash-recovery tests**

Run: `python -m pytest tests/security/test_project_sandbox_isolation.py tests/security/test_crash_recovery.py tests/security/test_transactions.py tests/security/test_sandbox_limits_and_patches.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add app/sandbox tests/security/test_project_sandbox_isolation.py tests/security/test_crash_recovery.py
git commit -m "feat: namespace sandbox sessions by project"
```

### Task 5: Persist the AgentSession state machine

**Files:**
- Create: `app/agent/session.py`
- Create: `app/tasks/session_store.py`
- Modify: `app/tasks/models.py`
- Test: `tests/agent/test_agent_session.py`
- Test: `tests/test_session_store.py`

**Interfaces:**
- Consumes: `StoreContext`, `SandboxWorkspace.session_id`.
- Produces: `SessionStatus`; `AgentSession`; `AgentSession.transition(target, *, patch_manifest_id=None)`; `SessionStore.create/get/update/list_dirty`.

- [ ] **Step 1: Write failing transition and persistence tests**

```python
def make_session(status: SessionStatus = SessionStatus.CLEAN) -> AgentSession:
    return AgentSession(
        id="session-record",
        project_id="a" * 64,
        canonical_source_root="C:/project-a",
        sandbox_session_id="sandbox-a",
        status=status,
        active_run_id=None,
        patch_manifest_id=None,
        version=0,
    )


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (SessionStatus.CLEAN, SessionStatus.RUNNING),
        (SessionStatus.RUNNING, SessionStatus.DIRTY_VERIFIED),
        (SessionStatus.RUNNING, SessionStatus.DIRTY_FAILED),
        (SessionStatus.DIRTY_VERIFIED, SessionStatus.APPLIED),
        (SessionStatus.DIRTY_VERIFIED, SessionStatus.DISCARDED),
        (SessionStatus.DIRTY_FAILED, SessionStatus.DISCARDED),
    ],
)
def test_allowed_session_transitions(source, target):
    session = make_session(source)
    session.transition(target)
    assert session.status is target


def test_session_store_is_project_scoped(tmp_path):
    database = tmp_path / "shared.db"
    context_a = StoreContext(database, "a" * 64, str(tmp_path / "a"))
    context_b = StoreContext(database, "b" * 64, str(tmp_path / "b"))
    session_id = SessionStore(context_a).create(sandbox_session_id="s")
    assert SessionStore(context_a).get(session_id) is not None
    assert SessionStore(context_b).get(session_id) is None
```

- [ ] **Step 2: Run tests and confirm missing session types**

Run: `python -m pytest tests/agent/test_agent_session.py tests/test_session_store.py -q`

Expected: FAIL because the session state machine and table do not exist.

- [ ] **Step 3: Implement deterministic transitions and idempotent table creation**

```python
class SessionStatus(str, Enum):
    CLEAN = "CLEAN"
    RUNNING = "RUNNING"
    DIRTY_VERIFIED = "DIRTY_VERIFIED"
    DIRTY_FAILED = "DIRTY_FAILED"
    APPLIED = "APPLIED"
    DISCARDED = "DISCARDED"


ALLOWED_TRANSITIONS = {
    SessionStatus.CLEAN: {SessionStatus.RUNNING, SessionStatus.DISCARDED},
    SessionStatus.RUNNING: {
        SessionStatus.DIRTY_VERIFIED,
        SessionStatus.DIRTY_FAILED,
        SessionStatus.CLEAN,
    },
    SessionStatus.DIRTY_VERIFIED: {
        SessionStatus.APPLIED,
        SessionStatus.DISCARDED,
    },
    SessionStatus.DIRTY_FAILED: {SessionStatus.DISCARDED},
    SessionStatus.APPLIED: {SessionStatus.CLEAN},
    SessionStatus.DISCARDED: {SessionStatus.CLEAN},
}
```

The `agent_sessions` table stores `id`, `project_id`, `canonical_source_root`, `sandbox_session_id`, `status`, nullable `active_run_id`, nullable `patch_manifest_id`, timestamps, and a monotonically increasing `version`. Updates use `WHERE id = ? AND project_id = ? AND version = ?` so stale writers fail closed.

- [ ] **Step 4: Run session tests**

Run: `python -m pytest tests/agent/test_agent_session.py tests/test_session_store.py -q`

Expected: PASS, including illegal-transition and stale-version negative tests.

- [ ] **Step 5: Commit**

```powershell
git add app/agent/session.py app/tasks/session_store.py app/tasks/models.py tests/agent/test_agent_session.py tests/test_session_store.py
git commit -m "feat: persist agent session lifecycle"
```

### Task 6: Add clean rebase and discard primitives

**Files:**
- Modify: `app/sandbox/workspace.py`
- Test: `tests/security/test_session_rebase_discard.py`

**Interfaces:**
- Consumes: project-bound `SandboxWorkspace`.
- Produces: `SandboxWorkspace.rebase_from_source() -> None`; `SandboxWorkspace.discard_and_recreate() -> None`; both preserve identity and session ID while replacing baseline/workspace atomically.

- [ ] **Step 1: Write failing rebase/discard tests**

```python
def tree_digest(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_rebase_uses_current_source_for_both_trees(workspace):
    (workspace.project_root / "value.txt").write_text("source-v2", encoding="utf-8")
    (workspace.workspace_root / "value.txt").write_text("dirty", encoding="utf-8")
    workspace.rebase_from_source()
    assert (workspace.baseline_root / "value.txt").read_text() == "source-v2"
    assert (workspace.workspace_root / "value.txt").read_text() == "source-v2"
    assert workspace.changed_files() == []


def test_failed_rebase_restores_previous_baseline_and_workspace(workspace, monkeypatch):
    before = tree_digest(workspace.baseline_root), tree_digest(workspace.workspace_root)
    real_replace = workspace._replace_tree
    calls = 0

    def fail_on_second_replace(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected promotion failure")
        real_replace(source, destination)

    monkeypatch.setattr(workspace, "_replace_tree", fail_on_second_replace)
    with pytest.raises(SandboxResetError):
        workspace.rebase_from_source()
    assert (tree_digest(workspace.baseline_root), tree_digest(workspace.workspace_root)) == before
```

- [ ] **Step 2: Run tests and confirm methods are absent**

Run: `python -m pytest tests/security/test_session_rebase_discard.py -q`

Expected: FAIL with missing reset methods.

- [ ] **Step 3: Implement staged replacement**

Build the replacement baseline and workspace under the same project sandbox filesystem using unique temporary directory names. Only after both copies succeed, rename the current trees to backups and promote both replacements. On any promotion failure, restore both backups. Remove backups after success. `discard_and_recreate()` delegates to the same transaction because both operations establish source as the new clean baseline; the session layer distinguishes their semantic outcome.

- [ ] **Step 4: Run workspace regressions**

Run: `python -m pytest tests/security/test_session_rebase_discard.py tests/security/test_transactions.py tests/security/test_sandbox_limits_and_patches.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add app/sandbox/workspace.py tests/security/test_session_rebase_discard.py
git commit -m "feat: reset sandbox sessions atomically"
```

### Task 7: Compose identity and session lifecycle in AgentRuntime

**Files:**
- Modify: `app/agent/runtime.py`
- Modify: `app/agent/controller.py`
- Modify: `app/main.py`
- Test: `tests/agent/test_runtime_project_session.py`
- Modify: `tests/agent/test_runtime.py`
- Modify: `tests/agent/test_controller.py`

**Interfaces:**
- Consumes: all interfaces from Tasks 1-6.
- Produces: `AgentRuntime.project_identity`; `AgentRuntime.project_storage`; `AgentRuntime.session`; `new_session(discard_dirty: bool = False)`, `discard_session()`, `rebase_session_after_apply()`; runtime run transitions.

- [ ] **Step 1: Write failing composition tests**

```python
def test_runtime_derives_storage_before_opening_stores(tmp_path, fake_llm):
    project = tmp_path / "project"
    project.mkdir()
    runtime = AgentRuntime(project_root=project, llm=fake_llm)
    assert runtime.project_identity.canonical_source_root == project.resolve()
    assert runtime.runtime_store.database_path == runtime.project_storage.database_path
    assert runtime.session.project_id == runtime.project_identity.project_id


def test_new_session_requires_explicit_dirty_resolution(tmp_path, fake_llm):
    project = tmp_path / "project"
    project.mkdir()
    runtime = AgentRuntime(project_root=project, llm=fake_llm)
    runtime.session.status = SessionStatus.DIRTY_VERIFIED
    with pytest.raises(DirtySessionError):
        runtime.new_session()
    old_id = runtime.session.id
    runtime.new_session(discard_dirty=True)
    assert runtime.session.id != old_id
    assert runtime.session.status is SessionStatus.CLEAN
```

- [ ] **Step 2: Run tests and confirm current identity/session failures**

Run: `python -m pytest tests/agent/test_runtime_project_session.py -q`

Expected: FAIL because stores and sandbox are opened before any project/session binding.

- [ ] **Step 3: Reorder runtime composition and wire transitions**

Runtime initialization order must be:

```python
self.project_identity = ProjectIdentity.from_source_root(project_root)
self.project_storage = ProjectStorage.for_identity(self.project_identity)
self.project_storage.ensure_layout()
self.store_context = StoreContext(
    self.project_storage.database_path,
    self.project_identity.project_id,
    str(self.project_identity.canonical_source_root),
)
self.runtime_store = RuntimeStore(self.store_context)
self.session_store = SessionStore(self.store_context)
```

Only after matching interrupted runs are recovered may runtime open or create the project's sandbox and session. `run()` atomically claims a CLEAN session as RUNNING, then transitions to `DIRTY_VERIFIED` on verified sandbox changes, `CLEAN` on verified no-op, or `DIRTY_FAILED` on failure/cancellation with retained reviewable changes. Direct apply transitions through `APPLIED` and then rebases to a new CLEAN session state. Do not let event persistence exceptions alter transitions.

- [ ] **Step 4: Run runtime/controller regressions**

Run: `python -m pytest tests/agent/test_runtime_project_session.py tests/agent/test_runtime.py tests/agent/test_controller.py tests/agent/test_agent_loop.py tests/test_cli_commands.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add app/agent/runtime.py app/agent/controller.py app/main.py tests/agent/test_runtime_project_session.py tests/agent/test_runtime.py tests/agent/test_controller.py
git commit -m "feat: integrate project sessions into runtime"
```

### Task 8: Prove cross-project recovery and legacy fail-closed behavior

**Files:**
- Create: `tests/security/test_cross_project_runtime_isolation.py`
- Create: `tests/helpers/legacy_sqlite.py`
- Modify: `tests/security/test_crash_recovery.py`
- Modify: `tests/test_memory_store.py`
- Modify: `tests/test_task_store.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: completed Stage 1 public interfaces.
- Produces: executable security evidence and user-facing storage/recovery documentation.

- [ ] **Step 1: Write end-to-end negative-security tests**

```python
from tests.helpers.legacy_sqlite import seed_legacy_memory, seed_legacy_run


def test_project_b_never_resumes_project_a(tmp_path, fake_llm):
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    crashed = AgentRuntime(project_root=project_a, llm=fake_llm)
    run_id = crashed.runtime_store.start_run(
        request="A", sandbox_session_id=crashed.session.sandbox_session_id
    )
    restarted_b = AgentRuntime(project_root=project_b, llm=fake_llm)
    assert restarted_b.resumed_run_id is None
    assert restarted_b.session.project_id != crashed.session.project_id
    assert restarted_b.runtime_store.get_run(run_id) is None


def test_legacy_unscoped_rows_are_not_adopted(tmp_path, fake_llm):
    external_project = tmp_path / "external"
    external_project.mkdir()
    legacy_database = tmp_path / "legacy.db"
    seed_legacy_run(legacy_database, "legacy", "legacy-session")
    seed_legacy_memory(legacy_database, "FACT", "Legacy project fact")
    runtime = AgentRuntime(
        project_root=external_project,
        database_path=legacy_database,
        llm=fake_llm,
    )
    assert runtime.interrupted_runs == []
    assert runtime.memory.get_active_memories() == []
```

Implement the test-only legacy seeds in `tests/helpers/legacy_sqlite.py`; keep this SQL outside production code:

```python
def seed_legacy_run(database: Path, request: str, sandbox_session_id: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'RUNNING',
                phase TEXT, plan_id INTEGER, task_id INTEGER,
                step_id INTEGER, attempt_id INTEGER,
                sandbox_session_id TEXT, checkpoint_id TEXT,
                recovery_note TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        connection.execute(
            "INSERT INTO agent_runs (request, status, sandbox_session_id) VALUES (?, 'RUNNING', ?)",
            (request, sandbox_session_id),
        )


def seed_legacy_memory(database: Path, memory_type: str, content: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL, content TEXT NOT NULL, why TEXT,
                source TEXT NOT NULL, importance INTEGER NOT NULL DEFAULT 50,
                confidence REAL NOT NULL DEFAULT 1.0,
                status TEXT NOT NULL DEFAULT 'ACTIVE', superseded_by INTEGER,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        connection.execute(
            "INSERT INTO memories (type, content, source) VALUES (?, ?, 'USER')",
            (memory_type, content),
        )
```

- [ ] **Step 2: Run the security file and confirm any remaining leaks**

Run: `python -m pytest tests/security/test_cross_project_runtime_isolation.py -q`

Expected before final fixes: at least one FAIL if a store, recovery path, or memory query still bypasses project binding.

- [ ] **Step 3: Close the specific binding gaps and document behavior**

For each failing assertion, route the operation through `StoreContext` or project-scoped workspace lookup. Do not add permissive fallback queries. Document the exact `data/projects/<project_id>` and `.sandbox/projects/<project_id>` locations, global-rule behavior, dirty-session states, and legacy exclusion policy in README.

- [ ] **Step 4: Run the complete Stage 1 gate**

Run:

```powershell
python -m compileall app tests
python -m pytest tests/test_project_identity.py tests/test_project_store_isolation.py tests/test_memory_project_scope.py tests/test_session_store.py tests/agent/test_agent_session.py tests/agent/test_runtime_project_session.py tests/security/test_project_sandbox_isolation.py tests/security/test_session_rebase_discard.py tests/security/test_cross_project_runtime_isolation.py tests/security/test_crash_recovery.py -q
```

Expected: compilation succeeds and every listed test passes. Then run `python -m pytest -q -m "not real_llm"` once as the Stage 1 aggregate regression and record all failures before starting Stage 2.

- [ ] **Step 5: Commit**

```powershell
git add app tests README.md
git commit -m "test: prove project and session isolation"
```

## Subsequent plan sequence

After this plan passes its aggregate regression, write and execute these plans in order against the stabilized interfaces:

1. `2026-09-14-snapshot-path-command-boundaries.md` — SnapshotManifest, secret/link/size policy, ProjectPath, AllowedChangeSet, exact Delete, and ephemeral Docker command workspace.
2. `2026-09-14-qwen-planner-verification-repair.md` — ActionEnvelopeDecoder, bounded context, deterministic StepBuilder, VerificationSpec, dependency blocking, ApproachFingerprint, and structural task replan.
3. `2026-09-14-patch-apply-backend-v3.md` — durable state corrections, bounded sanitized events, PatchManifest, transactional ApplyService, and responsive protocol v3.
4. `2026-09-14-vscode-vsix-release.md` — complete VS Code UX, configuration, packaging, CI/docs/hygiene, full regressions, real Docker/Qwen acceptance, installed VSIX smoke, and final evidence report.

Each subsequent plan must cite the same design spec, preserve the Global Constraints above, use TDD, and end with its stated real acceptance gate. The full MVP goal remains open until all four subsequent plans and every release gate are complete.
