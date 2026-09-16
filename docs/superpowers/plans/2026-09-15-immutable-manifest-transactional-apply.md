# Immutable Manifest and Transactional Apply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace live-workspace copying with one durable, immutable `PatchManifest` consumed transactionally by manual Sandbox Apply and Direct auto-apply.

**Architecture:** A framework-owned builder compares the immutable session baseline with the verified workspace and persists a content-addressed manifest plus ordered entries. `ApplyService` consumes that exact manifest, validates every source and destination before the first write, journals filesystem intent, applies through same-volume staging and backups, and rolls back every touched path on failure. Runtime preview, manual Apply, and backend auto-apply all use this service; `SandboxWorkspace.apply_to_project()` is removed from the production path.

**Tech Stack:** Python 3.12, dataclasses/enums, SHA-256, `pathlib`, SQLite additive migrations, pytest.

**Spec:** `docs/superpowers/specs/2026-09-14-persistentcoder-mvp-design.md` (Stage 4 patch/apply slice, delivered as current architecture Stage 3).

## Global Constraints

- Work only in `F:\PersistentCoder` on `master`; commit and push `master` after the completed stage.
- The source project is never mutated before the complete manifest passes preflight.
- `PatchManifest` is immutable and bound to project identity, session, baseline digest, verified workspace digest, and verification context.
- Each entry is exactly `ADD`, `MODIFY`, or `DELETE` and records canonical path, before/after hashes, size, reviewability, and apply safety.
- Binary, non-UTF-8, symlink, junction, reparse-point, special-file, and unsafe-path changes are visible but blocked for MVP apply.
- Source conflict, manifest corruption, missing content, cancellation, or apply failure must leave or restore the complete pre-apply source tree.
- Manual Apply and Direct auto-apply call the same `ApplyService`; neither reads a fresh unmanifested change set.
- Schema changes are additive, ordered, idempotent migrations. No ad hoc `ALTER TABLE` is permitted.
- No automatic Git operation or Git repository requirement is introduced.

---

### Task 1: Immutable manifest domain and builder

**Files:**
- Create: `app/apply/manifest.py`
- Create: `app/apply/builder.py`
- Create: `app/apply/__init__.py`
- Modify: `app/sandbox/workspace.py`
- Test: `tests/apply/test_patch_manifest.py`

**Interfaces:**
- Consumes: `ProjectIdentity`, `ProjectPath`, `SnapshotManifest`, `VerificationContext`, session baseline/workspace roots.
- Produces: `PatchOperation`, `PatchEntry`, `PatchManifest`, and `PatchManifestBuilder.build(...) -> PatchManifest`.

- [x] Write failing tests for deterministic ADD/MODIFY/DELETE entries, manifest identity, stable ordering, and canonical serialization.
- [x] Run the tests and confirm failure because the manifest domain does not exist.
- [x] Implement frozen manifest value objects with strict enum/hash/path/size invariants and canonical SHA-256 identity.
- [x] Implement a single builder that inventories baseline and workspace without following links and retains unsafe/unreviewable entries with explicit reasons.
- [x] Add failing tests for UTF-8 text, binary/non-UTF-8, protected paths, links/reparse points, special files, and workspace mutation during build.
- [x] Implement fail-closed classification and a final workspace digest recheck.
- [x] Run manifest tests and the existing snapshot/path/patch regressions.

### Task 2: Durable manifest store and apply journal

**Files:**
- Modify: `app/tasks/migrations.py`
- Create: `app/apply/store.py`
- Modify: `app/tasks/unit_of_work.py`
- Modify: `app/agent/session.py`
- Test: `tests/apply/test_manifest_store.py`
- Test: `tests/tasks/test_runtime_unit_of_work.py`

**Interfaces:**
- Consumes: canonical manifest dictionaries and project-scoped `StoreContext`.
- Produces: `PatchManifestStore.save/get`, immutable manifest rows/entries, and `RuntimeUnitOfWork` apply-journal/session transitions.

- [ ] Write failing migration/store tests for project isolation, immutable rows, ordered entries, round-trip identity, and duplicate-save idempotence.
- [ ] Add migrations for `patch_manifests`, `patch_manifest_entries`, and `apply_journal`, including identity/scope foreign keys and state checks.
- [ ] Implement a project-scoped store that verifies canonical manifest ID while reading and refuses mutation or cross-project lookup.
- [ ] Write failing transition tests for `PREPARING`, `APPLYING`, `COMMITTED`, `ROLLED_BACK`, `CONFLICT`, and `RECOVERY_FAILED` plus optimistic session version checks.
- [ ] Implement atomic journal creation/state transitions and atomic session manifest binding/clearing in `RuntimeUnitOfWork`.
- [ ] Run migration, store, session, and UoW tests.

### Task 3: Transactional ApplyService

**Files:**
- Create: `app/apply/service.py`
- Test: `tests/apply/test_apply_service.py`
- Test: `tests/security/test_apply_transactions.py`

**Interfaces:**
- Consumes: persisted `PatchManifest`, immutable workspace content, canonical source root, project identity, session ID/version, `RuntimeUnitOfWork`, and project-local staging root.
- Produces: `ApplyResult(status, applied_paths, reason, manifest_id, journal_id)`.

- [ ] Write failing positive tests for additions, modifications, deletions, empty parent cleanup policy, and exact manifest order.
- [ ] Write failing preflight tests for project/session mismatch, stale source hash, changed workspace hash, unsafe entry, missing content, parent symlink/reparse substitution, and destination type changes.
- [ ] Implement a read-only preflight that validates the complete manifest before creating or modifying source paths.
- [ ] Write failing fault-injection tests at every promotion/delete boundary and prove byte-for-byte rollback of all touched source paths.
- [ ] Implement same-volume staging/backups, journaled `APPLYING`, atomic per-file replacement, explicit deletion, reverse-order rollback, and bounded cleanup.
- [ ] Write and implement cancellation and crash-recovery tests; recovery must deterministically finish rollback from durable backups or mark `RECOVERY_FAILED` without claiming success.
- [ ] Run ApplyService, security boundary, and recovery tests.

### Task 4: Runtime, session, preview, and Direct integration

**Files:**
- Modify: `app/agent/runtime.py`
- Modify: `app/vscode_backend.py`
- Modify: `app/main.py`
- Modify: `app/sandbox/workspace.py`
- Test: `tests/agent/test_runtime_project_session.py`
- Test: `tests/security/test_patch_apply_guard.py`
- Test: `tests/test_vscode_backend.py`
- Test: `tests/test_cli_commands.py`

**Interfaces:**
- Consumes: `PatchManifestBuilder`, `PatchManifestStore`, and `ApplyService`.
- Produces: manifest-backed `patch_preview()` and `apply_patch(confirmed=...)` compatibility façades used by CLI/backend.

- [ ] Write failing runtime tests proving final PASS creates and binds exactly one manifest while FAIL/BLOCKED creates none.
- [ ] Create/persist the manifest before the run/session terminal transition and bind its ID atomically to `DIRTY_VERIFIED`.
- [ ] Write failing preview tests proving preview remains tied to the persisted manifest and detects workspace tampering.
- [ ] Replace live `changed_files()` preview with manifest-backed projection while retaining a generated review diff as a derived artifact.
- [ ] Write failing manual/Direct tests proving both modes invoke the same `ApplyService`, deletions work, conflicts apply zero files, and no legacy copier is called.
- [ ] Route CLI and backend apply through the runtime façade; preserve existing protocol fields and return structured conflict/rollback reasons.
- [ ] Remove `SandboxWorkspace.apply_to_project()` from the production path and reject manifestless legacy apply.
- [ ] Run runtime, CLI, backend, session, and apply security tests.

### Task 5: Stage 3 completion gate

**Files:**
- Modify: `README.md`
- Modify: this plan.

**Interfaces:**
- Consumes: all Stage 3 components and acceptance evidence.
- Produces: documented Stage 3 behavior and a pushed `master` commit.

- [ ] Audit every Stage 3 invariant against executable tests, including ADD/MODIFY/DELETE, binary/link blocking, stale hashes, source conflict, rollback, recovery, manual/Direct parity, and project isolation.
- [ ] Run focused Stage 3 tests and request an independent code review; fix all P1/P2 findings.
- [ ] Run `python -m compileall -q app tests`, full `pytest -q -m "not real_llm"`, real Docker tests when available, and `npm run compile` in `vscode-extension`.
- [ ] Run `git diff --check`, inspect the staged diff, commit implementation, and push `master`.
- [ ] Mark this plan complete in a final documentation commit, push it, fetch origin, and prove local `HEAD == origin/master` with a clean worktree.

## Completion boundary

This plan completes current architecture Stage 3: immutable verified results and transactional source publication. Backend protocol v3 concurrency, responsive control messages, full VS Code product UX, VSIX packaging, and release acceptance remain later stages except for the minimal compatibility changes needed to route existing manual and Direct Apply through the shared service.
