from __future__ import annotations

from dataclasses import dataclass

from app.sandbox.policy import PolicyViolation


class LimitExceeded(PolicyViolation):
    """
    A framework-controlled resource limit was exceeded.

    Fail closed: the offending action is refused and the caller
    must roll the attempt back.
    """


@dataclass(frozen=True)
class SandboxLimits:
    """
    Resource limits owned by the framework (never by the model).

    Protects the host PC from resource abuse / disk-filling by
    generated code, and keeps a small local model from drowning in
    repository content.
    """

    # Writes
    max_file_bytes: int = 1_000_000
    max_workspace_bytes: int = 64_000_000
    max_patch_bytes: int = 4_000_000

    # Initial/checkpoint snapshots
    max_snapshot_files: int = 20_000
    max_snapshot_bytes: int = 256_000_000
    max_snapshot_file_bytes: int = 8_000_000

    # Reads / observe loop
    max_read_bytes: int = 200_000
    max_files_read: int = 40
    max_observe_bytes: int = 400_000
    max_tool_iterations: int = 8

    # Commands
    max_output_bytes: int = 64_000
    command_timeout: int = 180
    max_command_workspace_bytes: int = 64_000_000


DEFAULT_LIMITS = SandboxLimits()
