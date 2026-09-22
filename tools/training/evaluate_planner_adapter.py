from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from app.agent.planner.contract_builder import ContractBuilder
from app.agent.planner.decomposer import GoalAnalyzer, TaskDecomposer, parse_json_object
from app.agent.planner.step_validator import StepValidator
from app.agent.planner.task_builder import TaskBuilder
from app.agent.coder.protocol import ActionEnvelopeDecoder
from app.agent.coder.tool_protocol import decode_tool_calls
from app.tasks.change_scope import AllowedChangeSet


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = PROJECT_ROOT / "models" / "manual" / "Qwen2.5-Coder-3B-Instruct"
DEFAULT_ADAPTER = PROJECT_ROOT / "models" / "adapters" / "planner-coder-debugger-qwen2.5-3b-v6"
DEFAULT_DATASET = PROJECT_ROOT / "data" / "training" / "planner-v1" / "validation.jsonl"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "training" / "evaluations" / "planner-coder-debugger-adapter-v6.jsonl"


def _inside_project(path: Path, label: str) -> Path:
    result = path.resolve()
    if not result.is_relative_to(PROJECT_ROOT):
        raise SystemExit(f"{label} must stay inside the project")
    return result


def _validate(stage: str, response: str, expected: str) -> None:
    if stage == "GOAL_ANALYZER":
        GoalAnalyzer.parse(response)
        return
    if stage == "TASK_DECOMPOSER":
        components = TaskDecomposer.parse(response)
        builder = TaskBuilder()
        tasks = builder.build(components)
        errors = StepValidator().validate_all(tasks, builder.build_steps(components))
        if errors:
            raise ValueError("; ".join(errors))
        ContractBuilder().validate_contracts(tasks)
        return
    if stage == "DEPENDENCY_BUILDER":
        actual = parse_json_object(response).get("dependencies")
        expected_keys = set(parse_json_object(expected)["dependencies"])
        if not isinstance(actual, dict) or set(actual) != expected_keys:
            raise ValueError("dependencies must contain every expected task key")
        if not all(
            isinstance(values, list)
            and all(isinstance(item, str) and item in expected_keys for item in values)
            for values in actual.values()
        ):
            raise ValueError("dependency values must be lists of known task keys")
        return
    if stage == "PLANNER_REPAIR":
        expected_object = parse_json_object(expected)
        if "tasks" in expected_object:
            _validate("TASK_DECOMPOSER", response, expected)
        elif "dependencies" in expected_object:
            _validate("DEPENDENCY_BUILDER", response, expected)
        else:
            _validate("GOAL_ANALYZER", response, expected)
        return
    if stage in {"CODER_ACTION", "CODER_REPAIR", "COMMAND_REPAIR"}:
        expected_envelope = ActionEnvelopeDecoder().decode(expected)
        paths = [
            str(item["path"])
            for item in expected_envelope.get("files", [])
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        ]
        if not paths and isinstance(expected_envelope.get("path"), str):
            paths = [str(expected_envelope["path"])]
        allowed = AllowedChangeSet(paths, paths) if paths else None
        actual = ActionEnvelopeDecoder().decode(response, allowed_changes=allowed)
        if actual.get("commands") not in (None, []):
            raise ValueError("arbitrary commands are forbidden")
        decode_tool_calls(actual.get("tools"), allowed_changes=allowed)
        if actual.get("action") != expected_envelope.get("action"):
            raise ValueError(
                f"expected action {expected_envelope.get('action')}, got {actual.get('action')}"
            )
        if expected_envelope.get("action") == "edit":
            actual_paths = [
                item.get("path") for item in actual.get("files", []) if isinstance(item, dict)
            ]
            if actual_paths != paths:
                raise ValueError(f"expected edit paths {paths}, got {actual_paths}")
        return
    if stage == "DEBUG_DIAGNOSIS":
        actual = parse_json_object(response)
        required = {"root_cause", "do_not_repeat", "next_action"}
        if set(actual) != required or not all(
            isinstance(actual[key], str) and actual[key].strip() for key in required
        ):
            raise ValueError("debugger response must contain three non-empty string fields")
        return
    raise ValueError(f"unknown stage: {stage}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--adapter", type=Path, default=DEFAULT_ADAPTER)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--stage",
        choices=[
            "ALL",
            "GOAL_ANALYZER",
            "TASK_DECOMPOSER",
            "DEPENDENCY_BUILDER",
            "PLANNER_REPAIR",
            "CODER_ACTION",
            "CODER_REPAIR",
            "COMMAND_REPAIR",
            "DEBUG_DIAGNOSIS",
        ],
        default="ALL",
    )
    args = parser.parse_args()

    model = _inside_project(args.model, "model")
    adapter = _inside_project(args.adapter, "adapter")
    dataset = _inside_project(args.dataset, "dataset")
    output = _inside_project(args.output, "output")
    output.parent.mkdir(parents=True, exist_ok=True)

    os.environ["PERSISTENTCODER_MODEL_NAME"] = str(model)
    os.environ["PERSISTENTCODER_MODEL_ADAPTER"] = str(adapter)
    os.environ["PERSISTENTCODER_LOAD_IN_4BIT"] = "1"
    from app.llm.client import QwenClient

    records = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines()]
    if args.stage != "ALL":
        records = [record for record in records if record["stage"] == args.stage]
    llm = QwenClient()
    results: list[dict[str, object]] = []
    limits = {
        "GOAL_ANALYZER": 384,
        "TASK_DECOMPOSER": 1024,
        "DEPENDENCY_BUILDER": 640,
        "PLANNER_REPAIR": 1024,
        "CODER_ACTION": 1024,
        "CODER_REPAIR": 1024,
        "COMMAND_REPAIR": 1024,
        "DEBUG_DIAGNOSIS": 384,
    }

    for record in records:
        started = time.perf_counter()
        response = llm.chat(record["messages"], max_new_tokens=limits[record["stage"]])
        error = None
        try:
            _validate(record["stage"], response, record["response"])
        except Exception as caught:  # evaluation must retain every failure
            error = f"{type(caught).__name__}: {caught}"
        result = {
            "id": record["id"],
            "stage": record["stage"],
            "valid": error is None,
            "error": error,
            "duration_seconds": time.perf_counter() - started,
            "response": response,
        }
        results.append(result)
        print(json.dumps({key: value for key, value in result.items() if key != "response"}), flush=True)

    output.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in results),
        encoding="utf-8",
        newline="\n",
    )
    summary = {
        "total": len(results),
        "valid": sum(item["valid"] is True for item in results),
        "invalid": sum(item["valid"] is False for item in results),
        "duration_seconds": sum(float(item["duration_seconds"]) for item in results),
        "output": str(output.relative_to(PROJECT_ROOT)),
    }
    print(json.dumps(summary, indent=2), flush=True)
    if summary["invalid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
