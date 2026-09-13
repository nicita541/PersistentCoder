from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


# ==========================================
# CONTEXT LIMITS (framework-owned)
# ==========================================


@dataclass(frozen=True)
class ContextLimits:
    """
    Bounds for repository context selection.

    A small local model must never receive the whole repository, and
    a SHORT prompt matters twice: weak models follow the action
    protocol far better with it, and every extra kilobyte costs
    prefill time on CPU.
    """

    max_candidate_files: int = 40
    max_selected_files: int = 3
    max_file_bytes: int = 3_000
    max_total_context_bytes: int = 7_000
    max_search_terms: int = 4
    max_search_results: int = 16


DEFAULT_CONTEXT_LIMITS = ContextLimits()


PATH_PATTERN = re.compile(
    r"[A-Za-z0-9_\-./\\]+"
    r"\.(?:py|pyi|txt|md|json|toml|cfg|ini|ya?ml|sql)"
)

IDENTIFIER_PATTERN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]{3,}"
)

IGNORED_IDENTIFIERS = frozenset(
    {
        "self", "none", "true", "false", "return",
        "string", "object", "import", "class", "def",
        "assert", "print", "json", "with", "that", "this",
        "from", "test", "tests", "code", "file", "files",
        "task", "step", "plan", "does", "should", "must",
        "user", "users", "make", "need", "please",
    }
)


@dataclass(frozen=True)
class RepoContext:
    """
    Relevance-selected, bounded view of the repository.
    """

    files: tuple[str, ...]
    text: str
    candidates: tuple[str, ...] = ()
    scores: tuple[tuple[str, float], ...] = ()

    def is_empty(self) -> bool:
        return not self.files


def _normalize_path(
    raw: str,
) -> str:
    return (
        raw.strip()
        .strip("\"'`")
        .replace("\\", "/")
        .lstrip("./")
    )


class RepoContextSelector:
    """
    Deterministic repository context selector.

    Signals (no embeddings, no second LLM):

      - Task title/description/requires/produces/criteria;
      - current Step;
      - previous failure feedback;
      - explicit file paths mentioned by the model;
      - identifiers / symbols / module names;
      - source <-> test counterparts;
      - files already touched in this run.

    The result is bounded by ContextLimits and contains only
    sandbox-relative paths.
    """

    def __init__(
        self,
        project,
        *,
        limits: ContextLimits = (
            DEFAULT_CONTEXT_LIMITS
        ),
    ) -> None:
        self.project = project
        self.limits = limits

    # ==================================
    # SIGNALS
    # ==================================

    def task_text(
        self,
        *,
        task=None,
        step=None,
        feedback: str | None = None,
        extra: str = "",
    ) -> str:
        parts: list[str] = []

        if task is not None:
            for attribute in (
                "key",
                "title",
                "description",
            ):
                value = getattr(
                    task,
                    attribute,
                    None,
                )

                if value:
                    parts.append(str(value))

            for attribute in (
                "requires",
                "produces",
                "success_criteria",
                "external_dependencies",
            ):
                value = getattr(
                    task,
                    attribute,
                    None,
                ) or []

                parts.extend(
                    str(item)
                    for item in value
                )

        if step is not None:
            for attribute in (
                "title",
                "description",
            ):
                value = getattr(
                    step,
                    attribute,
                    None,
                )

                if value:
                    parts.append(str(value))

            parts.extend(
                str(item)
                for item in (
                    getattr(
                        step,
                        "success_criteria",
                        None,
                    )
                    or []
                )
            )

        if feedback:
            parts.append(str(feedback))

        if extra:
            parts.append(str(extra))

        return "\n".join(parts)

    def explicit_paths(
        self,
        text: str,
    ) -> set[str]:
        return {
            _normalize_path(raw)
            for raw in PATH_PATTERN.findall(
                text
            )
        }

    def identifiers(
        self,
        text: str,
    ) -> set[str]:
        found: set[str] = set()

        for raw in IDENTIFIER_PATTERN.findall(
            text
        ):
            lowered = raw.casefold()

            if lowered in IGNORED_IDENTIFIERS:
                continue

            found.add(lowered)

            for part in re.findall(
                r"[A-Z][a-z0-9]{2,}",
                raw,
            ):
                found.add(part.casefold())

        return found

    # ==================================
    # CANDIDATE SELECTION
    # ==================================

    @staticmethod
    def _stem(
        path: str,
    ) -> str:
        return path.rsplit("/", 1)[-1].rsplit(
            ".",
            1,
        )[0].casefold()

    @staticmethod
    def _parts(
        path: str,
    ) -> set[str]:
        return {
            part.casefold()
            for part in re.split(
                r"[/.\\]",
                path,
            )
            if part
        }

    def _match_score(
        self,
        path: str,
        *,
        identifiers: set[str],
        explicit: set[str],
        touched: set[str],
    ) -> float:
        score = 0.0

        if path in explicit:
            score += 100.0

        stem = self._stem(path)
        parts = self._parts(path)

        if stem in identifiers:
            score += 50.0

        for identifier in identifiers:
            if len(identifier) < 4:
                continue

            if identifier in stem:
                score += 12.0
            elif identifier in parts:
                score += 8.0

        if path in touched:
            score += 35.0

        if path.endswith(".py"):
            score += 5.0

        return score

    def candidate_paths(
        self,
        *,
        task=None,
        step=None,
        feedback: str | None = None,
        explicit_paths=(),
        touched=(),
        extra: str = "",
    ) -> list[str]:
        text = self.task_text(
            task=task,
            step=step,
            feedback=feedback,
            extra=extra,
        )

        identifiers = self.identifiers(text)
        explicit = (
            self.explicit_paths(text)
            | {
                _normalize_path(path)
                for path in explicit_paths
            }
        )

        all_files = self.project.list_files()
        known = set(all_files)

        touched_set = {
            _normalize_path(path)
            for path in touched
        }

        candidates: list[str] = []

        def add(path: str) -> None:
            if (
                path in known
                and path not in candidates
            ):
                candidates.append(path)

        for path in explicit:
            if path in known:
                add(path)

        for path in touched_set:
            add(path)

        for path in all_files:
            if len(candidates) >= (
                self.limits.max_candidate_files
            ):
                break

            stem = self._stem(path)
            parts = self._parts(path)

            if stem in identifiers or (
                parts & identifiers
            ):
                add(path)

        # Textual search: task/step symbols really present in code.
        search_terms = sorted(
            (
                identifier
                for identifier in identifiers
                if len(identifier) >= 5
            ),
            key=lambda item: (-len(item), item),
        )[: self.limits.max_search_terms]

        for term in search_terms:
            for hit in self.project.search(
                term,
                max_results=(
                    self.limits.max_search_results
                ),
            ):
                add(hit.split(":", 1)[0])

        return candidates[
            : self.limits.max_candidate_files
        ]

    def select_paths(
        self,
        *,
        task=None,
        step=None,
        feedback: str | None = None,
        explicit_paths=(),
        touched=(),
        max_files: int | None = None,
    ) -> list[str]:
        """
        Cheap variant: relevant paths only (no file contents).
        """

        limit = (
            max_files
            or self.limits.max_selected_files
        )

        text = self.task_text(
            task=task,
            step=step,
            feedback=feedback,
        )

        identifiers = self.identifiers(text)
        explicit = (
            self.explicit_paths(text)
            | {
                _normalize_path(path)
                for path in explicit_paths
            }
        )

        candidates = self.candidate_paths(
            task=task,
            step=step,
            feedback=feedback,
            explicit_paths=explicit_paths,
            touched=touched,
        )

        all_files = self.project.list_files()

        def counterpart_score(
            path: str,
        ) -> float:
            stem = self._stem(path)

            if stem.startswith("test_"):
                return 0.0

            for other in all_files:
                other_stem = self._stem(other)

                if other_stem == f"test_{stem}":
                    return 25.0

                if other_stem == f"{stem}_test":
                    return 25.0

            return 0.0

        scored = sorted(
            (
                (
                    self._match_score(
                        path,
                        identifiers=identifiers,
                        explicit=explicit,
                        touched={
                            _normalize_path(item)
                            for item in touched
                        },
                    )
                    + counterpart_score(path),
                    path,
                )
                for path in candidates
            ),
            key=lambda item: (-item[0], item[1]),
        )

        selected: list[str] = []

        for score, path in scored:
            if score <= 0:
                continue

            selected.append(path)

            if len(selected) >= limit:
                break

        # Explicit paths are never dropped by the bound.
        for path in explicit:
            if (
                path in set(all_files)
                and path not in selected
            ):
                selected.append(path)

        return selected


    def _listing_text(
        self,
        paths: list[str],
    ) -> str:
        if not paths:
            return ""

        lines = [
            "RELEVANT PROJECT FILES (paths only):"
        ]

        lines.extend(
            f"- {path}"
            for path in paths[
                : self.limits.max_candidate_files
            ]
        )

        lines.append(
            "No existing file was referenced by this task, so no "
            "file content is included. READ a file before editing it."
        )

        return "\n".join(lines)

    def select(
        self,
        *,
        task=None,
        step=None,
        feedback: str | None = None,
        explicit_paths=(),
        touched=(),
        extra: str = "",
    ) -> RepoContext:
        """
        Relevance-selected repository context for ONE coding call.

        Reads only the selected files, bounded by max_file_bytes and
        max_total_context_bytes.
        """

        paths = self.select_paths(
            task=task,
            step=step,
            feedback=feedback,
            explicit_paths=explicit_paths,
            touched=touched,
        )

        candidates = self.candidate_paths(
            task=task,
            step=step,
            feedback=feedback,
            explicit_paths=explicit_paths,
            touched=touched,
            extra=extra,
        )

        text = self.task_text(
            task=task,
            step=step,
            feedback=feedback,
            extra=extra,
        )

        explicit = self.explicit_paths(text) | {
            _normalize_path(path)
            for path in explicit_paths
        }

        if explicit:
            # Reference-driven: include ONLY the referenced files
            # (when they exist), their test counterparts and files
            # already touched in this run. Unrelated search hits stay
            # out of the prompt entirely.
            allowed: list[str] = []

            existing = set(
                self.project.list_files()
            )

            touched_set = {
                _normalize_path(path)
                for path in touched
            }

            for path in explicit:
                if path in existing:
                    allowed.append(path)

            for candidate in paths:
                if candidate in touched_set:
                    allowed.append(candidate)

            stems = {
                Path(path).stem.casefold()
                for path in allowed
            }

            for candidate in paths:
                if candidate in allowed:
                    continue

                stem = Path(candidate).stem.casefold()

                if not stem.startswith("test_"):
                    continue

                remainder = stem[
                    len("test_"):
                ]

                if any(
                    declared in remainder
                    or remainder in declared
                    for declared in stems
                ):
                    allowed.append(candidate)

            paths = allowed[
                : self.limits.max_selected_files
            ]

        else:
            # The task references no existing file: send the relevant
            # PATHS only. Dumping unrelated file bodies makes a small
            # model "helpfully" rewrite them.
            return RepoContext(
                files=(),
                text=self._listing_text(paths),
                candidates=tuple(candidates),
            )

        files: list[str] = []
        rendered: list[str] = []
        used = 0

        for path in paths:
            try:
                content = self.project.read(path)

            except Exception:
                continue

            encoded = content.encode("utf-8")

            if len(encoded) > self.limits.max_file_bytes:
                content = encoded[
                    : self.limits.max_file_bytes
                ].decode(
                    "utf-8",
                    errors="ignore",
                )

                content += "\n... (truncated)"

            block = f"FILE: {path}\n{content}"

            size = len(block.encode("utf-8"))

            if (
                used + size
                > self.limits.max_total_context_bytes
            ):
                if files:
                    break

                block = block[
                    : self.limits.max_total_context_bytes
                ]

                size = len(block.encode("utf-8"))

            files.append(path)
            rendered.append(block)
            used += size

        if not files:
            return RepoContext(
                files=(),
                text="",
                candidates=tuple(candidates),
            )

        header = (
            "RELEVANT PROJECT CONTEXT "
            "(deterministic relevance selection, bounded):\n"
            f"selected files: {len(files)}\n"
            f"candidate files: {len(candidates)}\n"
            "Editing a file that already exists still requires "
            "READING it first."
        )

        return RepoContext(
            files=tuple(files),
            text=(
                header
                + "\n\n"
                + "\n\n".join(rendered)
            ),
            candidates=tuple(candidates),
        )
