from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

SYSTEM_PROMPT_PATH = (
    PROJECT_ROOT
    / "config"
    / "system_prompt.txt"
)


def load_system_prompt() -> str:
    if not SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"System prompt не найден: {SYSTEM_PROMPT_PATH}"
        )

    prompt = SYSTEM_PROMPT_PATH.read_text(
        encoding="utf-8"
    ).strip()

    if not prompt:
        raise RuntimeError(
            "config/system_prompt.txt пуст."
        )

    return prompt