from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ------------------------------------------------------------------
# DATA / TEMP / CACHE (all project-local)
# ------------------------------------------------------------------

DATA_ROOT = PROJECT_ROOT / "data"
TMP_ROOT = DATA_ROOT / "tmp"
PIP_CACHE = TMP_ROOT / "pip-cache"

DATABASE_PATH = DATA_ROOT / "persistent_coder.db"

# ------------------------------------------------------------------
# MODELS (project-local Hugging Face cache)
# ------------------------------------------------------------------

MODELS_ROOT = PROJECT_ROOT / "models"
HF_CACHE = MODELS_ROOT / "huggingface"

# ------------------------------------------------------------------
# SANDBOX (host-side sandbox metadata, project-local)
# ------------------------------------------------------------------

SANDBOX_ROOT = PROJECT_ROOT / ".sandbox"
SANDBOX_SESSIONS = SANDBOX_ROOT / "sessions"
SANDBOX_SNAPSHOTS = SANDBOX_ROOT / "snapshots"
SANDBOX_PATCHES = SANDBOX_ROOT / "patches"
SANDBOX_LOGS = SANDBOX_ROOT / "logs"
SANDBOX_TMP = SANDBOX_ROOT / "tmp"


PROJECT_DIRECTORIES = (
    DATA_ROOT,
    TMP_ROOT,
    MODELS_ROOT,
    HF_CACHE,
    SANDBOX_ROOT,
    SANDBOX_SESSIONS,
    SANDBOX_SNAPSHOTS,
    SANDBOX_PATCHES,
    SANDBOX_LOGS,
    SANDBOX_TMP,
)


def ensure_layout() -> None:
    """Create every project-local directory the runtime expects."""

    for directory in PROJECT_DIRECTORIES:
        directory.mkdir(
            parents=True,
            exist_ok=True,
        )


def is_within_project(
    path: str | Path,
) -> bool:
    candidate = Path(path).resolve()

    return (
        candidate == PROJECT_ROOT
        or PROJECT_ROOT in candidate.parents
    )


def configure_project_env() -> None:
    """
    Force Hugging Face / pip / temp to stay inside PROJECT_ROOT.

    Called explicitly by the runtime (never on import) so that no
    PersistentCoder artifact can leak to C:\\ / D:\\ / %TEMP%.
    """

    ensure_layout()

    for name in (
        "HF_HOME",
        "HF_HUB_CACHE",
        "TRANSFORMERS_CACHE",
    ):
        os.environ[name] = str(HF_CACHE)

    os.environ.setdefault(
        "HF_HUB_DISABLE_TELEMETRY",
        "1",
    )

    for name in ("TMPDIR", "TEMP", "TMP"):
        os.environ[name] = str(TMP_ROOT)

    os.environ["PIP_CACHE_DIR"] = str(PIP_CACHE)
