# Snapshot, Path, and Command Boundaries Implementation Plan

> **Execution note:** Implement this plan task-by-task with TDD and review each
> checkpoint before continuing.

**Goal:** Build one canonical path and snapshot security boundary, prevent
model-generated commands from mutating the persistent host sandbox, and limit
every task/step to an exact framework-owned file change set.

**Design source:**
`docs/superpowers/specs/2026-09-14-persistentcoder-mvp-design.md` and the MVP
master analysis dated 2026-09-14. Document text is treated as product
requirements, never as runtime/model instructions.

**Architecture:** `ProjectPath` is the sole lexical normalizer for model and
planner paths. `ProtectedPathPolicy` and `SnapshotManifest` vet source entries
without following links, apply ignore/secret/size policy, and seed baseline and
workspace from the same immutable inventory. `AllowedChangeSet` owns exact
Task/Step write authority. Docker receives the persistent sandbox only as
read-only `/input`, copies it into a capped tmpfs `/workspace`, runs there, and
discards the container. Only `FileTools` can mutate the persistent sandbox.

**Global constraints:** fail closed; no `eval`; no host-shell fallback; no
recursive model delete; no automatic apply; no model-controlled dependency
installation; no silent compatibility path that bypasses the new boundary.

---

### Task 1: Introduce canonical `ProjectPath`

**Files:**
- Create: `app/sandbox/project_path.py`
- Create: `tests/security/test_project_path.py`
- Modify: `app/sandbox/policy.py`
- Modify: `app/tools/file_tools.py`
- Modify: `app/agent/coder/workspace.py`

**Interfaces:**
- `ProjectPath.parse(value) -> ProjectPath`
- `.value` is normalized POSIX-relative text; `.comparison_key` is
  case-insensitive on Windows; `.resolve_under(root)` verifies containment.

- [x] Write tests for `.github/workflows/ci.yml`, exact leading `./`, mixed
  separators, duplicate separators, empty/dot, `..`, POSIX absolute, drive and
  UNC paths, NUL, and Windows case comparison.
- [x] Confirm tests fail against the current scattered normalization.
- [x] Implement the immutable normalizer and route `PathPolicy`, `FileTools`,
  and `Workspace.relative` through it without weakening resolved containment.
- [x] Run `tests/security/test_project_path.py`, path-boundary, file-tool, and
  coder protocol regressions.
- [x] Commit: `feat: centralize project path validation`.

### Task 2: Build protected-path and ignore policy

**Files:**
- Create: `app/sandbox/protected_paths.py`
- Create: `app/sandbox/gitignore.py`
- Create: `tests/security/test_snapshot_policy.py`
- Modify: `app/sandbox/workspace.py`

**Interfaces:**
- `ProtectedPathPolicy.classify(ProjectPath) -> inclusion decision/reason`
- `GitIgnoreMatcher.from_project(root).is_ignored(path, is_dir)`

- [x] Write failing tests proving `.git`, caches, `.env`, `.env.*` except
  `.env.example`, private keys/certificates and credential files are excluded;
  normal target `data/` and `models/` are retained; project `.gitignore` rules
  and negations are honored; `.github` is retained.
- [x] Implement deterministic path-component/name policy with explicit reasons
  and a bounded repository-local `.gitignore` matcher (no global git config).
- [x] Remove name-only `data/models/.vscode/.idea` exclusions from target
  snapshots while retaining framework metadata/cache protections.
- [x] Run snapshot-policy and existing patch-safety tests.
- [x] Commit: `feat: define protected snapshot paths`.

### Task 3: Create a bounded `SnapshotManifest`

**Files:**
- Create: `app/sandbox/snapshot.py`
- Create: `tests/security/test_snapshot_manifest.py`
- Modify: `app/sandbox/limits.py`
- Modify: `app/sandbox/workspace.py`

**Interfaces:**
- `SnapshotEntry(path, size, sha256, source_path)`
- `SnapshotManifest.build(source_root, limits, policy) -> SnapshotManifest`
- `SnapshotManifest.materialize(destination)`

- [x] Write failing tests for normal files, single-file size, total bytes, file
  count, secret exclusion, symlink file/dir refusal, and Windows reparse-point
  refusal. Assert the scan completes and validates all entries before copying.
- [x] Add `max_snapshot_files`, `max_snapshot_bytes`, and
  `max_snapshot_file_bytes` framework limits.
- [x] Implement an `os.scandir(..., follow_symlinks=False)` walk that rejects
  every symlink/reparse entry before reading targets, hashes only regular files,
  and materializes only after the complete manifest passes.
- [x] Replace `_copy_tree` in create/reset/checkpoint paths. Baseline and
  workspace must be materialized independently from the exact same manifest;
  expose initial manifest digest in session metadata.
- [x] Run snapshot, reset, isolation, transaction, and sandbox-limit suites.
- [x] Commit: `feat: vet sandbox snapshots before copying`.

### Task 4: Make Docker command workspaces ephemeral

**Files:**
- Modify: `app/sandbox/runner.py`
- Modify: `app/sandbox/limits.py`
- Modify: `tests/security/test_command_sandbox.py`
- Modify: `tests/security/test_docker_sandbox_integration.py`
- Create: `tests/security/test_command_workspace_isolation.py`

**Interfaces:**
- Docker argv mounts `<sandbox>:/input:ro`, declares
  `--tmpfs /workspace:rw,size=<limit>,...`, starts at `/workspace`, copies
  `/input/.` into it, then executes the validated command.
- `CancellationToken.cancel()` / `.cancelled`; runner returns code 130 when
  cancelled and terminates an active container/process without host fallback.

- [x] Update argv/hardening tests first; add integration evidence that a command
  can create a file for its own test process but cannot change source or the
  persistent sandbox.
- [x] Add tmpfs-size and cancellation limits/hooks while preserving network
  none, cap-drop ALL, no-new-privileges, PIDs, memory, CPU, non-root user,
  timeout/output bounds, and docker-socket denial.
- [x] Use a fixed framework shell wrapper (`cp -a /input/. /workspace/ && ...`)
  inside the container; the model controls only the already validated command.
- [x] Run unit security tests and Docker integration tests when the daemon/image
  is available; otherwise record explicit skips, never a host fallback.
- [x] Commit: `feat: isolate command writes in ephemeral tmpfs`.

### Task 5: Add exact `AllowedChangeSet`

**Files:**
- Create: `app/tasks/change_scope.py`
- Create: `tests/test_allowed_change_set.py`
- Modify: `app/tasks/models.py`
- Modify: `app/tasks/store.py`
- Modify: `app/tasks/step_store.py`

**Interfaces:**
- `AllowedChangeSet(task_paths, step_paths)` validates canonical exact file
  ownership; Step scope must be a subset of Task scope.
- `assert_can_read`, `assert_can_write`, and `assert_can_delete` return the
  canonical `ProjectPath` or raise `ChangeScopeError`.

- [ ] Write tests for exact ownership, no same-directory exception, Step subset,
  Windows comparison keys, duplicate normalized producers, and cross-task
  ownership conflicts.
- [ ] Implement the value object and persist canonical declared Task/Step paths
  in backward-compatible nullable JSON columns. Legacy missing scope is empty,
  not unrestricted.
- [ ] Reject conflicting producers before plan persistence and reject a Step
  scope outside its Task.
- [ ] Run task model/store/step/dependency/versioning regressions.
- [ ] Commit: `feat: enforce exact task change scopes`.

### Task 6: Enforce current-attempt reads and exact delete

**Files:**
- Modify: `app/agent/coder/executor.py`
- Modify: `app/agent/coder/workspace.py`
- Modify: `app/tools/file_tools.py`
- Create: `tests/agent/test_change_scope_enforcement.py`
- Modify: `tests/agent/test_coder_protocol.py`

**Interfaces:**
- `CodeExecutor.execute(..., allowed_changes=AllowedChangeSet | None)`; runtime
  uses a non-null scope for planned work.
- Canonical file entries accept `operation: "write" | "delete"`; delete has no
  content and targets one existing regular file only.

- [ ] Write failing tests: out-of-scope edit/delete, same-directory sibling,
  existing file unread in current attempt, read from a prior attempt, safe exact
  delete, missing delete, directory delete, symlink delete, and transactional
  rollback when any entry fails.
- [ ] Replace executor-local `_normalize` authority with `ProjectPath`; observed
  reads are per-execute call and keyed by comparison key. `known_files` never
  exempts a pre-existing file from current-attempt observe-before-edit.
- [ ] Prevalidate the complete write/delete envelope and scope before mutating;
  apply through `FileTools.write_text/delete` only. Keep rollback semantics.
- [ ] Wire the active Task/Step scope from `AgentController` to `CodingAgent` and
  executor. Temporary compatibility in direct unit construction must be
  explicit and cannot be used by `AgentRuntime`.
- [ ] Run coder/controller/loop/runtime and security transaction regressions.
- [ ] Commit: `feat: constrain agent edits to declared files`.

### Task 7: Complete the Stage 2 security gate

**Files:**
- Modify: `README.md`
- Modify: this plan
- Modify only production/tests implicated by failing evidence.

- [ ] Document snapshot inclusion/exclusion, limits, exact change scopes,
  ephemeral command semantics, cancellation, and unsupported link/binary cases.
- [ ] Run `python -m compileall app tests`.
- [ ] Run the focused Stage 2 suite:

```powershell
python -m pytest tests/security/test_project_path.py tests/security/test_snapshot_policy.py tests/security/test_snapshot_manifest.py tests/security/test_project_sandbox_isolation.py tests/security/test_session_rebase_discard.py tests/security/test_command_sandbox.py tests/security/test_command_workspace_isolation.py tests/test_allowed_change_set.py tests/agent/test_change_scope_enforcement.py tests/security/test_transactions.py -q
```

- [ ] Run Docker integration tests if available and record skip/pass evidence.
- [ ] Run `python -m pytest -q -m "not real_llm"`; record every failure,
  including the acknowledged linked-worktree `.venv` infrastructure exception.
- [ ] Commit: `test: prove snapshot path and command boundaries`.

## Completion boundary

This plan completes only Stage 2. It does not complete the MVP. Next, write and
execute `2026-09-14-qwen-planner-verification-repair.md` against these stabilized
interfaces; then continue with backend v3 and VS Code/VSIX release plans.
