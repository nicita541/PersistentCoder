from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.sandbox.project_path import ProjectPath


def _translate_glob(pattern: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 1
                if index + 1 < len(pattern) and pattern[index + 1] == "/":
                    index += 1
                    output.append("(?:.*/)?")
                else:
                    output.append(".*")
            else:
                output.append("[^/]*")
        elif char == "?":
            output.append("[^/]")
        elif char == "[":
            closing = pattern.find("]", index + 1)
            if closing == -1:
                output.append(r"\[")
            else:
                group = pattern[index + 1 : closing]
                if group.startswith("!"):
                    group = "^" + group[1:]
                output.append("[" + group + "]")
                index = closing
        else:
            output.append(re.escape(char))
        index += 1
    return "".join(output)


@dataclass(frozen=True, slots=True)
class _Rule:
    expression: re.Pattern[str]
    negated: bool

    def matches(self, value: str) -> bool:
        return self.expression.fullmatch(value) is not None


class GitIgnoreMatcher:
    def __init__(self, rules: tuple[_Rule, ...] = ()) -> None:
        self._rules = rules

    @classmethod
    def from_project(cls, root: str | Path) -> "GitIgnoreMatcher":
        ignore_file = Path(root) / ".gitignore"
        if not ignore_file.is_file() or ignore_file.is_symlink():
            return cls()

        rules: list[_Rule] = []
        try:
            lines = ignore_file.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return cls()

        for line in lines:
            value = line.rstrip()
            if not value or value.lstrip().startswith("#"):
                continue
            negated = value.startswith("!")
            if negated:
                value = value[1:]
            if not value:
                continue

            anchored = value.startswith("/")
            if anchored:
                value = value[1:]
            value = value.rstrip("/").replace("\\", "/")
            if not value:
                continue

            translated = _translate_glob(value)
            if anchored or "/" in value:
                prefix = "^"
            else:
                prefix = r"^(?:.*/)?"
            expression = re.compile(prefix + translated + r"(?:/.*)?$")
            rules.append(_Rule(expression, negated))

        return cls(tuple(rules))

    def is_ignored(
        self,
        path: ProjectPath,
        *,
        is_dir: bool = False,
    ) -> bool:
        del is_dir  # Matching descendants makes directory rules deterministic.
        ignored = False
        for rule in self._rules:
            if rule.matches(path.value):
                ignored = not rule.negated
        return ignored
