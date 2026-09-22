from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import time
from pathlib import Path

import torch
from peft import (
    LoraConfig,
    PeftModel,
    get_peft_model,
    prepare_model_for_kbit_training,
)
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL = PROJECT_ROOT / "models" / "manual" / "Qwen2.5-Coder-3B-Instruct"
DEFAULT_DATASET = PROJECT_ROOT / "data" / "training" / "planner-v1" / "train.jsonl"
DEFAULT_OUTPUT = PROJECT_ROOT / "models" / "adapters" / "planner-coder-qwen2.5-3b-v2"


def _inside_project(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(PROJECT_ROOT):
        raise SystemExit(f"{label} must stay inside {PROJECT_ROOT}")
    return resolved


def _records(
    path: Path,
    decomposer_weight: int,
    coder_weight: int = 3,
    debugger_weight: int = 3,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            weight = (
                decomposer_weight
                if record["stage"] == "TASK_DECOMPOSER"
                else (
                    coder_weight
                    if record["stage"]
                    in {"CODER_ACTION", "CODER_REPAIR", "COMMAND_REPAIR"}
                    else (
                        debugger_weight
                        if record["stage"] == "DEBUG_DIAGNOSIS"
                        else 1
                    )
                )
            )
            records.extend([record] * weight)
    if not records:
        raise SystemExit("training dataset is empty")
    return records


def _encode(tokenizer, record: dict[str, object], max_length: int) -> dict[str, object]:
    prompt_ids = tokenizer.apply_chat_template(
        record["messages"],
        tokenize=True,
        add_generation_prompt=True,
    )
    if not isinstance(prompt_ids, list):
        prompt_ids = prompt_ids["input_ids"]
    if prompt_ids and isinstance(prompt_ids[0], list):
        prompt_ids = prompt_ids[0]
    response_ids = tokenizer.encode(
        str(record["response"]) + tokenizer.eos_token,
        add_special_tokens=False,
    )
    if len(response_ids) >= max_length:
        raise ValueError(f"response is too long for {record['id']}")
    prompt_budget = max_length - len(response_ids)
    if len(prompt_ids) > prompt_budget:
        prompt_ids = prompt_ids[-prompt_budget:]
    input_ids = prompt_ids + response_ids
    return {
        "id": record["id"],
        "stage": record["stage"],
        "input_ids": input_ids,
        "labels": [-100] * len(prompt_ids) + response_ids,
    }


def _batch(item: dict[str, object], device: torch.device) -> dict[str, torch.Tensor]:
    input_ids = torch.tensor([item["input_ids"]], dtype=torch.long, device=device)
    labels = torch.tensor([item["labels"]], dtype=torch.long, device=device)
    return {
        "input_ids": input_ids,
        "attention_mask": torch.ones_like(input_ids),
        "labels": labels,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--init-adapter", type=Path)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--max-length", type=int, default=2304)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--gradient-accumulation", type=int, default=4)
    parser.add_argument("--decomposer-weight", type=int, default=3)
    parser.add_argument("--coder-weight", type=int, default=3)
    parser.add_argument("--debugger-weight", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260922)
    args = parser.parse_args()

    model_path = _inside_project(args.model, "model")
    dataset_path = _inside_project(args.dataset, "dataset")
    output_path = _inside_project(args.output, "output")
    init_adapter = (
        _inside_project(args.init_adapter, "init adapter")
        if args.init_adapter is not None
        else None
    )
    if output_path.exists() and any(output_path.iterdir()):
        raise SystemExit(f"output already contains files: {output_path}")
    output_path.mkdir(parents=True, exist_ok=True)

    if not torch.cuda.is_available():
        raise SystemExit("CUDA is required for this QLoRA training script")
    if not torch.cuda.is_bf16_supported():
        raise SystemExit("this training configuration requires bfloat16 support")

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)

    records = _records(
        dataset_path,
        args.decomposer_weight,
        args.coder_weight,
        args.debugger_weight,
    )
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    encoded = [_encode(tokenizer, record, args.max_length) for record in records]

    quantization = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        model_path,
        local_files_only=True,
        quantization_config=quantization,
        device_map={"": 0},
        torch_dtype=torch.bfloat16,
    )
    model.config.use_cache = False
    model = prepare_model_for_kbit_training(
        model,
        use_gradient_checkpointing=True,
    )
    if init_adapter is not None:
        model = PeftModel.from_pretrained(
            model,
            init_adapter,
            is_trainable=True,
        )
    else:
        model = get_peft_model(
            model,
            LoraConfig(
                r=8,
                lora_alpha=16,
                lora_dropout=0.05,
                bias="none",
                task_type="CAUSAL_LM",
                target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
            ),
        )
    model.train()
    trainable = [parameter for parameter in model.parameters() if parameter.requires_grad]
    optimizer = torch.optim.AdamW(trainable, lr=args.learning_rate)
    device = torch.device("cuda:0")

    total_micro_steps = len(encoded) * args.epochs
    total_optimizer_steps = math.ceil(total_micro_steps / args.gradient_accumulation)
    optimizer.zero_grad(set_to_none=True)
    started = time.perf_counter()
    losses: list[float] = []
    optimizer_step = 0
    micro_step = 0

    print(
        json.dumps(
            {
                "records_after_weighting": len(encoded),
                "epochs": args.epochs,
                "micro_steps": total_micro_steps,
                "optimizer_steps": total_optimizer_steps,
                "max_tokens": max(len(item["input_ids"]) for item in encoded),
                "trainable_parameters": sum(item.numel() for item in trainable),
                "cuda_allocated_gib": round(torch.cuda.memory_allocated() / 1024**3, 3),
            },
            indent=2,
        ),
        flush=True,
    )

    for epoch in range(args.epochs):
        order = list(range(len(encoded)))
        random.Random(args.seed + epoch).shuffle(order)
        for position, index in enumerate(order, start=1):
            micro_step += 1
            item = encoded[index]
            output = model(**_batch(item, device))
            loss = output.loss
            (loss / args.gradient_accumulation).backward()
            losses.append(float(loss.detach().cpu()))

            final_micro_step = micro_step == total_micro_steps
            if micro_step % args.gradient_accumulation == 0 or final_micro_step:
                torch.nn.utils.clip_grad_norm_(trainable, 1.0)
                optimizer.step()
                optimizer.zero_grad(set_to_none=True)
                optimizer_step += 1
                if optimizer_step == 1 or optimizer_step % 5 == 0 or final_micro_step:
                    window = losses[-args.gradient_accumulation :]
                    print(
                        json.dumps(
                            {
                                "epoch": epoch + 1,
                                "position": position,
                                "optimizer_step": optimizer_step,
                                "loss": round(sum(window) / len(window), 5),
                                "elapsed_seconds": round(time.perf_counter() - started, 1),
                                "cuda_peak_gib": round(torch.cuda.max_memory_allocated() / 1024**3, 3),
                            }
                        ),
                        flush=True,
                    )

    model.save_pretrained(output_path, safe_serialization=True)
    tokenizer.save_pretrained(output_path)
    dataset_sha = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    metadata = {
        "base_model": str(model_path.relative_to(PROJECT_ROOT)),
        "init_adapter": (
            str(init_adapter.relative_to(PROJECT_ROOT))
            if init_adapter is not None
            else None
        ),
        "dataset": str(dataset_path.relative_to(PROJECT_ROOT)),
        "dataset_sha256": dataset_sha,
        "epochs": args.epochs,
        "max_length": args.max_length,
        "learning_rate": args.learning_rate,
        "gradient_accumulation": args.gradient_accumulation,
        "decomposer_weight": args.decomposer_weight,
        "coder_weight": args.coder_weight,
        "debugger_weight": args.debugger_weight,
        "seed": args.seed,
        "micro_steps": total_micro_steps,
        "optimizer_steps": optimizer_step,
        "initial_loss": losses[0],
        "final_loss": losses[-1],
        "mean_loss": sum(losses) / len(losses),
        "duration_seconds": time.perf_counter() - started,
        "cuda_peak_gib": torch.cuda.max_memory_allocated() / 1024**3,
    }
    (output_path / "training_metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(metadata, indent=2), flush=True)


if __name__ == "__main__":
    main()
