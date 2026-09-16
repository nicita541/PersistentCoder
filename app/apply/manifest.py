from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from app.sandbox.project_path import ProjectPath


class ManifestValidationError(ValueError):
    """Raised when a manifest cannot be trusted as canonical content."""


class PatchOperation(str, Enum):
    ADD = "ADD"
    MODIFY = "MODIFY"
    DELETE = "DELETE"


_SHA256_LENGTH = 64
_MAX_ID_LENGTH = 128
_MAX_REASONS = 8
_MAX_REASON_LENGTH = 200
_ENTRY_KEYS = frozenset(
    {
        "path",
        "operation",
        "before_sha256",
        "after_sha256",
        "before_size",
        "after_size",
        "reviewable",
        "apply_safe",
        "reasons",
    }
)
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "manifest_id",
        "project_id",
        "canonical_source_root",
        "session_id",
        "baseline_sha256",
        "workspace_sha256",
        "verification_id",
        "entries",
    }
)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _require_sha256(value: object, field: str) -> str:
    if not _is_sha256(value):
        raise ManifestValidationError(f"{field} must be a lowercase SHA-256")
    return value


def _require_id(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_ID_LENGTH
        or value != value.strip()
        or any(ord(character) < 32 for character in value)
    ):
        raise ManifestValidationError(f"{field} is invalid")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _project_id_for_root(root: str) -> str:
    comparison_root = os.path.normcase(root) if os.name == "nt" else root
    return hashlib.sha256(comparison_root.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PatchEntry:
    path: ProjectPath | str
    operation: PatchOperation
    before_sha256: str | None
    after_sha256: str | None
    before_size: int | None
    after_size: int | None
    reviewable: bool
    apply_safe: bool
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            path = ProjectPath.parse(
                self.path.value
                if isinstance(self.path, ProjectPath)
                else self.path
            )
        except (TypeError, ValueError) as error:
            raise ManifestValidationError("entry path is invalid") from error
        object.__setattr__(self, "path", path)

        if not isinstance(self.operation, PatchOperation):
            raise ManifestValidationError("operation must be a PatchOperation")
        if type(self.reviewable) is not bool or type(self.apply_safe) is not bool:
            raise ManifestValidationError("reviewable and apply_safe must be booleans")
        if self.apply_safe and not self.reviewable:
            raise ManifestValidationError("apply-safe entries must be reviewable")

        hashes_and_sizes = (
            (self.before_sha256, self.before_size, "before"),
            (self.after_sha256, self.after_size, "after"),
        )
        for digest, size, side in hashes_and_sizes:
            if (digest is None) != (size is None):
                raise ManifestValidationError(
                    f"{side} hash and size must both be present or absent"
                )
            if digest is not None:
                _require_sha256(digest, f"{side}_sha256")
                if type(size) is not int or size < 0:
                    raise ManifestValidationError(f"{side}_size is invalid")

        before_present = self.before_sha256 is not None
        after_present = self.after_sha256 is not None
        expected = {
            PatchOperation.ADD: (False, True),
            PatchOperation.MODIFY: (True, True),
            PatchOperation.DELETE: (True, False),
        }[self.operation]
        if (before_present, after_present) != expected:
            raise ManifestValidationError(
                f"hash presence is inconsistent with {self.operation.value}"
            )

        if not isinstance(self.reasons, (tuple, list)):
            raise ManifestValidationError("reasons must be a bounded sequence")
        reasons = tuple(self.reasons)
        if len(reasons) > _MAX_REASONS:
            raise ManifestValidationError("too many entry reasons")
        for reason in reasons:
            if (
                not isinstance(reason, str)
                or not reason
                or reason != reason.strip()
                or len(reason) > _MAX_REASON_LENGTH
                or any(ord(character) < 32 for character in reason)
            ):
                raise ManifestValidationError("entry reason is invalid")
        object.__setattr__(self, "reasons", reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path.value,
            "operation": self.operation.value,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "before_size": self.before_size,
            "after_size": self.after_size,
            "reviewable": self.reviewable,
            "apply_safe": self.apply_safe,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, value: object) -> "PatchEntry":
        if not isinstance(value, dict) or set(value) != _ENTRY_KEYS:
            raise ManifestValidationError("entry fields are not canonical")
        raw_path = value["path"]
        try:
            canonical_path = ProjectPath.parse(raw_path)
        except (TypeError, ValueError) as error:
            raise ManifestValidationError("entry path is invalid") from error
        if not isinstance(raw_path, str) or canonical_path.value != raw_path:
            raise ManifestValidationError("serialized entry path is not canonical")
        try:
            operation = PatchOperation(value["operation"])
        except (TypeError, ValueError) as error:
            raise ManifestValidationError("entry operation is invalid") from error
        try:
            return cls(
                path=canonical_path,
                operation=operation,
                before_sha256=value["before_sha256"],
                after_sha256=value["after_sha256"],
                before_size=value["before_size"],
                after_size=value["after_size"],
                reviewable=value["reviewable"],
                apply_safe=value["apply_safe"],
                reasons=value["reasons"],
            )
        except (TypeError, ValueError) as error:
            if isinstance(error, ManifestValidationError):
                raise
            raise ManifestValidationError("entry is invalid") from error


@dataclass(frozen=True, slots=True)
class PatchManifest:
    manifest_id: str
    project_id: str
    canonical_source_root: str
    session_id: str
    baseline_sha256: str
    workspace_sha256: str
    verification_id: str
    entries: tuple[PatchEntry, ...]

    SCHEMA_VERSION = 1

    def __post_init__(self) -> None:
        _require_sha256(self.manifest_id, "manifest_id")
        _require_sha256(self.project_id, "project_id")
        _require_sha256(self.baseline_sha256, "baseline_sha256")
        _require_sha256(self.workspace_sha256, "workspace_sha256")
        _require_id(self.session_id, "session_id")
        _require_id(self.verification_id, "verification_id")

        if not isinstance(self.canonical_source_root, str):
            raise ManifestValidationError("canonical_source_root is invalid")
        root = Path(self.canonical_source_root)
        try:
            resolved_root = root.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise ManifestValidationError("canonical_source_root is invalid") from error
        if not root.is_absolute() or str(resolved_root) != self.canonical_source_root:
            raise ManifestValidationError("canonical_source_root is not canonical")
        if _project_id_for_root(self.canonical_source_root) != self.project_id:
            raise ManifestValidationError("project identity does not match source root")

        if not isinstance(self.entries, (tuple, list)):
            raise ManifestValidationError("entries must be an ordered sequence")
        entries = tuple(self.entries)
        if any(not isinstance(entry, PatchEntry) for entry in entries):
            raise ManifestValidationError("entries must contain PatchEntry values")
        paths = [entry.path.value for entry in entries]
        if paths != sorted(paths):
            raise ManifestValidationError("manifest paths must be sorted")
        keys = [entry.path.comparison_key for entry in entries]
        if len(keys) != len(set(keys)):
            raise ManifestValidationError("manifest paths must be unique")
        object.__setattr__(self, "entries", entries)

        if self.manifest_id != self._computed_manifest_id():
            raise ManifestValidationError("manifest identity does not match content")

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        canonical_source_root: str,
        session_id: str,
        baseline_sha256: str,
        workspace_sha256: str,
        verification_id: str,
        entries: tuple[PatchEntry, ...] | list[PatchEntry],
    ) -> "PatchManifest":
        values = {
            "schema_version": cls.SCHEMA_VERSION,
            "project_id": project_id,
            "canonical_source_root": canonical_source_root,
            "session_id": session_id,
            "baseline_sha256": baseline_sha256,
            "workspace_sha256": workspace_sha256,
            "verification_id": verification_id,
            "entries": [entry.to_dict() for entry in entries],
        }
        manifest_id = hashlib.sha256(_canonical_json(values).encode("utf-8")).hexdigest()
        return cls(
            manifest_id=manifest_id,
            project_id=project_id,
            canonical_source_root=canonical_source_root,
            session_id=session_id,
            baseline_sha256=baseline_sha256,
            workspace_sha256=workspace_sha256,
            verification_id=verification_id,
            entries=tuple(entries),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PatchManifest":
        if not isinstance(value, dict) or set(value) != _MANIFEST_KEYS:
            raise ManifestValidationError("manifest fields are not canonical")
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != cls.SCHEMA_VERSION
        ):
            raise ManifestValidationError("manifest schema version is unsupported")
        if not isinstance(value["entries"], list):
            raise ManifestValidationError("manifest entries are not canonical")
        try:
            entries = tuple(PatchEntry.from_dict(item) for item in value["entries"])
            return cls(
                manifest_id=value["manifest_id"],
                project_id=value["project_id"],
                canonical_source_root=value["canonical_source_root"],
                session_id=value["session_id"],
                baseline_sha256=value["baseline_sha256"],
                workspace_sha256=value["workspace_sha256"],
                verification_id=value["verification_id"],
                entries=entries,
            )
        except (TypeError, ValueError) as error:
            if isinstance(error, ManifestValidationError):
                raise
            raise ManifestValidationError("manifest is invalid") from error

    @classmethod
    def from_json(cls, value: str) -> "PatchManifest":
        if not isinstance(value, str):
            raise ManifestValidationError("manifest JSON must be text")
        try:
            decoded = json.loads(value, object_pairs_hook=_reject_duplicate_keys)
        except ManifestValidationError:
            raise
        except (json.JSONDecodeError, TypeError) as error:
            raise ManifestValidationError("manifest JSON is invalid") from error
        return cls.from_dict(decoded)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "manifest_id": self.manifest_id,
            "project_id": self.project_id,
            "canonical_source_root": self.canonical_source_root,
            "session_id": self.session_id,
            "baseline_sha256": self.baseline_sha256,
            "workspace_sha256": self.workspace_sha256,
            "verification_id": self.verification_id,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    def _computed_manifest_id(self) -> str:
        payload = self.to_dict()
        payload.pop("manifest_id")
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
