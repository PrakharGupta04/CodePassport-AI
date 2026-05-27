"""
baseline.py — Compare three approaches side-by-side on the same test inputs.

Approaches:
    1. Zero-shot   : Pretrained CodeT5 with no prompt engineering (vanilla)
    2. Prompt-eng  : Pretrained CodeT5 with structured few-shot prompt
    3. Fine-tuned  : LoRA fine-tuned CodePassport model

Usage:
    python src/baseline.py \
        --finetuned_path  models/codepassport-lora/ \
        --test_data       data/processed/test.jsonl \
        --num_samples     100 \
        --output_file     evaluation/baseline_comparison.json
"""

import argparse
import json
import os
import sys
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import read_jsonl, ensure_dirs
from src.evaluate import compute_bleu, compute_rouge


# ──────────────────────────────────────────────
# Prompt strategies
# ──────────────────────────────────────────────

def zero_shot_prompt(code: str) -> str:
    """Minimal prompt — pretrained model only."""
    return f"Summarize this Python function:\n{code.strip()}"


def few_shot_prompt(code: str) -> str:
    """
    Few-shot prompt with one example to guide structure.
    This is the prompt-engineering baseline.
    """
    example_code = '''def add(a, b):
    return a + b'''

    example_output = (
        "DOCSTRING: Returns the sum of two numbers.\n"
        "PURPOSE: Adds two numeric values and returns the result.\n"
        "BEHAVIOR SUMMARY: Takes two inputs and returns their sum.\n"
        "INPUTS / OUTPUTS: Input: a, b (numeric). Output: numeric sum.\n"
        "ASSUMPTIONS: Inputs are numeric types.\n"
        "EDGE CASES: No edge cases for well-typed inputs.\n"
        "DEVELOPER NOTE: Use built-in sum() for lists instead."
    )

    prompt = (
        f"Generate a structured developer passport with sections: "
        f"DOCSTRING, PURPOSE, BEHAVIOR SUMMARY, INPUTS / OUTPUTS, "
        f"ASSUMPTIONS, EDGE CASES, DEVELOPER NOTE.\n\n"
        f"### Example:\nFunction:\n{example_code}\n\nPassport:\n{example_output}\n\n"
        f"### Now generate for:\nFunction:\n{code.strip()}\n\nPassport:\n"
    )
    return prompt


def finetuned_prompt(code: str) -> str:
    """The exact prompt format used during LoRA fine-tuning."""
    from src.utils import build_passport_prompt
    return build_passport_prompt(code)


# ──────────────────────────────────────────────
# Generate with a given prompt strategy
# ──────────────────────────────────────────────

def generate_with_prompt(prompt: str, model, tokenizer, device) -> str:
    import torch

    inputs = tokenizer(
        prompt,
        return_tensors="pt",
        max_length=512,
        truncation=True,
    ).to(device)

    with torch.no_grad():
        outputs = model.generate(
            input_ids=inputs["input_ids"],
            attention_mask=inputs["attention_mask"],
            max_new_tokens=256,
            num_beams=4,
            early_stopping=True,
            no_repeat_ngram_size=3,
        )

    return tokenizer.decode(outputs[0], skip_special_tokens=True)


# ──────────────────────────────────────────────
# Main comparison
# ──────────────────────────────────────────────

def run_baseline_comparison(
    finetuned_path: str,
    test_data_path: str,
    num_samples: int,
    output_file: str,
) -> None:
    import torch
    from transformers import AutoTokenizer, T5ForConditionalGeneration
    from peft import PeftModel, PeftConfig

    print("\n🔬 CodePassport AI — Baseline Comparison")
    print("=" * 60)

    device = "cuda" if torch.cuda.is_available() else "cpu"

    # ── Load base model (shared for zero-shot + few-shot) ──
    BASE_MODEL = "Salesforce/codet5-base"
    print(f"\n📦 Loading base model: {BASE_MODEL}")
    base_tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    base_model = T5ForConditionalGeneration.from_pretrained(BASE_MODEL).to(device).eval()

    # ── Load fine-tuned model ──
    print(f"\n📦 Loading fine-tuned model: {finetuned_path}")
    ft_tokenizer = AutoTokenizer.from_pretrained(finetuned_path)
    try:
        config = PeftConfig.from_pretrained(finetuned_path)
        ft_base = T5ForConditionalGeneration.from_pretrained(config.base_model_name_or_path)
        ft_model = PeftModel.from_pretrained(ft_base, finetuned_path).merge_and_unload()
    except Exception:
        ft_model = T5ForConditionalGeneration.from_pretrained(finetuned_path)
    ft_model = ft_model.to(device).eval()

    # ── Load test samples ──
    all_records = read_jsonl(test_data_path)
    random.seed(42)
    records = random.sample(all_records, min(num_samples, len(all_records)))

    results = {
        "zero_shot":   {"refs": [], "hyps": []},
        "prompt_eng":  {"refs": [], "hyps": []},
        "finetuned":   {"refs": [], "hyps": []},
    }

    print(f"\n⚙️  Generating outputs for {len(records)} samples (3 approaches × {len(records)} = {3*len(records)} calls)...")

    for i, rec in enumerate(records):
        if i % 10 == 0:
            print(f"   {i}/{len(records)}...")

        code = rec["original_code"]
        ref  = rec["target"]

        # Zero-shot (base model, minimal prompt)
        zs_out = generate_with_prompt(zero_shot_prompt(code), base_model, base_tokenizer, device)

        # Few-shot / prompt engineering (base model, structured few-shot prompt)
        fs_out = generate_with_prompt(few_shot_prompt(code), base_model, base_tokenizer, device)

        # Fine-tuned (LoRA model, training prompt format)
        ft_out = generate_with_prompt(finetuned_prompt(code), ft_model, ft_tokenizer, device)

        for key, hyp in [("zero_shot", zs_out), ("prompt_eng", fs_out), ("finetuned", ft_out)]:
            results[key]["refs"].append(ref)
            results[key]["hyps"].append(hyp)

    # ── Compute metrics ──────────────────────────────────
    print("\n📊 Computing metrics for all three approaches...")
    summary = {}

    for approach, data in results.items():
        bleu  = compute_bleu(data["refs"], data["hyps"])
        rouge = compute_rouge(data["refs"], data["hyps"])
        summary[approach] = {**bleu, **rouge}

    # ── Print comparison table ───────────────────────────
    print("\n" + "═" * 70)
    print(f"  BASELINE COMPARISON — {len(records)} samples")
    print("═" * 70)
    headers = ["BLEU-1", "BLEU-4", "ROUGE-1", "ROUGE-2", "ROUGE-L"]
    print(f"  {'Approach':<16}", end="")
    for h in headers:
        print(f"  {h:>8}", end="")
    print()
    print("  " + "─" * 66)

    labels = {
        "zero_shot":  "Zero-shot",
        "prompt_eng": "Prompt-eng",
        "finetuned":  "LoRA Fine-tuned ✅",
    }
    for key, label in labels.items():
        print(f"  {label:<20}", end="")
        for h in headers:
            print(f"  {summary[key][h]:>8.2f}", end="")
        print()

    print("═" * 70)

    # ── Qualitative sample output ─────────────────────────
    print("\n📝 Sample qualitative comparison (first test case):")
    print("─" * 60)
    rec = records[0]
    print(f"  CODE:\n{rec['original_code'][:300]}\n")
    print(f"  [1] ZERO-SHOT OUTPUT:\n  {results['zero_shot']['hyps'][0][:200]}\n")
    print(f"  [2] PROMPT-ENG OUTPUT:\n  {results['prompt_eng']['hyps'][0][:200]}\n")
    print(f"  [3] FINE-TUNED OUTPUT:\n  {results['finetuned']['hyps'][0][:200]}\n")
    print(f"  [✓] REFERENCE:\n  {rec['target'][:200]}")
    print("─" * 60)

    # ── Save ─────────────────────────────────────────────
    ensure_dirs(os.path.dirname(output_file) if os.path.dirname(output_file) else ".")
    output_data = {
        "num_samples": len(records),
        "metrics":     summary,
        "sample": {
            "code":       records[0]["original_code"],
            "reference":  records[0]["target"],
            "zero_shot":  results["zero_shot"]["hyps"][0],
            "prompt_eng": results["prompt_eng"]["hyps"][0],
            "finetuned":  results["finetuned"]["hyps"][0],
        },
    }
    with open(output_file, "w") as f:
        json.dump(output_data, f, indent=2)

    print(f"\n✅ Baseline comparison saved → {output_file}")


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CodePassport AI — Baseline Comparison")
    parser.add_argument("--finetuned_path", required=True)
    parser.add_argument("--test_data",      required=True)
    parser.add_argument("--num_samples",    type=int, default=100)
    parser.add_argument("--output_file",    default="evaluation/baseline_comparison.json")
    args = parser.parse_args()

    run_baseline_comparison(
        finetuned_path=args.finetuned_path,
        test_data_path=args.test_data,
        num_samples=args.num_samples,
        output_file=args.output_file,
    )
