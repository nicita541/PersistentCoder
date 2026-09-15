from __future__ import annotations

import sys
from pathlib import Path


def test_tests_run_from_a_virtual_environment():
    executable = Path(sys.executable).resolve()
    environment = executable.parent.parent

    assert environment.name == ".venv"
    assert environment.exists()
