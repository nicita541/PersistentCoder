from __future__ import annotations

from dataclasses import dataclass

from app.sandbox.project_path import ProjectPath


_PROTECTED_COMPONENTS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "node_modules",
        ".sandbox",
    }
)

_SECRET_NAMES = frozenset(
    {
        "credentials",
        "credentials.json",
        "credentials.yml",
        "credentials.yaml",
        "service-account.json",
        "service_account.json",
        "id_rsa",
        "id_ed25519",
        ".npmrc",
        ".pypirc",
        ".netrc",
    }
)

_SECRET_SUFFIXES = frozenset(
    {".pem", ".key", ".p12", ".pfx", ".crt", ".cer"}
)


@dataclass(frozen=True, slots=True)
class PathDecision:
    included: bool
    reason: str | None = None


class ProtectedPathPolicy:
    """Classifies framework metadata, caches, and likely secret files."""

    def classify(self, path: ProjectPath) -> PathDecision:
        parts = tuple(part.casefold() for part in path.value.split("/"))
        protected = next(
            (part for part in parts if part in _PROTECTED_COMPONENTS),
            None,
        )
        if protected is not None:
            return PathDecision(False, f"protected component: {protected}")

        name = parts[-1]
        if name == ".env" or (
            name.startswith(".env.") and name != ".env.example"
        ):
            return PathDecision(False, "environment secret file")
        if name in _SECRET_NAMES:
            return PathDecision(False, "credential file")
        if any(name.endswith(suffix) for suffix in _SECRET_SUFFIXES):
            return PathDecision(False, "key or certificate file")

        return PathDecision(True)
