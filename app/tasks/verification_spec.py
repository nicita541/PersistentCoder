from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from app.sandbox.project_path import ProjectPath


class VerificationKind(str, Enum):
    FILE_EXISTS = "FILE_EXISTS"
    FILE_ABSENT = "FILE_ABSENT"
    PY_COMPILE = "PY_COMPILE"
    PY_IMPORT = "PY_IMPORT"
    PYTEST = "PYTEST"
    PY_SYMBOL = "PY_SYMBOL"
    PY_SIGNATURE = "PY_SIGNATURE"


@dataclass(frozen=True, slots=True)
class VerificationSpec:
    kind: VerificationKind
    target: str | None = None
    symbol: str | None = None
    expected_signature: list[str] = field(default_factory=list)
    full_suite_allowed: bool = False

    def __post_init__(self) -> None:
        if self.target is not None:
            object.__setattr__(self, "target", ProjectPath.parse(self.target).value)
        if self.kind in {
            VerificationKind.FILE_EXISTS,
            VerificationKind.FILE_ABSENT,
            VerificationKind.PY_COMPILE,
            VerificationKind.PY_IMPORT,
            VerificationKind.PY_SYMBOL,
            VerificationKind.PY_SIGNATURE,
        } and self.target is None:
            raise ValueError(f"{self.kind.value} requires target")
        if self.kind in {
            VerificationKind.PY_SYMBOL,
            VerificationKind.PY_SIGNATURE,
        } and not self.symbol:
            raise ValueError(f"{self.kind.value} requires symbol")
        if self.kind is VerificationKind.PYTEST and (
            self.target is None and not self.full_suite_allowed
        ):
            # Valid persisted state, but verification will be BLOCKED.
            return

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "target": self.target,
            "symbol": self.symbol,
            "expected_signature": list(self.expected_signature),
            "full_suite_allowed": self.full_suite_allowed,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "VerificationSpec":
        signature = value.get("expected_signature", [])
        if not isinstance(signature, list) or not all(
            isinstance(item, str) for item in signature
        ):
            raise ValueError("expected_signature must be a string list")
        return cls(
            kind=VerificationKind(str(value.get("kind", ""))),
            target=(
                str(value["target"]) if value.get("target") is not None else None
            ),
            symbol=(
                str(value["symbol"]) if value.get("symbol") is not None else None
            ),
            expected_signature=list(signature),
            full_suite_allowed=value.get("full_suite_allowed") is True,
        )


def parse_verification_specs(values: object) -> list[VerificationSpec]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("verification_specs must be a list")
    result: list[VerificationSpec] = []
    for value in values:
        if isinstance(value, VerificationSpec):
            result.append(value)
        elif isinstance(value, dict):
            result.append(VerificationSpec.from_dict(value))
        else:
            raise ValueError("each verification spec must be an object")
    return result
