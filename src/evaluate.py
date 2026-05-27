"""
evaluate.py — Quantitative evaluation: BLEU-4, ROUGE-L, ROUGE-1, ROUGE-2

Usage:
    python src/evaluate.py \
        --model_path  models/codepassport-lora/ \
        --test_data   data/processed/test.jsonl \
        --output_file evaluation/results.json \
        --num_samples 200

Output:
    - Prints metrics table to console
    - Saves detailed results to JSON (for viva presentation)
    - Shows failure cases + hallucination examples
"""

import argparse
import json
import os
import sys
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import read_jsonl, ensure_dirs


# ──────────────────────────────────────────────
# Metric helpers
# ──────────────────────────────────────────────

def compute_bleu(references: list, hypotheses: list) -> dict:
    """Compute BLEU-1 through BLEU-4 using NLTK."""
    import nltk
    from nltk.translate.bleu_score import corpus_bleu, SmoothingFunction

    # Download tokenizer data if not present
    try:
        nltk.data.find("tokenizers/punkt")
    except LookupError:
        nltk.download("punkt", quiet=True)

    smoother = SmoothingFunction().method1

    # Tokenize: split on whitespace for simplicity
    refs_tok  = [[ref.lower().split()] for ref in references]
    hyps_tok  = [hyp.lower().split()   for hyp in hypotheses]

    bleu1 = corpus_bleu(refs_tok, hyps_tok, weights=(1, 0, 0, 0), smoothing_function=smoother)
    bleu2 = corpus_bleu(refs_tok, hyps_tok, weights=(0.5, 0.5, 0, 0), smoothing_function=smoother)
    bleu3 = corpus_bleu(refs_tok, hyps_tok, weights=(0.33, 0.33, 0.34, 0), smoothing_function=smoother)
    bleu4 = corpus_bleu(refs_tok, hyps_tok, weights=(0.25, 0.25, 0.25, 0.25), smoothing_function=smoother)

    return {
        "BLEU-1": round(bleu1 * 100, 2),
        "BLEU-2": round(bleu2 * 100, 2),
        "BLEU-3": round(bleu3 * 100, 2),
        "BLEU-4": round(bleu4 * 100, 2),
    }


def compute_rouge(references: list, hypotheses: list) -> dict:
    """Compute ROUGE-1, ROUGE-2, ROUGE-L F1 scores."""
    from rouge_score import rouge_scorer

    scorer = rouge_scorer.RougeScorer(["rouge1", "rouge2", "rougeL"], use_stemmer=True)

    r1_scores, r2_scores, rl_scores = [], [], []

    for ref, hyp in zip(references, hypotheses):
        scores = scorer.score(ref, hyp)
        r1_scores.append(scores["rouge1"].fmeasure)
        r2_scores.append(scores["rouge2"].fmeasure)
        rl_scores.append(scores["rougeL"].fmeasure)

    n = len(references)
    return {
        "ROUGE-1": round(sum(r1_scores) / n * 100, 2),
        "ROUGE-2": round(sum(r2_scores) / n * 100, 2),
        "ROUGE-L": round(sum(rl_scores) / n * 100, 2),
    }


def avg_length(texts: list) -> float:
    return round(sum(len(t.split()) for t in texts) / max(len(texts), 1), 1)


# ──────────────────────────────────────────────
# Hallucination / failure analysis
# ──────────────────────────────────────────────

def analyze_failures(records: list, generated: list, n_show: int = 5) -> list:
    """
    Identify likely hallucination/failure cases.
    Heuristics:
    - Short output (< 30 words) → incomplete generation
    - Output repeats input code tokens verbatim → copy failure
    - Section headers missing → structural failure
    """
    from src.utils import PASSPORT_SECTIONS

    failures = []
    for rec, gen in zip(records, generated):
        issues = []
        word_count = len(gen.split())

        if word_count < 30:
            issues.append(f"Very short output ({word_count} words) — possible truncation")

        missing_sections = [s for s in PASSPORT_SECTIONS if s not in gen.upper()]
        if len(missing_sections) >= 4:
            issues.append(f"Missing sections: {', '.join(missing_sections)}")

        # Check if model just repeated the code
        code_tokens = set(rec["original_code"].split()[:20])
        gen_tokens  = set(gen.split())
        overlap = len(code_tokens & gen_tokens) / max(len(code_tokens), 1)
        if overlap > 0.6:
            issues.append("High code-token overlap — possible hallucination/copy")

        if issues:
            failures.append({
                "code_snippet": rec["original_code"][:200],
                "generated":    gen[:300],
                "reference":    rec["target"][:300],
                "issues":       issues,
            })

    return failures[:n_show]


# ──────────────────────────────────────────────
# Main evaluation loop
# ──────────────────────────────────────────────

def evaluate(
    model_path: str,
    test_data_path: str,
    output_file: str,
    num_samples: int,
    is_baseline: bool = False,
) -> dict:
    import torch
    from src.inference import load_model, generate_passport

    print(f"\n📊 CodePassport AI — Evaluation")
    print(f"   Model  : {model_path}")
    print(f"   Test   : {test_data_path}")
    print(f"   Samples: {num_samples}")
    print(f"   Mode   : {'BASELINE (pretrained)' if is_baseline else 'FINE-TUNED (LoRA)'}\n")

    # Load model
    model, tokenizer, device = load_model(model_path, is_baseline=is_baseline)

    # Load test samples
    all_records = read_jsonl(test_data_path)
    random.seed(42)
    if len(all_records) > num_samples:
        records = random.sample(all_records, num_samples)
    else:
        records = all_records

    print(f"⚙️  Generating passports for {len(records)} test samples...")

    references  = []
    hypotheses  = []

    for i, rec in enumerate(records):
        if i % 20 == 0:
            print(f"   {i}/{len(records)}...")

        try:
            generated = generate_passport(rec["original_code"], model, tokenizer, device)
        except Exception as e:
            generated = ""  # count as empty output

        references.append(rec["target"])
        hypotheses.append(generated)

    print(f"\n📐 Computing metrics...")
    bleu_scores  = compute_bleu(references, hypotheses)
    rouge_scores = compute_rouge(references, hypotheses)

    metrics = {**bleu_scores, **rouge_scores}

    # ── Print table ──────────────────────────────
    print("\n" + "═" * 50)
    print(f"  📊  EVALUATION RESULTS")
    print(f"  Model: {os.path.basename(model_path)}")
    print("═" * 50)
    for k, v in metrics.items():
        bar = "█" * int(v / 3)
        print(f"  {k:<12} {v:>6.2f}   {bar}")
    print(f"\n  Avg ref length : {avg_length(references)} words")
    print(f"  Avg gen length : {avg_length(hypotheses)} words")
    print("═" * 50 + "\n")

    # ── Failure analysis ─────────────────────────
    print("🔍 Failure / Hallucination Analysis:")
    failures = analyze_failures(records, hypotheses)
    for i, f in enumerate(failures, 1):
        print(f"\n  Case {i}: {' | '.join(f['issues'])}")
        print(f"  Code  : {f['code_snippet'][:100]}...")
        print(f"  Output: {f['generated'][:150]}...")

    # ── Save results ─────────────────────────────
    ensure_dirs(os.path.dirname(output_file) if os.path.dirname(output_file) else ".")
    result_data = {
        "model":          model_path,
        "is_baseline":    is_baseline,
        "num_samples":    len(records),
        "metrics":        metrics,
        "avg_ref_length": avg_length(references),
        "avg_gen_length": avg_length(hypotheses),
        "failure_cases":  failures,
        "sample_outputs": [
            {
                "code":      records[i]["original_code"][:300],
                "reference": references[i][:400],
                "generated": hypotheses[i][:400],
            }
            for i in range(min(5, len(records)))
        ],
    }

    with open(output_file, "w") as f:
        json.dump(result_data, f, indent=2)

    print(f"\n✅ Results saved → {output_file}")
    return metrics


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CodePassport AI — Evaluation")
    parser.add_argument("--model_path",  required=True)
    parser.add_argument("--test_data",   required=True)
    parser.add_argument("--output_file", default="evaluation/results.json")
    parser.add_argument("--num_samples", type=int, default=200)
    parser.add_argument("--baseline",    action="store_true")
    args = parser.parse_args()

    evaluate(
        model_path=args.model_path,
        test_data_path=args.test_data,
        output_file=args.output_file,
        num_samples=args.num_samples,
        is_baseline=args.baseline,
    )
