"""
tokenize_data.py — Tokenize processed JSONL files for CodeT5 training.

Usage:
    python src/tokenize_data.py \
        --data_dir  data/processed/ \
        --output_dir data/tokenized/ \
        --model_name Salesforce/codet5-base

What this script does:
1. Loads train/val/test JSONL files
2. Tokenizes prompts (encoder input) and targets (decoder labels)
3. Pads/truncates to model max length
4. Saves HuggingFace Dataset objects to disk for fast loading during training
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import read_jsonl, ensure_dirs

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────

MAX_INPUT_LEN  = 512   # encoder tokens  (code prompt)
MAX_TARGET_LEN = 256   # decoder tokens  (passport text)


def tokenize_split(records: list, tokenizer, split_name: str) -> dict:
    """
    Tokenize a list of (prompt, target) records.
    Returns a dict of lists: input_ids, attention_mask, labels.
    """
    input_ids_list      = []
    attention_mask_list = []
    labels_list         = []

    print(f"\n⚙️  Tokenizing {split_name} ({len(records):,} samples)...")

    for i, rec in enumerate(records):
        if i % 2000 == 0 and i > 0:
            print(f"   {i:,}/{len(records):,} done...")

        # Tokenize input prompt (encoder side)
        enc = tokenizer(
            rec["prompt"],
            max_length=MAX_INPUT_LEN,
            padding="max_length",
            truncation=True,
            return_tensors="pt",
        )

        # Tokenize target passport (decoder side)
        with tokenizer.as_target_tokenizer():
            dec = tokenizer(
                rec["target"],
                max_length=MAX_TARGET_LEN,
                padding="max_length",
                truncation=True,
                return_tensors="pt",
            )

        labels = dec["input_ids"].squeeze().tolist()

        # Replace padding token id in labels with -100
        # so PyTorch's CrossEntropyLoss ignores them
        labels = [
            token_id if token_id != tokenizer.pad_token_id else -100
            for token_id in labels
        ]

        input_ids_list.append(enc["input_ids"].squeeze().tolist())
        attention_mask_list.append(enc["attention_mask"].squeeze().tolist())
        labels_list.append(labels)

    return {
        "input_ids":      input_ids_list,
        "attention_mask": attention_mask_list,
        "labels":         labels_list,
    }


def main(data_dir: str, output_dir: str, model_name: str) -> None:
    from transformers import AutoTokenizer
    from datasets import Dataset

    print(f"\n🔤 Loading tokenizer: {model_name}")
    tokenizer = AutoTokenizer.from_pretrained(model_name)

    # CodeT5 uses a special target tokenizer context
    # Some versions don't need it — handle both
    has_target_ctx = hasattr(tokenizer, "as_target_tokenizer")

    ensure_dirs(output_dir)

    for split in ["train", "val", "test"]:
        path = os.path.join(data_dir, f"{split}.jsonl")
        if not os.path.exists(path):
            print(f"⚠️  Skipping {split} — file not found: {path}")
            continue

        records = read_jsonl(path)

        # ── Tokenize ──────────────────────────────────
        # For CodeT5, use the simpler text_target parameter
        prompts = [r["prompt"] for r in records]
        targets = [r["target"] for r in records]

        print(f"\n⚙️  Tokenizing {split} ({len(records):,} samples)...")

        tokenized = tokenizer(
            prompts,
            text_target=targets,
            max_length=MAX_INPUT_LEN,
            max_target_length=MAX_TARGET_LEN,
            padding="max_length",
            truncation=True,
            return_tensors="pt",
        )

        # Build labels: replace pad tokens with -100
        labels = tokenized["labels"].tolist()
        labels_cleaned = [
            [tok if tok != tokenizer.pad_token_id else -100 for tok in row]
            for row in labels
        ]

        dataset = Dataset.from_dict({
            "input_ids":      tokenized["input_ids"].tolist(),
            "attention_mask": tokenized["attention_mask"].tolist(),
            "labels":         labels_cleaned,
        })

        out_path = os.path.join(output_dir, split)
        dataset.save_to_disk(out_path)
        print(f"   ✅ Saved {split} → {out_path}  ({len(dataset):,} rows)")

    print("\n✅ Tokenization complete!\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CodePassport AI — Tokenization")
    parser.add_argument("--data_dir",    required=True, help="Processed JSONL directory")
    parser.add_argument("--output_dir",  required=True, help="Where to save tokenized datasets")
    parser.add_argument("--model_name",  default="Salesforce/codet5-base",
                        help="HuggingFace model name")
    args = parser.parse_args()

    main(
        data_dir=args.data_dir,
        output_dir=args.output_dir,
        model_name=args.model_name,
    )
