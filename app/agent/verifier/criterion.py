from __future__ import annotations

import re

from app.agent.state import CriterionResult


_FILE_TOKEN_RE = re.compile(
    r"[A-Za-z0-9_\-./\\]+\.(?:py|txt|md|json|cfg|toml|ini|ya?ml|js|ts|html|css|csv|sh)"
)

_IMPORT_TOKEN_RE = re.compile(
    r"import\s+([A-Za-z_][A-Za-z0-9_.]*)",
    re.IGNORECASE,
)


def classify_criterion(
    text: str,
) -> str:
    """
    Deterministic classification of a success criterion into the
    authoritative check CriterionEvaluator would run.

    Returns one of: "py_compile", "import", "pytest", "file_exists",
    or "unknown" (unknown criteria can never auto-PASS).
    """

    raw = (text or "").strip()

    if not raw:
        return "unknown"

    lowered = raw.casefold()

    def _has(
        hints: tuple[str, ...],
    ) -> bool:
        return any(hint in lowered for hint in hints)

    if _has(CriterionEvaluator.SYNTAX_HINTS):
        return "py_compile"

    if _has(CriterionEvaluator.IMPORT_HINTS):
        return "import"

    if (
        _has(CriterionEvaluator.TEST_HINTS)
        and _has(CriterionEvaluator.PASS_HINTS)
    ):
        return "pytest"

    if _has(CriterionEvaluator.EXIST_HINTS):
        if _FILE_TOKEN_RE.search(raw):
            return "file_exists"

        return "unknown"

    return "unknown"


class CriterionEvaluator:
    """
    Maps each success criterion to an AUTHORITATIVE check executed
    inside the sandbox (filesystem / py_compile / import / pytest).

    A criterion is never treated as its own evidence: if no real
    check applies, the criterion is BLOCKED (never auto-PASS).
    """

    EXIST_HINTS = (
        "exist",
        "present",
        "created",
        "available",
        "существует",
        "создан",
    )
    TEST_HINTS = ("test", "pytest")
    PASS_HINTS = (
        "pass",
        "passes",
        "green",
        "succeed",
        "fails",
        "fail",
    )
    SYNTAX_HINTS = ("syntax", "compil", "parse")
    IMPORT_HINTS = ("import",)

    def __init__(
        self,
        *,
        workspace,
        command_runner,
        python: str = "python",
    ) -> None:
        self.workspace = workspace
        self.command_runner = command_runner
        self.python = python
        self._command_cache: dict[
            str,
            tuple[bool, str],
        ] = {}

    def _run(
        self,
        command: str,
    ) -> tuple[bool, str]:
        if command in self._command_cache:
            return self._command_cache[command]

        result = self.command_runner.run(command)

        ok = result.returncode == 0

        output = (
            result.stdout or result.stderr or ""
        ).strip()

        if len(output) > 400:
            output = output[:400] + "..."

        self._command_cache[command] = (
            ok,
            output,
        )

        return ok, output

    @staticmethod
    def _file_token(
        text: str,
    ) -> str | None:
        match = _FILE_TOKEN_RE.search(text)

        if not match:
            return None

        return (
            match.group(0)
            .replace("\\", "/")
            .lstrip("./")
        )

    @staticmethod
    def _has(
        lowered: str,
        hints: tuple[str, ...],
    ) -> bool:
        return any(hint in lowered for hint in hints)

    def evaluate(
        self,
        criterion: str,
    ) -> CriterionResult:
        text = (criterion or "").strip()

        if not text:
            return CriterionResult(
                criterion=criterion,
                status="BLOCKED",
                reason="empty criterion",
            )

        lowered = text.casefold()
        token = self._file_token(text)

        if self._has(lowered, self.SYNTAX_HINTS):
            return self._py_compile(criterion, token)

        if self._has(lowered, self.IMPORT_HINTS):
            return self._import(
                criterion,
                token,
                text,
            )

        if (
            self._has(lowered, self.TEST_HINTS)
            and self._has(lowered, self.PASS_HINTS)
        ):
            return self._pytest(criterion)

        if self._has(lowered, self.EXIST_HINTS):
            return self._file_exists(
                criterion,
                token,
            )

        if token is not None:
            return self._file_exists(
                criterion,
                token,
            )

        return CriterionResult(
            criterion=criterion,
            status="BLOCKED",
            check="none",
            reason=(
                "criterion cannot be verified "
                "automatically"
            ),
        )

    def _file_exists(
        self,
        criterion: str,
        path: str | None,
    ) -> CriterionResult:
        if path is None:
            return CriterionResult(
                criterion=criterion,
                status="BLOCKED",
                check="file_exists",
                reason="no file path in criterion",
            )

        if self.workspace is None:
            return CriterionResult(
                criterion=criterion,
                status="BLOCKED",
                check="file_exists",
                reason="no sandbox workspace",
            )

        try:
            exists = self.workspace.exists(path)

        except Exception as error:
            return CriterionResult(
                criterion=criterion,
                status="FAIL",
                check="file_exists",
                reason=str(error),
            )

        if exists:
            return CriterionResult(
                criterion=criterion,
                status="PASS",
                check="file_exists",
                evidence=[f"file exists: {path}"],
            )

        return CriterionResult(
            criterion=criterion,
            status="FAIL",
            check="file_exists",
            reason=f"file missing: {path}",
            evidence=[f"file missing: {path}"],
        )

    def _py_compile(
        self,
        criterion: str,
        path: str | None,
    ) -> CriterionResult:
        if path is None or self.command_runner is None:
            return CriterionResult(
                criterion=criterion,
                status="BLOCKED",
                check="py_compile",
                reason="no file path / command runner",
            )

        if (
            self.workspace is not None
            and not self.workspace.exists(path)
        ):
            return CriterionResult(
                criterion=criterion,
                status="FAIL",
                check="py_compile",
                reason=f"file missing: {path}",
            )

        ok, output = self._run(
            f'{self.python} -m py_compile "{path}"'
        )

        return CriterionResult(
            criterion=criterion,
            status="PASS" if ok else "FAIL",
            check="py_compile",
            evidence=[
                f"py_compile {path}: "
                f"rc={0 if ok else 1} {output}".strip()
            ],
            reason="" if ok else (
                f"py_compile failed: {output}"
            ),
        )

    def _import(
        self,
        criterion: str,
        token: str | None,
        text: str,
    ) -> CriterionResult:
        if self.command_runner is None:
            return CriterionResult(
                criterion=criterion,
                status="BLOCKED",
                check="import",
                reason="no command runner",
            )

        module: str | None = None

        if token and token.endswith(".py"):
            stem = token.split("/")[-1][:-3]

            if stem != "__init__":
                module = stem

        if module is None:
            match = _IMPORT_TOKEN_RE.search(text)

            if match:
                module = match.group(1)

        if not module:
            return CriterionResult(
                criterion=criterion,
                status="BLOCKED",
                check="import",
                reason="no module name in criterion",
            )

        ok, output = self._run(
            f'{self.python} -c "import {module}"'
        )

        return CriterionResult(
            criterion=criterion,
            status="PASS" if ok else "FAIL",
            check="import",
            evidence=[
                f"import {module}: "
                f"rc={0 if ok else 1} {output}".strip()
            ],
            reason="" if ok else (
                f"import failed: {output}"
            ),
        )

    @staticmethod
    def _test_target(
        text: str,
    ) -> str | None:
        """
        A criterion like "pytest tests/test_calc.py passes" must run
        THAT test file, not the entire repository suite.
        """

        for raw in _FILE_TOKEN_RE.findall(text):
            token = (
                raw.replace("\\", "/").lstrip("./")
            )

            if not token.endswith(".py"):
                continue

            stem = token.split("/")[-1][:-3].casefold()

            if (
                stem.startswith("test_")
                or stem.endswith("_test")
            ):
                return token

        return None

    def _pytest(
        self,
        criterion: str,
    ) -> CriterionResult:
        if self.command_runner is None:
            return CriterionResult(
                criterion=criterion,
                status="BLOCKED",
                check="pytest",
                reason="no command runner",
            )

        target = self._test_target(criterion)

        command = f"{self.python} -m pytest -q"

        if target:
            command += f' "{target}"'

        ok, output = self._run(command)

        return CriterionResult(
            criterion=criterion,
            status="PASS" if ok else "FAIL",
            check="pytest",
            evidence=[
                f"pytest rc={0 if ok else 1}: "
                f"{output}".strip()
            ],
            reason="" if ok else (
                f"pytest failed: {output}"
            ),
        )

