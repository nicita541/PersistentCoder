from __future__ import annotations

from app.agent.state import ExecutionResult


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
    ) -> ExecutionResult:
        return self.executor.execute(
            task,
            step=step,
            feedback=feedback,
        )

