# Stage 3: Qwen, Planner, Verification, Repair, and Durable State

> Execute task-by-task with tests first and preserve the authoritative Python/security boundaries.

## Goal

Make the Python-first agent reliable with the real Qwen protocol: bounded decoding and observations, repository-aware file-oriented plans, structured verification, dependency gating, materially different repair/replan, and durable terminal/recovery/event state.

## Task 1 — Central action decoder

- Add `app/agent/coder/protocol.py` with typed envelopes/errors.
- Support strict/fenced JSON, known action aliases, mapping/single-file forms, narrowly bounded triple-quoted content repair, and unique task-aware `root/` normalization.
- Do not use `eval` or general Python parsing.
- Add sanitized Qwen regression fixtures and decoder tests.

## Task 2 — Bounded observable tool loop and real events

- Enforce cumulative observation bytes and transcript size.
- Emit sanitized list/read/search/protocol-repair/edit/delete events without contents/prompts.
- Thread cancellation through model loop and command runner.
- Add budget, event, and cancellation tests.

## Task 3 — Repository-aware file planner

- Give Goal/Task decomposition bounded capability/path/language/project-memory context.
- Require canonical unique Task file ownership.
- Deterministically generate one writable file per Step; explicit test files are separate owned work.
- Prefer deterministic resource DAG; use LLM dependency output only as bounded fallback.
- Reject unowned, duplicate, infrastructure-inventing, or unverifiable plans before persistence.

## Task 4 — Structured verification

- Add persisted `VerificationSpec` kinds: FILE_EXISTS, FILE_ABSENT, PY_COMPILE, PY_IMPORT, PY_SYMBOL, PY_SIGNATURE, PYTEST.
- Implement fail-closed AST/import/signature and targeted multi-pytest checks.
- Never infer PASS from filename-like prose and never run the whole suite unless explicitly authorized.
- Verify Task-wide accepted artifacts and persist STEP/TASK target plus PASS/FAIL/BLOCKED.

## Task 5 — Dependency authority

- Parse requirements with `packaging.Requirement` and deny URLs/paths/options.
- Preserve Poetry constraints and fix generated Dockerfile continuation.
- Gate dependent Tasks as BLOCKED when framework dependency image is unavailable; never ask the model to install/fix it.
- Add real Docker build test behind the Docker marker.

## Task 6 — Repair and structural replan

- Pass typed `RepairContext` to the next coder prompt.
- Store/check one canonical `ApproachFingerprint`.
- Count only FAILED/BLOCKED attempts against retry budgets.
- Replace obsolete Steps on REPLAN_TASK while keeping accepted work and persisting the reason.
- Ensure Task failure repairs Task rather than reopening the last Step.

## Task 7 — Durable state, recovery, and events

- Persist canonical AgentPhase and terminal Plan status.
- Add authoritative state sync that clears nullable active IDs/checkpoint.
- Sanitize fields before valid JSON serialization; isolate failing subscribers and bound the event buffer.
- Recover only the matching project/session and expose interrupted state explicitly.
- Bound WorkingContext and remove/avoid unsupported read-only UI claims.

## Stage gate

Run compileall, focused coder/planner/verifier/repair/runtime/security tests, aggregate non-real-LLM pytest, Docker-marked tests when Docker is available, then document exact evidence.
