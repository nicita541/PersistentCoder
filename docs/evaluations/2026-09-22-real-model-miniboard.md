# Real-model evaluation: MiniBoard

## Post-training update: typed tools and role-aware v6

The original baseline below remains useful as a record, but it is no longer the
current result.  After the baseline, the project gained a reproducible QLoRA
dataset, typed model tools, bounded Debugger analysis, and the local adapter:

`models/adapters/planner-coder-debugger-qwen2.5-3b-v6`

### What changed

- Model-originated arbitrary command strings are rejected before any file write.
- The model may request only `py_compile`, focused `pytest`, `python_file`, or an
  allowlisted `python_module`; trusted code constructs argv and Docker executes
  it without a shell.
- Missing-file reads receive explicit non-repetition feedback. Repeated
  observations are recorded as evidence.
- The Debugger produces a bounded structured diagnosis with `root_cause`,
  `do_not_repeat`, and `next_action`. Its token budget is adaptive: LOW 160,
  MEDIUM 256, HIGH 384.
- Planner, Coder, and Debugger accept independent LLM clients, while the default
  still shares one model to fit an 8 GB-class GPU.
- Invalid outer JSON caused by unescaped quotes inside one generated file can be
  repaired through a narrow string parser. A nested object is no longer mistaken
  for the action envelope.

Direct execution on the Windows host is intentionally not enabled. A working
directory and path checks cannot stop arbitrary Python code from opening an
absolute host path. The future `ProjectToolRunner` seam requires an OS-enforced,
fail-closed filesystem boundary before it may replace Docker.

### Training and held-out evaluation

The generated dataset now contains 183 training and 35 validation records over
nine stages, plus dedicated recovery files for coder tools, Debugger failures,
and combined role recovery. It covers planning, planner repair, new and existing
files, deletion, protocol repair, invalid-command repair, missing-file loops,
and structured failure diagnosis.

| Adapter | Training scope | Time | Peak GPU | Held-out result |
|---|---|---:|---:|---:|
| v4-tools | planner/coder recovery, 125 micro-steps | 213 s | 6.47 GiB | 30/30 |
| v5-tools | focused tool recovery, 68 micro-steps | 70.5 s | 5.28 GiB | missing-read repair 4/4 |
| v6 roles | coder + Debugger, 92 micro-steps | 75.2 s | 5.28 GiB | 35/35 |

The 35/35 result is contract validity, not proof of project completion. The
MiniBoard E2E remained deliberately separate.

### Updated MiniBoard E2E result

v6 consistently created a seven-task plan. In the strongest run it completed
and independently verified four tasks:

1. update the existing Issue model;
2. create the JSON repository;
3. create the service;
4. create the CLI.

Every generated Python module passed a Docker `python -m py_compile` invocation
selected through a typed tool. The fifth task created a focused pytest file and
ran `pytest(tests/test_service.py)`, but collection returned rc=2 because the
model used an invalid relative import from the top-level tests package. The
Debugger received the failure, but two retries reproduced the bad test and the
run safely ended `FAILED`. README and legacy cleanup were therefore not reached.

An earlier v6 run exposed malformed outer JSON around otherwise correct test
content. The deterministic decoder fix allowed later runs to reach real pytest;
it did not hide or auto-correct the faulty Python import.

No source MiniBoard file was changed. Failed sandbox work was either retained for
inspection or explicitly discarded before a fresh run; no manifest was eligible
for Apply.

### Updated comparison

The trained system is materially better than the baseline: it plans reliably,
performs real bounded edits, executes typed verification tools, records failure
evidence, and gets through most of this small project. It is no longer accurate
to describe it as unable to leave planning.

A conventional client backed by a stronger model is still the better completion
tool today. PersistentCoder's 3B model stopped at an elementary test import after
several minutes, while a strong client would likely repair it immediately. The
current advantage remains safety and auditability: exact change scopes,
Docker-only execution, durable attempts, deterministic verification, rollback,
and no source mutation before Apply.

The next model experiment should therefore be an A/B test, not more blind
fine-tuning: keep the v6 Coder for cheap local edits and route Planner or Debugger
to a stronger compatible local model on the same fixed evaluation set.

Framework regression status after these changes: `760 passed` in the complete
pytest suite (140 seconds, including Docker-gated tests available on this host).

Date: 2026-09-22  
Models: `Qwen/Qwen2.5-Coder-1.5B-Instruct` and
`Qwen/Qwen2.5-Coder-3B-Instruct`  
Target: disposable Python project under `F:\PersistentCoder\data\evaluations`  
Mode: public VS Code JSONL backend, sandbox mode, real local model

## Task

Build a small but complete local issue tracker in approximately the scope of a
5–10 minute coding task:

- typed `Issue` domain model with IDs, status and timestamps;
- atomic JSON persistence;
- create/list/show/close service;
- `python -m miniboard` CLI;
- pytest coverage and README examples;
- delete an obsolete module;
- standard library only and modular design.

The starter contained six small files: `pyproject.toml`, `README.md`, three
package files (including the obsolete module) and one smoke test.

## Method

Three real runs were made against equivalent untouched source trees:

| Run | Model | Prompt | Length | Duration | Terminal result |
|---|---|---|---:|---:|---|
| 1 | Qwen 1.5B | Detailed requirements | 946 chars | 83 s | Planner failure |
| 2 | Qwen 1.5B | Compact requirements | 297 chars | 91 s | Planner failure |
| 3 | Qwen 3B | Same compact prompt | 297 chars | 187 s | Planner failure |

Both models were loaded locally. The 3B model occupied approximately 7.78 GB of
the 8.15 GB GPU and loaded in about four seconds. Docker was available to the
backend, although no run reached command execution or verification.

The first two disposable-run artifacts were initially created in a Codex
visualization directory. After the storage boundary was clarified, those files
and the junction were deleted; the retained evaluation project, request,
download helper and model weights all live under `F:\PersistentCoder`.

## Observed behaviour

### Run 1

The goal-analysis/decomposition pipeline attempted to repair malformed model
output once and then returned:

```text
TASK_DECOMPOSER repair failed after 1 repair attempts: tasks must be a list
```

### Run 2

The shorter prompt got further into plan validation and used both plan-repair
attempts, then returned:

```text
plan repair failed after 2 attempts: invalid step decomposition:
task 'pytest_tests' step 'Implement src/tests/test_miniboard.py'
requires unknown resource 'CLI'
```

This indicates that the model understood the broad deliverables (`pytest_tests`
and `CLI`) but could not satisfy the planner's exact resource graph schema.

### Run 3 — larger model

Qwen 3B received exactly the same 297-character compact request as run 2. It
used 187 seconds—2.05 times the 1.5B duration—and returned the same primary
failure as run 1:

```text
TASK_DECOMPOSER repair failed after 1 repair attempts: tasks must be a list
```

Doubling the model size therefore did not improve the completion stage reached
or produce any durable plan. It only increased latency on this hardware.

## What was persisted

The two project-scoped SQLite databases contain in total:

- 3 evaluated `agent_runs`, all `status=INTERRUPTED`, `phase=FAILED`;
- 6 state snapshots: `PLANNING` and `FAILED` for each run;
- 3 `observe` events;
- clean sandbox sessions with no bound manifest;
- 0 plans, tasks, steps, attempts, verifications or memories;
- 0 patch manifests, manifest entries or apply journals.

The state snapshots retain the full request and terminal phase, but they do not
retain the actual planner validation error. The event payload is capped; the
946-character request is truncated in the event while the full value remains in
the run and snapshot rows.

After process restart, CLI `/status`, `/plan` and `/timeline` reported no latest
run or plan even though the two terminal run rows and four snapshots existed.
This is an observability gap: the durable data exists, but the normal user-facing
inspection path does not surface an early planning failure.

## Safety result

All six source files had exactly the same SHA-256 before and after all three
runs. The inspected sandbox workspaces also matched the source tree
file-for-file and byte-for-byte. No source write, deletion, manifest or Apply
was attempted.

This is the strongest part of the current system: planner failures fail closed
and do not leak partial work into the user's project.

## Scorecard

| Area | Score | Evidence |
|---|---:|---|
| Source isolation | 10/10 | All source SHA-256 values unchanged |
| Fail-closed behaviour | 9/10 | Explicit backend failure; session returned to `CLEAN` |
| Planning correctness | 1/10 | 0 successful plans from 3 runs and 2 model sizes |
| Repair effectiveness | 2/10 | Repairs ran but did not normalize either schema failure |
| Persistence | 6/10 | Runs and snapshots durable; terminal reason not durable |
| User observability | 3/10 | Roughly 90 s of silence; restart UI cannot show the failed run |
| Coding quality | Not reached | No task, step or file operation was produced |
| Verification/Apply | Not reached | No verification context or manifest was produced |
| End-to-end usefulness | 1/10 | A normal 5–10 minute task cannot currently leave planning |

## Comparison with a conventional coding client

This comparison is an engineering judgement based on the observed runs, not a
separate benchmark of a named product.

A conventional coding client with a stronger general coding model would likely
inspect the six files, produce a short informal plan and begin implementation.
For this task it would probably be materially more productive: PersistentCoder
spent 361 seconds across three completed attempts and produced no plan or code.

PersistentCoder is better in a narrower but important area: isolation and
transaction safety. A conventional direct-write client may leave half-finished
files when planning or generation fails; PersistentCoder left both source and
sandbox at a known clean baseline.

The current trade-off is therefore unfavourable for routine work:

- **PersistentCoder wins:** isolation, explicit state machine, durable snapshots,
  no partial source writes, transactional Apply architecture.
- **Conventional client wins:** tolerance of imperfect model output, time to
  first edit, graceful replanning and practical completion rate.

The strict planner is acting as a schema test for a 1.5B model rather than as a
useful coding workflow. The safety architecture is ready; the real-model control
plane is not.

## Recommended fixes, in order

1. Persist a terminal `error_code` and sanitized `error_message` on every run;
   classify handled planner failures as `FAILED`, not `INTERRUPTED`.
2. Make `/status` and `/timeline` load the latest terminal run after restart,
   even when no plan was created.
3. Add progress events for goal analysis, decomposition, dependency analysis and
   each repair attempt so a 90-second planner run is not silent.
4. Use constrained JSON/schema decoding. Scaling from 1.5B to 3B did not fix the
   schema error, while doubling planner latency; more retries or size alone are
   unlikely to be cost-effective.
5. Normalize common recoverable shapes deterministically (`tasks` wrapper,
   singular/list variants) before asking the model to repair them.
6. When a step references an unknown logical resource such as `CLI`, either map
   it to the producing task deterministically or degrade to a safe single-task
   plan instead of rejecting the whole run.
7. Add a small-task fast path: one plan call, bounded file set and direct
   verification. The current multi-pass planner is too expensive for a task of
   this size.
8. Add this real-model MiniBoard scenario as an opt-in/nightly evaluation with
   metrics for planning success, time to first edit, attempts and final tests.

## Verdict

PersistentCoder currently behaves safely but not usefully on this realistic
small project with either tested local model. It records enough to prove that
the source was protected, but not enough to explain the planner failure through
the normal UI after restart. A conventional coding client would be the better
choice for completing the task today; PersistentCoder's architecture becomes
valuable only after planner reliability and observability are fixed.
