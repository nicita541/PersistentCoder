# PersistentCoder Safe Local MVP Design

**Date:** 2026-09-14  
**Status:** Approved in chat; awaiting written-spec review  
**Source:** `PersistentCoder_MVP_MASTER_ANALYSIS_AND_CODEX_PLAN_2026-09-14.md`

## 1. Goal and release boundary

PersistentCoder will become a safe, local, Python-first coding agent for VS Code. A user selects one source project, submits a natural-language coding request, observes real execution events, and receives a verified set of changes. In Sandbox mode the source remains unchanged until explicit Apply. In Direct mode the framework applies the same verified manifest automatically. Reject, failed verification, conflicts, crashes, and cancellation must not mutate the source project.

The MVP is complete only when automated Python and extension suites pass and the real Docker, local Qwen, and installed VSIX acceptance scenarios pass. Node, .NET, Java, arbitrary binary editing, symlink editing, cloud operation, concurrent agents, automatic git operations, and marketplace publication remain outside the MVP.

## 2. Non-negotiable invariants

- The LLM makes semantic proposals; Python owns identities, state, paths, retries, verification, patching, and security decisions.
- Model-originated commands never execute through a host shell and never receive writable access to the source project or the persistent host sandbox.
- All persistent run, plan, memory, recovery, and sandbox state is scoped to a canonical Project Identity.
- Only validated file actions may mutate the persistent sandbox.
- Verification is structured and fail-closed. Unknown or unsupported verification cannot pass.
- Sandbox review, manual Apply, and Direct Apply consume the same immutable PatchManifest.
- Apply validates every entry before changing anything and rolls back every touched path if an operation fails.
- Events and diagnostics are sanitized before persistence or UI delivery.

## 3. Delivery strategy

Implementation proceeds in five vertical stages. Each stage begins with tests for required and forbidden behavior, then adds the smallest production changes that satisfy those tests, followed by a focused regression. This keeps cross-layer contracts executable while avoiding a long period in which production layers disagree.

### Stage 1: Project identity and session lifecycle

Introduce a `ProjectIdentity` value containing the resolved canonical source root and a stable SHA-256 `project_id`. Windows identity comparison is case-insensitive while the display root preserves its original spelling. Framework-owned state is stored under `data/projects/<project_id>` and `.sandbox/projects/<project_id>`; target projects receive no service files.

Every plan, run, active plan query, persisted agent phase, sandbox session, recovery record, and project memory record carries or is selected through `project_id`. Existing unscoped data is retained but is not silently attached to an arbitrary external project. Global memory is limited to explicit `USER_RULE` entries; `FACT`, `DECISION`, and `EXPERIENCE` are project-scoped.

Add an explicit `AgentSession` with `CLEAN`, `RUNNING`, `DIRTY_VERIFIED`, `DIRTY_FAILED`, `APPLIED`, and `DISCARDED` states. One VS Code chat owns one project identity and one current sandbox baseline. Apply rebases from source; Discard recreates a clean sandbox; New Chat invokes backend session logic and cannot silently abandon dirty work.

### Stage 2: Filesystem and command boundaries

Replace generic snapshot copying with a `SnapshotManifest` produced by a single vetted walker. The walker respects `.gitignore`, framework deny rules, file-count and size limits, and never follows symlinks, junctions, or reparse points. Protected secrets are excluded before content becomes readable to the LLM. Ordinary target directories named `models` or `data` are not globally excluded.

Use one `ProjectPath` normalizer across planning, tools, verification, manifests, and apply. It removes only an exact leading `./`, preserves dot-paths such as `.github`, normalizes separators, rejects absolute, UNC, and parent-traversal paths, and provides a Windows-safe ownership key.

An `AllowedChangeSet` enforces exact task and step ownership. Existing files must be observed in the current attempt before modification. Delete is an exact-file action; recursive directory deletion is unavailable. Rename is represented as explicit Add plus Delete.

Docker execution mounts the persistent sandbox as read-only `/input`, copies it into a size-capped tmpfs `/workspace`, executes without network or privilege, and discards the container workspace. Cancellation, time, output, CPU, memory, and PID bounds are enforced. Dependency installation is framework-controlled at image-build time only.

### Stage 3: Model protocol, planning, verification, and repair

Extract an `ActionEnvelopeDecoder` from the executor. It accepts strict or fenced JSON plus a narrowly enumerated set of real Qwen deviations and aliases. Triple-quoted content repair and `root/` normalization are deterministic and ambiguity fails closed. It never evaluates model output as code. Observation and generation budgets are cumulative and all emitted action events are sanitized.

The planner receives bounded repository context, relevant project memory, explicit user paths, and detected Python capabilities before decomposition. The LLM proposes coarse tasks and file ownership; deterministic Python code builds a dependency DAG and file-oriented steps. One writable file belongs to one implementation step, duplicate producers are rejected, tests are declared explicitly, and plans without authoritative verification contracts are rejected before persistence.

`VerificationSpec` supports `FILE_EXISTS`, `FILE_ABSENT`, `PY_COMPILE`, `PY_IMPORT`, `PY_SYMBOL`, `PY_SIGNATURE`, and targeted `PYTEST`. AST and import checks understand nested modules. Verification persists `PASS`, `FAIL`, or `BLOCKED` with step/task target metadata. Unsupported runtimes are explicit `BLOCKED`, never inferred success.

Repair receives a structured failure context and an `ApproachFingerprint`. Failed or blocked attempts consume the bounded retry budget; infrastructure dependency blocks do not ask the coder to edit project code. Replanning replaces executable pending steps, preserves accepted artifacts, supersedes obsolete work, persists the reason, and reruns task-level verification against the task rather than the last step.

### Stage 4: Durable results, patch/apply, and backend protocol

Durable state stores canonical phases and terminal plan states. Authoritative state synchronization can explicitly clear nullable task, step, attempt, and checkpoint fields. JSON is bounded structurally before serialization rather than truncated mid-string. Event subscribers cannot crash the controller, and the in-memory event buffer is bounded.

`PatchManifest` is the sole representation of verified changes. Each Add, Modify, or Delete entry records path, before and after hashes, type, size, reviewability, and apply safety. Binary, non-UTF-8, symlink, and reparse-point changes are visible but blocked for MVP apply.

`ApplyService` first validates project identity, all paths and parents, all source hashes, and all manifest entries. Any conflict changes nothing. It then stages backups, applies the complete manifest, restores all touched paths on error, persists the outcome, and rebases or discards the session as appropriate. Manual Sandbox Apply and Direct auto-apply call this same service.

Backend protocol v3 keeps the model loaded once while a single worker executes at most one run. The main JSONL loop remains responsive to `cancel`, `new_session`, `get_session_state`, `preview_changes`, `apply_changes`, and `discard_changes`, and streams sanitized `agent_event` messages. CLI and VS Code requests pass through the same policy and runtime entry point.

### Stage 5: VS Code product and release gates

The extension displays the selected project, requires explicit target selection for multi-root workspaces, and renders real planning, file, command, verification, repair, and completion events. It provides Stop, View Changes, Apply, Reject, Direct result, and a backend-backed New Chat flow with dirty-session handling. Placeholder controls are removed when no backend capability exists.

Installed VSIX operation uses configured `persistentCoder.backendRoot`, `persistentCoder.pythonPath`, and `persistentCoder.allowDependencyBuild`; it cannot infer the repository from the extension installation directory. Packaging excludes development dependencies and generated output from source control. README, current architecture documentation, retention cleanup, and deterministic CI are included.

## 4. End-to-end data flow

1. The extension selects a source root and asks the backend to open or create a session.
2. The backend derives Project Identity and opens only that project's durable state.
3. The session creates a vetted baseline and writable persistent sandbox from one SnapshotManifest.
4. Context selection reads only bounded repository data and permitted project/global memory.
5. The planner persists validated tasks, exact file ownership, steps, dependencies, and VerificationSpecs.
6. For each attempt, model output passes through the decoder and policy checks; only allowed file actions mutate the persistent sandbox.
7. Commands and authoritative checks run against an ephemeral container copy.
8. Verification either accepts work, produces structured repair context, requests a bounded structural replan, or terminates as failed/blocked.
9. Successful task verification creates one PatchManifest relative to the immutable baseline.
10. Sandbox returns the manifest for review. Direct invokes the same ApplyService.
11. Apply or Discard establishes a new clean baseline. Crash recovery resumes only a matching project/session and rolls incomplete attempts back to the last checkpoint.

## 5. Error and cancellation model

Errors are typed at subsystem boundaries: identity mismatch, snapshot limit, protected path, invalid action protocol, scope violation, dependency blocked, verification failed/blocked, source conflict, apply rollback, model missing/load/OOM/generation, cancellation, and backend protocol error. User-facing messages contain safe context but no secret content or unbounded model output.

Cancellation is cooperative through model generation, controller loops, and Docker commands. It leaves the source unchanged, rolls back the current attempt, records an interrupted/cancelled terminal outcome, and keeps the backend protocol responsive. Event persistence and UI failures are non-authoritative and cannot convert a failed operation into success.

## 6. Test architecture

Each stage adds unit, integration, negative-security, and migration tests alongside production code. Fixtures use synthetic or sanitized Qwen transcripts and projects. Focused suites run during a stage; aggregate suites run at integration checkpoints and again at release.

Required coverage includes:

- Project A/B isolation for state, memory, active plans, recovery, and sandbox namespaces.
- Secret, symlink/junction/reparse, ignore, count, path, and byte snapshot boundaries.
- Ephemeral command isolation and cancellation.
- Canonical dot-paths, exact ownership, current-attempt observation, and safe deletion.
- Real Qwen envelope variants without permissive parsing.
- Planner context, unique file producers, deterministic steps, and verification contracts.
- Python AST/import/signature/targeted-pytest behavior and explicit blocked runtimes.
- Dependency parsing/build policy and real Docker image execution.
- Changed repair approaches, structural task replans, retry bounds, and durable terminal state.
- Manifest additions/modifications/deletions, binary blocking, concurrent source conflicts, transactional rollback, rebase, and discard.
- Backend v3 responsiveness, live events, single-run enforcement, and cancellation.
- Extension process/protocol/actions, packaging, settings, and installed VSIX behavior.
- Existing security regressions plus real Docker and real local-Qwen end-to-end scenarios.

## 7. Acceptance evidence

Release completion requires recorded evidence for:

- Python compilation and the complete Python regression suite.
- Extension clean install, compile, and tests.
- Hardened real Docker integration.
- Real natural-language Qwen scenarios: create, observe-and-modify, multi-step feature, Sandbox preview, exact Apply, Direct apply, conflict blocking, Stop, and Project A/B isolation.
- VSIX packaging and manual installation outside the repository, including Reject, Apply, Direct, conflict, Stop, restart recovery, and second-project isolation.
- A final report listing files, migrations, tests, exact results, remaining post-MVP limits, and verification of every security invariant.

No MVP-complete claim is valid when any required gate is skipped, unavailable, or supported only by indirect evidence.

## 8. Compatibility and migration

Schema changes use additive, idempotent migrations. Legacy unscoped records remain readable for diagnostics but are excluded from external-project prompts and recovery until explicitly associated by a safe migration rule. Public Python and protocol interfaces retain compatibility adapters only where they cannot bypass new identity, path, scope, verification, or apply checks.

The implementation extends the existing `app/agent`, `app/context`, `app/llm`, `app/memory`, `app/policy`, `app/sandbox`, `app/tasks`, `app/tools`, `app/vscode_backend.py`, and `vscode-extension` layers. It does not create a parallel agent architecture.
