# Planner Tool Loop Implementation Plan

**Goal:** Replace monolithic AI plan JSON generation in runtime with a small iterative tool loop while preserving the existing Task OS, PlanStore, Scheduler, Verifier, Replanner, and legacy AIPlanner tests.

**Architecture:** Qwen emits one JSON tool action per turn. A deterministic Python workspace applies allowed actions, validates task contracts, infers `depends_on` only from `requires`/`produces`, and refuses `finish_plan` until the plan validates. External libraries remain in `external_dependencies`.

**Files:**
- Create `app/tasks/planner_tools.py`
- Create `app/tasks/planner_tool_loop.py`
- Modify `app/tasks/planner_runtime.py`
- Create `tests/test_planner_tool_loop.py`
- Keep `app/tasks/ai_planner.py` intact for regression coverage

**Validation:**
1. RED: tool-loop tests fail before implementation.
2. GREEN: targeted tool-loop tests pass.
3. Full regression: `python -m pytest -q`.
4. Manual runtime: `python -m app.tasks.planner_demo`.
