# Trusted Verification and Sandbox Limits Implementation Plan

> **Execution note:** Implement this plan task-by-task with TDD and verify each
> checkpoint before continuing.

**Goal:** Make verification a durable, revision-bound contract executed through
framework-owned argv in a bounded sandbox, with every missing or malformed
prerequisite reported as `BLOCKED` instead of silently weakening the check.

**Architecture:** `VerificationContext` fingerprints the workspace revision,
canonical verification specifications, and execution environment. Structured
verification returns a first-class `VerificationResult`; the task state machine
persists its transition separately together with the context. Framework checks
use `CommandRequest(argv, cwd)` and bypass model shell parsing. `ProjectPath`
remains the canonical lexical boundary for every filesystem-facing component.
The command runner streams bounded output and refuses work before launch when
capacity is exhausted. Dependency parsing is strict and reports malformed,
oversized, truncated, or unsupported manifests as blocked evidence.

---

### Task 1: Define revision-bound verification evidence

**Files:**
- Create: `app/tasks/verification_context.py`
- Modify: `app/agent/state.py`
- Modify: `app/tasks/models.py`
- Modify: `app/tasks/verification_store.py`
- Modify: `app/agent/verifier/agent.py`
- Create: `tests/agent/test_verification_context.py`

- [x] Add canonical spec and environment fingerprints plus a workspace snapshot
  digest.
- [x] Attach the context to every verification result and durable record.
- [x] Reject stale evidence after a workspace, spec, or environment change.
- [x] Keep verification computation separate from task/step state transitions.

### Task 2: Execute framework checks as argv

**Files:**
- Modify: `app/sandbox/runner.py`
- Modify: `app/agent/verifier/structured.py`
- Modify: `app/agent/verifier/criterion.py`
- Modify: `tests/agent/test_structured_verification.py`
- Modify: `tests/security/test_command_sandbox.py`

- [x] Add a framework-only argv API with canonical cwd validation.
- [x] Convert compile, import, and pytest checks to argv.
- [x] Prove shell metacharacters remain literal arguments.
- [x] Report unavailable Docker/image/runtime as `BLOCKED`.

### Task 3: Bound concurrent commands and streamed output

**Files:**
- Modify: `app/sandbox/limits.py`
- Modify: `app/sandbox/runner.py`
- Modify: `tests/security/test_command_sandbox.py`

- [x] Add admission control for concurrent commands.
- [x] Drain stdout/stderr incrementally with a hard retained-output bound.
- [x] Kill on timeout, cancellation, and output flood without buffering the full
  child output in memory.
- [x] Preserve Docker memory, disk/tmpfs, CPU, PID, user, and network limits.

### Task 4: Make dependency manifests fail closed

**Files:**
- Modify: `app/sandbox/dependencies.py`
- Modify: `tests/security/test_dependency_provisioning.py`

- [x] Reject oversized and undecodable manifests without truncation.
- [x] Report malformed TOML/CFG and unsafe nested requirement includes.
- [x] Reject requirement-count overflow instead of slicing it.
- [x] Ensure any refused or unsupported dependency declaration yields a blocked
  dependency plan.

### Task 5: Complete the Stage 2 gate

**Files:**
- Modify: `README.md`
- Modify: this plan
- Modify only production/tests implicated by failing evidence.

- [x] Cover ADS and reserved Windows paths through the shared `ProjectPath`.
- [x] Cover nested Python imports/signatures, no-collected-tests, changed test
  configuration, stale evidence, malformed dependencies, output flood,
  disk/memory limits, and unavailable Docker.
- [x] Run focused Stage 2 tests, `compileall`, the full non-real-LLM test suite,
  and the extension compile check.
- [ ] Commit and push Stage 2 to `master`.

## Completion boundary

This plan completes only Stage 2. It does not implement immutable apply
manifests or source publication; those remain Stage 3.
