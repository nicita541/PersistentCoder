from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.sandbox.project_path import ProjectPath, ProjectPathError
from app.tasks.change_scope import AllowedChangeSet


CONTENT_KEYS = (
    "content",
    "code",
    "file_content",
    "source",
    "body",
    "text",
    "new_content",
)

EDIT_ALIASES = frozenset(
    {
        "edit",
        "write",
        "create",
        "update",
        "apply",
        "patch",
        "modify",
        "save",
        "create_update_files",
        "create_or_update_files",
        "write_files",
    }
)

_TRIPLE_CONTENT = re.compile(
    r'("content"\s*:\s*)"""(.*?)"""',
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class ProtocolError(ValueError):
    code: str
    detail: str

    def __str__(self) -> str:
        return self.detail


def _strip_fence(raw: str) -> str:
    text = (raw or "").strip()
    if not text.startswith("```"):
        return text
    newline = text.find("\n")
    if newline < 0:
        return text
    body = text[newline + 1 :].rstrip()
    if body.endswith("```"):
        body = body[:-3]
    return body.strip()


def _extract_object(text: str) -> dict[str, object]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ProtocolError("INVALID_JSON", "model did not return valid JSON")


def _repair_triple_content(text: str) -> str:
    # This is deliberately not a Python parser. Only the exact, observed
    # Qwen deviation for a JSON `content` string is accepted.
    return _TRIPLE_CONTENT.sub(
        lambda match: match.group(1) + json.dumps(match.group(2)),
        text,
    )


def _content_key(entry: dict[str, object]) -> str | None:
    for key in CONTENT_KEYS:
        if isinstance(entry.get(key), str):
            return key
    return None


class ActionEnvelopeDecoder:
    """Narrow, deterministic decoder for CodingAgent model output."""

    def decode(
        self,
        raw: str,
        *,
        allowed_changes: AllowedChangeSet | None = None,
    ) -> dict[str, object]:
        text = _strip_fence(raw)
        try:
            proposal = _extract_object(text)
        except ProtocolError as first_error:
            repaired = _repair_triple_content(text)
            if repaired == text:
                raise first_error
            proposal = _extract_object(repaired)

        action = self._action_of(proposal)
        normalized = self._normalize(proposal, action)
        self._normalize_paths(normalized, allowed_changes)
        return normalized

    @staticmethod
    def _action_of(proposal: dict[str, object]) -> str | None:
        raw_action = proposal.get("action")
        if isinstance(raw_action, str) and raw_action.strip():
            action = raw_action.strip().casefold()
            if action in {"list", "ls", "list_files", "tree"}:
                return "list"
            if action in {"read", "read_file", "open", "cat"}:
                return "read"
            if action in {"search", "grep", "find", "find_symbol"}:
                return "search"
            if action in EDIT_ALIASES:
                return "edit"
            if action in {"delete", "delete_file", "remove_file"}:
                return "delete"
            return action
        if "files" in proposal or "commands" in proposal:
            return "edit"
        if isinstance(proposal.get("file"), dict):
            return "edit"
        if isinstance(proposal.get("path"), str) and _content_key(proposal):
            return "edit"
        if any(
            isinstance(proposal.get(key), str)
            for key in ("query", "needle", "text")
        ):
            return "search"
        return None

    @staticmethod
    def _normalize(
        proposal: dict[str, object], action: str | None
    ) -> dict[str, object]:
        if action == "delete":
            path = proposal.get("path")
            if not isinstance(path, str):
                raise ProtocolError("MISSING_PATH", "delete requires a path")
            return {
                "action": "edit",
                "files": [{"path": path, "operation": "delete"}],
                "commands": [],
            }

        normalized = dict(proposal)
        normalized["action"] = action
        if action != "edit":
            return normalized

        files = normalized.get("files")
        if isinstance(files, dict):
            files = [
                {"path": path, "content": content}
                for path, content in files.items()
            ]

        if files is None and "commands" not in normalized:
            entry = normalized.get("file")
            if isinstance(entry, str):
                key = _content_key(normalized)
                entry = (
                    {"path": entry, "content": normalized[key]}
                    if key
                    else None
                )
            if not isinstance(entry, dict):
                key = _content_key(normalized)
                entry = (
                    {"path": normalized["path"], "content": normalized[key]}
                    if isinstance(normalized.get("path"), str) and key
                    else None
                )
            if entry is not None:
                files = [entry]

        if files is not None:
            normalized["files"] = files
        return normalized

    def _normalize_paths(
        self,
        proposal: dict[str, object],
        allowed_changes: AllowedChangeSet | None,
    ) -> None:
        files = proposal.get("files")
        if not isinstance(files, list):
            return
        for entry in files:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                continue
            entry["path"] = self._normalize_one_path(
                entry["path"], allowed_changes
            )

    @staticmethod
    def _normalize_one_path(
        raw: str,
        allowed_changes: AllowedChangeSet | None,
    ) -> str:
        try:
            path = ProjectPath.parse(raw)
        except ProjectPathError as error:
            raise ProtocolError("INVALID_PATH", str(error)) from error

        if allowed_changes is None or not path.value.startswith("root/"):
            return path.value

        direct_keys = {p.comparison_key for p in allowed_changes.step_paths}
        if path.comparison_key in direct_keys:
            return path.value

        suffix = path.value[len("root/") :]
        suffix_key = ProjectPath.parse(suffix).comparison_key
        candidates = [
            candidate
            for candidate in allowed_changes.step_paths
            if candidate.comparison_key == suffix_key
            or candidate.comparison_key.endswith("/" + suffix_key)
        ]
        if len(candidates) == 1:
            return candidates[0].value
        if len(candidates) > 1:
            raise ProtocolError(
                "AMBIGUOUS_ROOT_PATH",
                f"root/ path is ambiguous in the active scope: {path.value}",
            )
        return path.value
