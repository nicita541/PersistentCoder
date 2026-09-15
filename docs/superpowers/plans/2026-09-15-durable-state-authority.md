# Durable State Authority — Stage 1 Implementation Plan

> **Execution:** This plan is implemented in the current `master` checkout. The approved architecture and the user's stage command are the design approval and execution choice.

**Goal:** Make execution state, repair budgets, writable attempts, recovery, and terminal outcomes durable under exceptions, process crashes, and concurrent writers.

**Architecture:** A project-scoped `RuntimeUnitOfWork` owns mandatory SQLite transitions under `BEGIN IMMEDIATE`. It writes immutable state snapshots, consumes repair budgets, coordinates Attempt and file-journal records, and commits terminal plan/run/session state. Filesystem checkpoints remain external resources, so a durable journal records intent before checkpoint creation and readiness after it; recovery reconciles every unfinished journal idempotently. EventBus remains best-effort telemetry and cannot change recovery state.

**Tech Stack:** Python 3.12, SQLite, pytest, existing Task OS and sandbox workspace.

## Constraints

- Preserve ProjectIdentity scoping and all existing history.
- Apply additive, numbered, idempotent migrations; do not dual-write old and new authorities.
- A coder call cannot start without a durable Attempt and a ready checkpoint journal.
- Mandatory persistence failures propagate and stop execution.
- Rollback failures remain durable and visible to the caller.
- Recovery is project-scoped and idempotent.
- SQLite permits only one mandatory writer at a time.
- Stage 2 concerns (command policy, immutable manifests, Apply Service) remain out of scope.

## Task 1: Versioned migrations and transaction primitive

**Files:**
- Create `app/tasks/migrations.py`
- Create `app/tasks/unit_of_work.py`
- Create `tests/tasks/test_runtime_unit_of_work.py`

- [x] Write failing tests for ordered/idempotent migrations, state snapshot atomicity, write failure rollback, foreign ProjectIdentity rejection, and concurrent writer rejection.
- [x] Add `schema_migrations`, `agent_state_snapshots`, `budget_ledger`, and `file_operation_journal` migrations.
- [x] Implement project-scoped `BEGIN IMMEDIATE` transactions and atomic state transitions.

## Task 2: Durable budgets and attempt/checkpoint protocol

**Files:**
- Modify `app/agent/controller.py`
- Modify `app/agent/state.py`
- Modify `app/tasks/unit_of_work.py`
- Create `tests/agent/test_durable_attempts.py`

- [x] Write failing tests proving checkpoint failure and DB failure prevent the coder call.
- [x] Persist Attempt plus PREPARING journal before touching files.
- [x] Mark the journal READY and persist its checkpoint cursor before writable execution.
- [x] Finish Attempt and journal through mandatory transitions.
- [x] Propagate rollback/commit failures and leave a recoverable journal.
- [x] Replace ephemeral repair-budget authority with the durable budget ledger.

## Task 3: Mandatory phase transitions and telemetry separation

**Files:**
- Modify `app/agent/loop.py`
- Modify `app/agent/runtime.py`
- Modify `tests/agent/test_runtime.py`

- [x] Write failing tests proving every phase transition is snapshotted and EventBus cannot alter cursor state.
- [x] Persist observed and post-controller states through the UnitOfWork.
- [x] Remove cursor updates from `_persist_event`; retain sanitized best-effort event logging.
- [x] Make mandatory transition failures interrupt the run.

## Task 4: Journal recovery and atomic terminal outcome

**Files:**
- Modify `app/agent/runtime.py`
- Modify `app/tasks/unit_of_work.py`
- Modify `tests/security/test_crash_recovery.py`
- Create `tests/security/test_durable_recovery.py`

- [x] Write crash-point tests before/after checkpoint, writable action, verification, and commit.
- [x] Test missing checkpoint, rollback failure, repeated recovery, and foreign-project journal isolation.
- [x] Reconcile pending journals before resuming or creating a sandbox.
- [x] Finish plan, run, snapshot, and session in one transaction.

## Task 5: Verification and delivery

- [x] Run focused Stage 1 tests.
- [x] Run the full pytest suite and compile checks.
- [x] Review the final diff for Stage 2 scope leakage and suppressed mandatory errors.
- [x] Commit Stage 1 and push `master` to `origin`.
