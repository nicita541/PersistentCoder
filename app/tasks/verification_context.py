from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from app.sandbox.limits import DEFAULT_LIMITS, LimitExceeded
from app.sandbox.project_path import ProjectPath
from app.tasks.verification_spec import VerificationSpec


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _workspace_digest(workspace_root: str | Path) -> str:
    """Hash the exact persistent workspace copied to command `/input`."""

    root = Path(workspace_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"verification workspace is not a directory: {root}")
    entries: list[dict[str, object]] = []
    total = 0

    def walk(directory: Path, prefix: tuple[str, ...] = ()) -> None:
        nonlocal total
        for child in sorted(os.scandir(directory), key=lambda item: item.name):
            path = ProjectPath.parse("/".join((*prefix, child.name)))
            metadata = child.stat(follow_symlinks=False)
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if child.is_symlink() or attributes & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
            ):
                raise ValueError(
                    f"verification workspace contains a link: {path.value}"
                )
            if child.is_dir(follow_symlinks=False):
                walk(Path(child.path), (*prefix, child.name))
                continue
            if not child.is_file(follow_symlinks=False):
                raise ValueError(
                    f"verification workspace entry is unsupported: {path.value}"
                )
            size = int(metadata.st_size)
            if size > DEFAULT_LIMITS.max_snapshot_file_bytes:
                raise LimitExceeded(
                    f"verification file exceeds limit: {path.value}"
                )
            if len(entries) + 1 > DEFAULT_LIMITS.max_snapshot_files:
                raise LimitExceeded("verification file count exceeds limit")
            total += size
            if total > DEFAULT_LIMITS.max_snapshot_bytes:
                raise LimitExceeded("verification workspace exceeds byte limit")
            content = Path(child.path).read_bytes()
            if len(content) != size:
                raise RuntimeError(
                    f"verification workspace changed while hashing: {path.value}"
                )
            entries.append(
                {
                    "path": path.value,
                    "size": size,
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )

    walk(root)
    entries.sort(key=lambda item: str(item["path"]))
    return _digest(entries)


@dataclass(frozen=True, slots=True)
class VerificationContext:
    """Immutable identity of what was checked, how, and where."""

    workspace_digest: str
    spec_digest: str
    environment_digest: str

    @classmethod
    def capture(
        cls,
        workspace_root: str | Path,
        *,
        specs: Sequence[VerificationSpec],
        criteria: Sequence[str] = (),
        environment: Mapping[str, object],
    ) -> "VerificationContext":
        spec_payload = {
            "specs": [spec.to_dict() for spec in specs],
            "legacy_criteria": [str(item).strip() for item in criteria],
        }
        return cls(
            workspace_digest=_workspace_digest(workspace_root),
            spec_digest=_digest(spec_payload),
            environment_digest=_digest(dict(environment)),
        )

    def matches(
        self,
        workspace_root: str | Path,
        *,
        specs: Sequence[VerificationSpec],
        criteria: Sequence[str] = (),
        environment: Mapping[str, object],
    ) -> bool:
        try:
            current = self.capture(
                workspace_root,
                specs=specs,
                criteria=criteria,
                environment=environment,
            )
        except (OSError, RuntimeError, ValueError):
            return False
        return current == self

    def to_dict(self) -> dict[str, str]:
        return {
            "workspace_digest": self.workspace_digest,
            "spec_digest": self.spec_digest,
            "environment_digest": self.environment_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "VerificationContext":
        return cls(
            workspace_digest=str(value["workspace_digest"]),
            spec_digest=str(value["spec_digest"]),
            environment_digest=str(value["environment_digest"]),
        )
