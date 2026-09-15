from __future__ import annotations

from app.agent.state import ExecutionResult
from app.tasks.change_scope import AllowedChangeSet


class CodingAgent:
    """
    Управляющий агент выполнения.

    Не содержит копий LLM/Context/Tools —
    только использует их через CodeExecutor.
    """

    def __init__(self, executor) -> None:
        self.executor = executor

    def execute(
        self,
        task,
        *,
        step=None,
        plan_id: int | None = None,
        feedback: str | None = None,
        allowed_changes: AllowedChangeSet | None = None,
        cancellation_token=None,
    ) -> ExecutionResult:
        return self.executor.execute(
            task,
            step=step,
            feedback=feedback,
            allowed_changes=allowed_changes,
            cancellation_token=cancellation_token,
        )

