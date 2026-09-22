from __future__ import annotations

import os
from pathlib import Path

import torch
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]

MODEL_CACHE = PROJECT_ROOT / "models" / "huggingface"

DEFAULT_MODEL_NAME = "Qwen/Qwen2.5-Coder-1.5B-Instruct"
MODEL_NAME = (
    os.environ.get("PERSISTENTCODER_MODEL_NAME", DEFAULT_MODEL_NAME).strip()
    or DEFAULT_MODEL_NAME
)
MODEL_ADAPTER = os.environ.get("PERSISTENTCODER_MODEL_ADAPTER", "").strip()
LOAD_IN_4BIT = os.environ.get("PERSISTENTCODER_LOAD_IN_4BIT", "").strip().casefold() in {
    "1",
    "true",
    "yes",
    "on",
}


def _adapter_path() -> Path | None:
    if not MODEL_ADAPTER:
        return None

    candidate = Path(MODEL_ADAPTER)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    candidate = candidate.resolve()

    if not candidate.is_relative_to(PROJECT_ROOT):
        raise ValueError("PERSISTENTCODER_MODEL_ADAPTER must stay inside the project")
    if not candidate.is_dir():
        raise FileNotFoundError(f"model adapter directory does not exist: {candidate}")
    return candidate


class QwenClient:
    def __init__(self) -> None:
        MODEL_CACHE.mkdir(
            parents=True,
            exist_ok=True,
        )

        print(f"Model cache: {MODEL_CACHE}")
        print(f"Loading model: {MODEL_NAME}")

        model_options: dict[str, object] = {
            "cache_dir": MODEL_CACHE,
            "device_map": "auto",
            "dtype": "auto",
        }
        if LOAD_IN_4BIT:
            model_options["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_use_double_quant=True,
            )
            print("Loading base model in 4-bit NF4 mode.")

        self.tokenizer = AutoTokenizer.from_pretrained(
            MODEL_NAME,
            cache_dir=MODEL_CACHE,
        )

        self.model = AutoModelForCausalLM.from_pretrained(
            MODEL_NAME,
            **model_options,
        )

        adapter = _adapter_path()
        if adapter is not None:
            try:
                from peft import PeftModel
            except ImportError as error:
                raise RuntimeError(
                    "Loading a planner adapter requires the optional "
                    "requirements-training.txt dependencies"
                ) from error
            self.model = PeftModel.from_pretrained(
                self.model,
                adapter,
                is_trainable=False,
            )
            print(f"Planner adapter loaded: {adapter}")

        self.model.eval()

        print("Model loaded successfully.")

    def chat(
        self,
        messages: list[dict[str, str]],
        max_new_tokens: int = 512,
    ) -> str:

        text = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )

        inputs = self.tokenizer(
            text,
            return_tensors="pt",
        ).to(self.model.device)

        with torch.no_grad():
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
            )

        generated_tokens = outputs[0][
            inputs["input_ids"].shape[1]:
        ]

        answer = self.tokenizer.decode(
            generated_tokens,
            skip_special_tokens=True,
        )

        return answer
