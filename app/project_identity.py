from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


class ProjectIdentityError(ValueError):
    """Raised when a source root cannot identify a project safely."""


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    canonical_source_root: Path
    comparison_root: str
    project_id: str

    @classmethod
    def from_source_root(
        cls,
        source_root: str | Path,
    ) -> "ProjectIdentity":
        try:
            root = Path(source_root).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ProjectIdentityError(
                f"Project root cannot be resolved: {source_root}"
            ) from error

        if not root.is_dir():
            raise ProjectIdentityError(
                f"Project root is not a directory: {root}"
            )

        display_root = str(root)
        comparison_root = (
            os.path.normcase(display_root)
            if os.name == "nt"
            else display_root
        )
        project_id = hashlib.sha256(
            comparison_root.encode("utf-8")
        ).hexdigest()

        return cls(
            canonical_source_root=root,
            comparison_root=comparison_root,
            project_id=project_id,
        )
