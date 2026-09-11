from __future__ import annotations

from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer


PROJECT_ROOT = Path(__file__).resolve().parents[2]

MODEL_CACHE = PROJECT_ROOT / "models" / "huggingface"

MODEL_NAME = "Qwen/Qwen2.5-Coder-0.5B-Instruct"


class QwenClient:
    def __init__(self) -> None:
        MODEL_CACHE.mkdir(
            parents=True,
            exist_ok=True,
        )

        print(f"Model cache: {MODEL_CACHE}")
        print(f"Loading model: {MODEL_NAME}")

        self.tokenizer = AutoTokenizer.from_pretrained(
            MODEL_NAME,
            cache_dir=MODEL_CACHE,
        )

        self.model = AutoModelForCausalLM.from_pretrained(
            MODEL_NAME,
            cache_dir=MODEL_CACHE,
            dtype="auto",
            device_map="auto",
        )

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