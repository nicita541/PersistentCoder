from __future__ import annotations

import pytest

from app.agent.planner.decomposer import PlannerError
from app.agent.planner.task_builder import TaskBuilder


def _component():
    return {
        "key": "feature",
        "title": "Feature",
        "description": "Implement source and tests",
        "requires": [],
        "produces": ["feature"],
        "success_criteria": ["feature works"],
        "change_paths": ["src/feature.py", "tests/test_feature.py"],
        "verification_specs": [
            {"kind": "PY_COMPILE", "target": "src/feature.py"},
            {"kind": "PYTEST", "target": "tests/test_feature.py"},
        ],
        "steps": [
            {
                "title": "model suggested broad step",
                "description": "touch both files",
                "change_paths": ["src/feature.py", "tests/test_feature.py"],
            }
        ],
    }


def test_step_builder_deterministically_creates_one_writable_file_per_step():
    steps = TaskBuilder().build_steps([_component()])["feature"]

    assert [step.change_paths for step in steps] == [
        ["src/feature.py"],
        ["tests/test_feature.py"],
    ]
    assert [step.verification_specs[0].target for step in steps] == [
        "src/feature.py",
        "tests/test_feature.py",
    ]


def test_every_owned_file_requires_a_structured_verification_spec():
    component = _component()
    component["verification_specs"] = []

    with pytest.raises(PlannerError, match="verification spec"):
        TaskBuilder().build_steps([component])
