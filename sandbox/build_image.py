"""
Build the PersistentCoder sandbox Docker image.

Everything stays project-local:

    F:\\PersistentCoder\\sandbox\\Dockerfile           (build context)
    F:\\PersistentCoder\\.sandbox\\...                 (runtime sandbox data)

Usage (from the project root, inside .venv):

    python sandbox/build_image.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.sandbox.paths import (  # noqa: E402
    SANDBOX_DOCKERFILE_DIR,
    SANDBOX_IMAGE,
)


def build() -> int:
    arguments = [
        "docker",
        "build",
        "-t",
        SANDBOX_IMAGE,
        str(SANDBOX_DOCKERFILE_DIR),
    ]

    print(" ".join(arguments))

    completed = subprocess.run(arguments)

    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(build())
