from __future__ import annotations

from typing import Protocol

from app.tools.terminal_tools import CommandResult


class ProjectToolRunner(Protocol):
    """Execution boundary used by model-selected typed tools.

    Implementations receive argv assembled by trusted application code, never a
    shell command supplied by the model.  The current implementation is the
    Docker sandbox runner.  A future host implementation must enforce its file
    boundary at the operating-system/process level; setting ``cwd`` is not a
    security boundary.
    """

    def run_argv(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        cancellation_token=None,
    ) -> CommandResult: ...
