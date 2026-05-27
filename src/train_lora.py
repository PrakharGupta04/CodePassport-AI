"""
train_lora.py — LoRA fine-tuning of CodeT5 for Developer Passport generation.

Run this on GOOGLE COLAB or KAGGLE (free T4 GPU).

Usage (after uploading files to Colab):
    python src/train_lora.py \
        --data_dir   data/processed/ \
        --output_dir models/codepassport-lora/ \
        --epochs 3

Architecture choice justification:
- Model : Salesforce/codet5-base (220M params, encoder-decoder)
- PEFT  : LoRA rank=16 on query+value projections in both encoder+decoder
- Why   : Only ~2.8M params trained instead of 220M → fits in 15GB VRAM
           Preserves pre-trained code knowledge; avoids catastrophic forgetting
"""

import argparse
import os
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

MODEL_NAME     = "Salesforce/codet5-base"
MAX_INPUT_LEN  = 512
MAX_TARGET_LEN = 256
BATCH_SIZE     = 8       # safe for T4 16GB; drop to 4 if OOM
GRAD_ACCUM     = 4       # effective batch = 8 × 4 = 32
LEARNING_RATE  = 3e-4
WARMUP_RATIO   = 0.05
WEIGHT_DECAY   = 0.01
FP16           = True    # mixed precision — cuts VRAM roughly in half

# LoRA hyper-parameters
LORA_R         = 16      # rank — higher = more capacity, more VRAM
LORA_ALPHA     = 32      # scaling factor (alpha/r = 2.0)
LORA_DROPOUT   = 0.05
LORA_TARGETS   = ["q", "v"]  # attention projections to adapt


def setup_lora_model(model):
    """Wrap CodeT5 model with LoRA adapters using PEFT."""
    from peft import get_peft_model, LoraConfig, TaskType

    lora_config = LoraConfig(
        task_type=TaskType.SEQ_2_SEQ_LM,   # Seq2Seq for encoder-decoder
        r=LORA_R,
        lora_alpha=LORA_ALPHA,
        target_modules=LORA_TARGETS,        # only adapt q and v projections
        lora_dropout=LORA_DROPOUT,
        bias="none",                        # don't adapt bias terms
    )

    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()     # shows how many params are actually trained
    return model


def count_parameters(model) -> str:
    """Pretty-print trainable vs total parameters."""
    total    = sum(p.numel() for p in model.parameters())
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    pct = 100.0 * trainable / total
    return f"Trainable: {trainable:,} / Total: {total:,} ({pct:.2f}%)"


def main(data_dir: str, output_dir: str, epochs: int, resume: bool) -> None:
    import torch
    from transformers import (
        AutoTokenizer,
        T5ForConditionalGeneration,
        DataCollatorForSeq2Seq,
        Seq2SeqTrainer,
        Seq2SeqTrainingArguments,
        EarlyStoppingCallback,
    )
    from datasets import load_from_disk, Dataset
    from src.utils import read_jsonl, ensure_dirs

    ensure_dirs(output_dir)

    # ── Device ────────────────────────────────────────
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"\n🖥️  Device: {device}")
    if device == "cuda":
        print(f"   GPU: {torch.cuda.get_device_name(0)}")
        print(f"   VRAM: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")

    # ── Tokenizer ─────────────────────────────────────
    print(f"\n🔤 Loading tokenizer: {MODEL_NAME}")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)

    # ── Dataset ───────────────────────────────────────
    # Try loading pre-tokenized datasets first; fall back to JSONL
    def load_split(split_name: str):
        disk_path = os.path.join(data_dir, "tokenized", split_name)
        if os.path.exists(disk_path):
            print(f"   Loading pre-tokenized {split_name} from disk...")
            return load_from_disk(disk_path)

        # On-the-fly tokenization from JSONL
        jsonl_path = os.path.join(data_dir, f"{split_name}.jsonl")
        print(f"   Tokenizing {split_name} on the fly from {jsonl_path}...")
        records = read_jsonl(jsonl_path)
        prompts = [r["prompt"] for r in records]
        targets = [r["target"] for r in records]

        tokenized = tokenizer(
            prompts,
            text_target=targets,
            max_length=MAX_INPUT_LEN,
            max_target_length=MAX_TARGET_LEN,
            padding=False,          # DataCollator will handle padding
            truncation=True,
        )
        labels_cleaned = [
            [tok if tok != tokenizer.pad_token_id else -100 for tok in row]
            for row in tokenized["labels"]
        ]
        return Dataset.from_dict({
            "input_ids":      tokenized["input_ids"],
            "attention_mask": tokenized["attention_mask"],
            "labels":         labels_cleaned,
        })

    print("\n📦 Loading datasets...")
    train_dataset = load_split("train")
    val_dataset   = load_split("val")
    print(f"   Train: {len(train_dataset):,} | Val: {len(val_dataset):,}")

    # ── Model ─────────────────────────────────────────
    print(f"\n🤖 Loading base model: {MODEL_NAME}")
    model = T5ForConditionalGeneration.from_pretrained(
        MODEL_NAME,
        torch_dtype=torch.float16 if FP16 else torch.float32,
    )

    # ── Apply LoRA ────────────────────────────────────
    print("\n🎯 Applying LoRA adapters...")
    model = setup_lora_model(model)
    print(f"   {count_parameters(model)}")

    # ── Data Collator ─────────────────────────────────
    # Handles dynamic padding — more memory efficient than static padding
    data_collator = DataCollatorForSeq2Seq(
        tokenizer=tokenizer,
        model=model,
        padding=True,
        label_pad_token_id=-100,
    )

    # ── Training Arguments ────────────────────────────
    steps_per_epoch = math.ceil(len(train_dataset) / (BATCH_SIZE * GRAD_ACCUM))
    eval_steps      = max(steps_per_epoch // 4, 50)  # eval 4× per epoch

    training_args = Seq2SeqTrainingArguments(
        output_dir=output_dir,
        num_train_epochs=epochs,
        per_device_train_batch_size=BATCH_SIZE,
        per_device_eval_batch_size=BATCH_SIZE,
        gradient_accumulation_steps=GRAD_ACCUM,
        learning_rate=LEARNING_RATE,
        weight_decay=WEIGHT_DECAY,
        warmup_ratio=WARMUP_RATIO,
        fp16=FP16 and (device == "cuda"),
        predict_with_generate=True,         # needed for Seq2Seq eval
        generation_max_length=MAX_TARGET_LEN,
        evaluation_strategy="steps",
        eval_steps=eval_steps,
        save_strategy="steps",
        save_steps=eval_steps,
        logging_steps=50,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        save_total_limit=2,                 # keep only 2 checkpoints to save disk
        report_to="none",                   # disable wandb/tensorboard on Colab
        resume_from_checkpoint=resume,
        dataloader_num_workers=2,
    )

    # ── Trainer ───────────────────────────────────────
    trainer = Seq2SeqTrainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=val_dataset,
        tokenizer=tokenizer,
        data_collator=data_collator,
        callbacks=[EarlyStoppingCallback(early_stopping_patience=3)],
    )

    # ── Train ─────────────────────────────────────────
    print(f"\n🚂 Starting training for {epochs} epoch(s)...")
    print(f"   Steps per epoch : {steps_per_epoch}")
    print(f"   Eval every      : {eval_steps} steps")
    print(f"   Effective batch : {BATCH_SIZE * GRAD_ACCUM}\n")

    trainer.train()

    # ── Save ──────────────────────────────────────────
    print(f"\n💾 Saving model + tokenizer → {output_dir}")
    # Save only the LoRA adapter (tiny — ~40 MB vs 800 MB full model)
    model.save_pretrained(output_dir)
    tokenizer.save_pretrained(output_dir)

    # Also save training config for reproducibility
    config_path = os.path.join(output_dir, "training_config.json")
    import json
    with open(config_path, "w") as f:
        json.dump({
            "base_model":    MODEL_NAME,
            "lora_r":        LORA_R,
            "lora_alpha":    LORA_ALPHA,
            "lora_targets":  LORA_TARGETS,
            "epochs":        epochs,
            "batch_size":    BATCH_SIZE,
            "grad_accum":    GRAD_ACCUM,
            "learning_rate": LEARNING_RATE,
            "max_input_len": MAX_INPUT_LEN,
            "max_target_len":MAX_TARGET_LEN,
        }, f, indent=2)

    print(f"\n✅ Training complete! Model saved → {output_dir}\n")
    print("📌 Next step: Download the 'models/codepassport-lora/' folder to your local PC")
    print("   Then run: python src/inference.py --model_path models/codepassport-lora/ --interactive\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CodePassport AI — LoRA Fine-tuning")
    parser.add_argument("--data_dir",   required=True, help="Processed data directory (with train/val JSONL)")
    parser.add_argument("--output_dir", required=True, help="Where to save the fine-tuned model")
    parser.add_argument("--epochs",     type=int, default=3, help="Number of training epochs")
    parser.add_argument("--resume",     action="store_true",  help="Resume from last checkpoint")
    args = parser.parse_args()

    main(
        data_dir=args.data_dir,
        output_dir=args.output_dir,
        epochs=args.epochs,
        resume=args.resume,
    )
