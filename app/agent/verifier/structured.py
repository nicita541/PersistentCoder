from __future__ import annotations

import ast

from app.agent.state import CriterionResult
from app.tasks.verification_spec import VerificationKind, VerificationSpec


class StructuredVerifier:
    def __init__(self, *, workspace, command_runner, python: str = "python") -> None:
        self.workspace = workspace
        self.command_runner = command_runner
        self.python = python

    def verify_all(self, specs: list[VerificationSpec]) -> list[CriterionResult]:
        results: list[CriterionResult | None] = [None] * len(specs)
        pytest_indexes = [
            index
            for index, spec in enumerate(specs)
            if spec.kind is VerificationKind.PYTEST
        ]
        if pytest_indexes:
            pytest_result = self._pytest([specs[index] for index in pytest_indexes])
            for index in pytest_indexes:
                results[index] = pytest_result
        for index, spec in enumerate(specs):
            if results[index] is None:
                results[index] = self._verify_one(spec)
        return [result for result in results if result is not None]

    @staticmethod
    def _label(spec: VerificationSpec) -> str:
        pieces = [spec.kind.value]
        if spec.target:
            pieces.append(spec.target)
        if spec.symbol:
            pieces.append(spec.symbol)
        return ":".join(pieces)

    def _result(
        self,
        spec: VerificationSpec,
        status: str,
        *,
        reason: str = "",
        evidence: list[str] | None = None,
    ) -> CriterionResult:
        return CriterionResult(
            criterion=self._label(spec),
            status=status,
            check=spec.kind.value,
            reason=reason,
            evidence=list(evidence or []),
        )

    def _verify_one(self, spec: VerificationSpec) -> CriterionResult:
        if spec.kind is VerificationKind.FILE_EXISTS:
            exists = self.workspace.exists(spec.target)
            return self._result(
                spec,
                "PASS" if exists else "FAIL",
                reason="" if exists else f"file missing: {spec.target}",
                evidence=[f"file exists: {spec.target}"] if exists else [],
            )
        if spec.kind is VerificationKind.FILE_ABSENT:
            absent = not self.workspace.exists(spec.target)
            return self._result(
                spec,
                "PASS" if absent else "FAIL",
                reason="" if absent else f"file still exists: {spec.target}",
                evidence=[f"file absent: {spec.target}"] if absent else [],
            )
        if spec.kind in {VerificationKind.PY_SYMBOL, VerificationKind.PY_SIGNATURE}:
            return self._ast(spec)
        if spec.kind is VerificationKind.PY_COMPILE:
            return self._command(
                spec, f'{self.python} -m py_compile "{spec.target}"'
            )
        if spec.kind is VerificationKind.PY_IMPORT:
            module = self._module_name(spec.target or "")
            if not module:
                return self._result(spec, "BLOCKED", reason="invalid import target")
            return self._command(spec, f'{self.python} -c "import {module}"')
        return self._result(spec, "BLOCKED", reason="unsupported verification kind")

    def _command(self, spec: VerificationSpec, command: str) -> CriterionResult:
        if self.command_runner is None:
            return self._result(spec, "BLOCKED", reason="no sandbox command runner")
        result = self.command_runner.run(command)
        ok = result.returncode == 0
        output = (result.stdout or result.stderr or "")[:400]
        return self._result(
            spec,
            "PASS" if ok else "FAIL",
            reason="" if ok else output or f"command failed: {command}",
            evidence=[f"{command} -> rc={result.returncode}: {output}"],
        )

    def _ast(self, spec: VerificationSpec) -> CriterionResult:
        try:
            tree = ast.parse(self.workspace.read(spec.target or ""))
        except Exception as error:
            return self._result(spec, "FAIL", reason=f"python parse failed: {error}")
        node: ast.AST | None = tree
        for part in str(spec.symbol).split("."):
            body = getattr(node, "body", [])
            node = next(
                (
                    item
                    for item in body
                    if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == part
                ),
                None,
            )
            if node is None:
                return self._result(
                    spec, "FAIL", reason=f"python symbol missing: {spec.symbol}"
                )
        if spec.kind is VerificationKind.PY_SIGNATURE:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return self._result(spec, "FAIL", reason="symbol has no function signature")
            actual = [
                argument.arg
                for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            ]
            if actual != list(spec.expected_signature):
                return self._result(
                    spec,
                    "FAIL",
                    reason=f"signature mismatch: expected {spec.expected_signature}, got {actual}",
                )
        return self._result(spec, "PASS", evidence=[f"AST verified: {spec.symbol}"])

    @staticmethod
    def _module_name(target: str) -> str | None:
        if not target.endswith(".py"):
            return None
        parts = target[:-3].replace("\\", "/").split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts or not all(part.isidentifier() for part in parts):
            return None
        return ".".join(parts)

    def _pytest(self, specs: list[VerificationSpec]) -> CriterionResult:
        targets = []
        full_suite = any(spec.full_suite_allowed for spec in specs)
        for spec in specs:
            if spec.target and spec.target not in targets:
                targets.append(spec.target)
        representative = specs[0]
        if not targets and not full_suite:
            return self._result(
                representative,
                "BLOCKED",
                reason="pytest target required; full suite was not authorized",
            )
        command = f"{self.python} -m pytest -q"
        if targets:
            command += " " + " ".join(f'"{target}"' for target in targets)
        return self._command(representative, command)
