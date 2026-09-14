from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from app.tasks.verification_spec import VerificationSpec


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    VERIFYING = "VERIFYING"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    VERIFYING = "VERIFYING"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"


class PlanStatus(str, Enum):
    ACTIVE = "ACTIVE"
    DONE = "DONE"
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"


class AttemptStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    PASS = "PASS"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class AttemptTargetType(str, Enum):
    TASK = "TASK"
    STEP = "STEP"


class VerificationStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class VerificationTargetType(str, Enum):
    TASK = "TASK"
    STEP = "STEP"


@dataclass
class TaskDraft:
    title: str
    description: str

    priority: int = 50
    parent_id: int | None = None

    requires: list[str] = field(
        default_factory=list
    )

    produces: list[str] = field(
        default_factory=list
    )

    success_criteria: list[str] = field(
        default_factory=list
    )

    key: str | None = None

    depends_on: list[str] = field(
        default_factory=list
    )
    external_dependencies: list[str] = field(
        default_factory=list
    )
    change_paths: list[str] = field(default_factory=list)
    verification_specs: list[VerificationSpec] = field(default_factory=list)


@dataclass
class StepDraft:
    title: str
    description: str

    requires: list[str] = field(
        default_factory=list
    )

    produces: list[str] = field(
        default_factory=list
    )

    success_criteria: list[str] = field(
        default_factory=list
    )
    change_paths: list[str] = field(default_factory=list)
    verification_specs: list[VerificationSpec] = field(default_factory=list)


@dataclass
class PlanDraft:
    user_request: str
    global_goal: str

    tasks: list[TaskDraft] = field(
        default_factory=list
    )


@dataclass
class TaskRecord:
    id: int
    plan_id: int
    parent_id: int | None

    title: str
    description: str

    status: TaskStatus
    priority: int

    requires: list[str]
    produces: list[str]
    success_criteria: list[str]

    current_step: int | None
    attempt_count: int

    result_summary: str | None
    result_artifacts: list[str]

    verification_status: str | None
    verification_evidence: list[str]

    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str

    key: str | None = None

    depends_on: list[int] = field(
        default_factory=list
    )

    external_dependencies: list[str] = field(
        default_factory=list
    )
    change_paths: list[str] = field(default_factory=list)
    verification_specs: list[VerificationSpec] = field(default_factory=list)

@dataclass
class StepRecord:
    id: int
    task_id: int
    position: int

    title: str
    description: str

    status: StepStatus

    requires: list[str]
    produces: list[str]
    success_criteria: list[str]

    attempt_count: int

    result_summary: str | None
    result_artifacts: list[str]

    failure_reason: str | None

    verification_status: str | None
    verification_evidence: list[str]

    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str
    change_paths: list[str] = field(default_factory=list)
    verification_specs: list[VerificationSpec] = field(default_factory=list)


@dataclass
class AttemptRecord:
    id: int

    target_type: AttemptTargetType
    target_id: int

    attempt_number: int
    status: AttemptStatus

    approach: str | None

    failure_reason: str | None

    result_summary: str | None
    result_artifacts: list[str]

    created_at: str
    started_at: str
    finished_at: str | None
    updated_at: str


@dataclass
class VerificationRecord:
    id: int

    target_type: VerificationTargetType
    target_id: int

    status: VerificationStatus

    reason: str | None
    evidence: list[str]

    created_at: str


@dataclass
class PlanRecord:
    id: int
    version: int

    user_request: str
    global_goal: str

    status: PlanStatus

    created_at: str
    updated_at: str

    project_id: str | None = None
    canonical_source_root: str | None = None

class ReplanScope(str, Enum):
    ACTION = "ACTION"
    STEP = "STEP"
    TASK = "TASK"
    GLOBAL = "GLOBAL"


class ReplanTrigger(str, Enum):
    VERIFICATION_FAIL = "VERIFICATION_FAIL"
    ATTEMPT_LIMIT = "ATTEMPT_LIMIT"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    USER_CHANGE = "USER_CHANGE"
    INVALID_ASSUMPTION = "INVALID_ASSUMPTION"


class ReplanTargetType(str, Enum):
    STEP = "STEP"
    TASK = "TASK"
    PLAN = "PLAN"


@dataclass(frozen=True)
class ReplanDecision:
    scope: ReplanScope
    trigger: ReplanTrigger

    target_type: ReplanTargetType
    target_id: int

    reason: str


@dataclass
class ReplanRecord:
    id: int

    scope: ReplanScope
    trigger: ReplanTrigger

    target_type: ReplanTargetType
    target_id: int

    reason: str

    created_at: str
