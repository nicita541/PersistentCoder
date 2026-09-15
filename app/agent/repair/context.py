from __future__ import annotations

import re


def approach_fingerprint(failure_class: str, root_cause: str) -> str:
    normalized_class = re.sub(r"\s+", " ", failure_class).strip().casefold()
    normalized_cause = re.sub(r"\s+", " ", root_cause).strip().casefold()
    return f"{normalized_class}::{normalized_cause}"
