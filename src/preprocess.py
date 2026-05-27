"""
preprocess.py — Data cleaning, target construction, and train/val/test splitting.
v3: Stricter quality filtering, grammar/symbol detection, deduplication,
    placeholder detection, and improved logging.

Usage:
    PYTHONPATH=. python src/preprocess.py \
        --input  data/raw/dataset.jsonl \
        --output_dir data/processed/
"""

import argparse
import random
import re
import sys
import os
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import (
    read_jsonl,
    write_jsonl,
    build_passport_prompt,
    build_passport_target,
    clean_docstring,
    split_sentences,
    ensure_dirs,
    _is_generic,
    _symbol_density,
    _GRAMMAR_LIKE_RE,
    _GENERIC_PHRASES,
)

# ──────────────────────────────────────────────
# Filter thresholds
# ──────────────────────────────────────────────

MIN_CODE_CHARS      = 80
MAX_CODE_CHARS      = 2500
MIN_CLEAN_DOC_CHARS = 30       # minimum chars AFTER cleaning
MAX_CLEAN_DOC_CHARS = 900
MIN_CLEAN_DOC_WORDS = 6        # minimum words in cleaned doc
MAX_SYMBOL_DENSITY  = 0.28     # >28% non-alphanumeric → likely junk
MIN_SENTENCE_LENGTH = 4        # minimum words in the primary sentence
MAX_PLACEHOLDER_RATIO = 0.4    # if >40% of target is boilerplate → skip

# Keywords in docstrings that indicate the string is not natural language
_NON_NL_PATTERNS = re.compile(
    r"""
    (?:
        ::=                          # BNF
        |[A-Z_]{2,}\s*\|            # GRAMMAR_RULE | OTHER
        |\bSTRING\b|\bNUMBER\b|\bIDENT\b|\bTOKEN\b|\bEXPR\b
        |%[a-z]{2,}                  # %token
        |<[A-Z][A-Z_]+>              # <NONTERMINAL>
        |\$\{?\w+\}?                 # $variable
        |\[\[.*?\]\]                 # [[wiki link]]
        |^\s*>>>\s                   # doctest
        |^\s*\.\.\s                  # rst continuation
    )
    """,
    re.VERBOSE | re.MULTILINE,
)

# Docs that are purely code references or import paths
_CODE_PATH_RE = re.compile(
    r'^[\w\.]+\.[\w\.]+\s*(?:\(.*\))?\s*$'
)

# Placeholder phrases that indicate a low-quality doc
_PLACEHOLDER_TRIGGERS = {
    "todo", "fixme", "hack", "deprecated", "not implemented",
    "placeholder", "stub", "pass", "raises notimplemented",
    "to be implemented", "work in progress", "wip",
}


def _is_doctest_only(doc: str) -> bool:
    """Return True if the docstring consists almost entirely of doctest examples."""
    lines = [l.strip() for l in doc.splitlines() if l.strip()]
    if not lines:
        return True
    doctest_lines = sum(1 for l in lines if l.startswith(">>>") or l.startswith("..."))
    return doctest_lines / len(lines) > 0.5


def _is_grammar_like(doc: str) -> bool:
    """Return True if the docstring looks like a parser grammar or BNF rule."""
    if _GRAMMAR_LIKE_RE.search(doc):
        return True
    if _NON_NL_PATTERNS.search(doc):
        return True
    return False


def _is_symbol_heavy(doc: str) -> bool:
    """Return True if the docstring has too high a density of non-alphanumeric chars."""
    # Check the first 300 chars (the summary, before any Args: block)
    sample = doc[:300]
    return _symbol_density(sample) > MAX_SYMBOL_DENSITY


def _is_placeholder_heavy(doc: str) -> bool:
    """Return True if the docstring is dominated by placeholder/boilerplate phrases."""
    doc_lower = doc.lower()
    return any(trigger in doc_lower for trigger in _PLACEHOLDER_TRIGGERS)


def _is_code_path(doc: str) -> bool:
    """Return True if the doc is just a dotted module path like 'module.submodule.func()'."""
    first_line = doc.strip().splitlines()[0].strip()
    return bool(_CODE_PATH_RE.match(first_line)) and len(doc.strip().splitlines()) <= 2


def _has_repetitive_sentences(sentences: list, threshold: float = 0.80) -> bool:
    """
    Return True if too many sentences in the doc are near-duplicates of each other.
    Uses Jaccard similarity on word sets.
    """
    if len(sentences) < 2:
        return False

    def jaccard(a: str, b: str) -> float:
        wa = set(a.lower().split())
        wb = set(b.lower().split())
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / len(wa | wb)

    for i in range(len(sentences) - 1):
        if jaccard(sentences[i], sentences[i + 1]) > threshold:
            return True
    return False


def _target_placeholder_ratio(target: str) -> float:
    """
    Compute the fraction of the target that consists of known placeholder phrases.
    Returns 0.0 to 1.0.
    """
    if not target:
        return 1.0
    target_lower = target.lower()
    total_words  = len(target_lower.split())
    if total_words == 0:
        return 1.0

    placeholder_words = 0
    for phrase in _GENERIC_PHRASES:
        if phrase in target_lower:
            placeholder_words += len(phrase.split())

    return min(placeholder_words / total_words, 1.0)


def _extract_func_name(code: str) -> str:
    """Extract function name for deduplication."""
    m = re.search(r'def\s+(\w+)\s*\(', code)
    return m.group(1) if m else ""


def is_quality_sample(
    code: str,
    raw_doc: str,
    clean_doc: str,
) -> tuple:
    """
    Multi-stage quality gate.
    Returns (True, "") if sample passes, or (False, reason_string) if rejected.
    """
    # ── Code length ────────────────────────────────────────
    if len(code) < MIN_CODE_CHARS:
        return False, "code_too_short"
    if len(code) > MAX_CODE_CHARS:
        return False, "code_too_long"

    # ── Raw doc sanity ─────────────────────────────────────
    if not raw_doc or not raw_doc.strip():
        return False, "empty_doc"

    # ── Doctest-only ───────────────────────────────────────
    if _is_doctest_only(raw_doc):
        return False, "doctest_only"

    # ── Grammar/parser-like doc ────────────────────────────
    if _is_grammar_like(raw_doc):
        return False, "grammar_like"

    # ── Symbol-heavy doc ──────────────────────────────────
    if _is_symbol_heavy(raw_doc):
        return False, "symbol_heavy"

    # ── Placeholder-heavy doc ─────────────────────────────
    if _is_placeholder_heavy(raw_doc):
        return False, "placeholder_heavy"

    # ── Code path doc (not natural language) ──────────────
    if _is_code_path(raw_doc):
        return False, "code_path_doc"

    # ── Cleaned doc length ────────────────────────────────
    if len(clean_doc) < MIN_CLEAN_DOC_CHARS:
        return False, "clean_doc_too_short"
    if len(clean_doc) > MAX_CLEAN_DOC_CHARS:
        return False, "clean_doc_too_long"
    if len(clean_doc.split()) < MIN_CLEAN_DOC_WORDS:
        return False, "clean_doc_too_few_words"

    # ── Sentence quality ──────────────────────────────────
    sentences = split_sentences(clean_doc)
    if not sentences:
        return False, "no_sentences"

    primary = sentences[0]
    if len(primary.split()) < MIN_SENTENCE_LENGTH:
        return False, "primary_sentence_too_short"

    # ── Repetitive sentences ──────────────────────────────
    if _has_repetitive_sentences(sentences):
        return False, "repetitive_sentences"

    # ── Doc should not be just the function name ──────────
    func_name = _extract_func_name(code)
    if func_name:
        # Strip underscores and compare case-insensitively
        readable_name = func_name.replace("_", " ").lower()
        if clean_doc.lower().strip().rstrip(".") in (
            func_name.lower(),
            readable_name,
        ):
            return False, "doc_is_just_func_name"

    return True, ""


def build_and_validate_target(code: str, raw_doc: str, clean_doc: str) -> tuple:
    """
    Build the passport target and validate it isn't dominated by placeholders.
    Returns (target_string, True) or ("", False).
    """
    target = build_passport_target(raw_doc, code=code)

    if not target or not target.strip():
        return "", False

    # Check placeholder ratio in the generated target
    ratio = _target_placeholder_ratio(target)
    if ratio > MAX_PLACEHOLDER_RATIO:
        return "", False

    # Check that at minimum DOCSTRING and one other section exist
    required = ["DOCSTRING:", "PURPOSE:", "BEHAVIOR SUMMARY:"]
    has_required = sum(1 for r in required if r in target)
    if has_required < 2:
        return "", False

    return target, True


# ──────────────────────────────────────────────
# Main pipeline
# ──────────────────────────────────────────────

def preprocess(
    input_path: str,
    output_dir: str,
    seed: int = 42,
    max_samples: int = None,
) -> None:

    print(f"\n📂 Loading dataset: {input_path}")
    raw_records = read_jsonl(input_path)
    print(f"   Raw records: {len(raw_records):,}")

    if max_samples:
        raw_records = raw_records[:max_samples]
        print(f"   Limited to: {max_samples:,} for testing")

    # ── Filter + Build ────────────────────────────────────
    print("\n🔍 Filtering and building passport targets...")

    rejection_counter = Counter()
    processed         = []
    seen_func_names   = Counter()   # for deduplication of very common func names

    for i, record in enumerate(raw_records):
        if i % 2000 == 0 and i > 0:
            print(f"   Processed {i:,}/{len(raw_records):,} "
                  f"(kept {len(processed):,} so far)...")

        code    = record.get("func_code_string", "").strip()
        raw_doc = record.get("func_documentation_string", "").strip()

        # Clean docstring first
        clean_doc = clean_docstring(raw_doc)

        # Quality gate
        passes, reason = is_quality_sample(code, raw_doc, clean_doc)
        if not passes:
            rejection_counter[reason] += 1
            continue

        # Deduplicate: skip if we've seen this function name 5+ times
        # (common names like 'get', 'set', 'run' appear hundreds of times)
        func_name = _extract_func_name(code)
        if func_name and seen_func_names[func_name] >= 5:
            rejection_counter["duplicate_func_name"] += 1
            continue
        if func_name:
            seen_func_names[func_name] += 1

        # Build and validate passport target
        target, valid = build_and_validate_target(code, raw_doc, clean_doc)
        if not valid:
            rejection_counter["weak_target"] += 1
            continue

        prompt = build_passport_prompt(code)

        processed.append({
            "prompt":        prompt,
            "target":        target,
            "original_code": code,
            "original_doc":  raw_doc,
            "clean_doc":     clean_doc,
        })

    # ── Report ────────────────────────────────────────────
    total_rejected = len(raw_records) - len(processed)
    print(f"\n📊 Filter report:")
    print(f"   Kept     : {len(processed):,} ({len(processed)/len(raw_records)*100:.1f}%)")
    print(f"   Rejected : {total_rejected:,} ({total_rejected/len(raw_records)*100:.1f}%)")
    print(f"\n   Rejection breakdown:")
    for reason, count in rejection_counter.most_common():
        print(f"     {reason:<35} {count:>5}")

    if len(processed) < 1000:
        print(
            "\n⚠️  WARNING: Fewer than 1000 samples kept. "
            "Consider relaxing MIN_CLEAN_DOC_WORDS or MIN_CLEAN_DOC_CHARS."
        )

    # ── Split ─────────────────────────────────────────────
    random.seed(seed)
    random.shuffle(processed)

    n       = len(processed)
    n_val   = int(n * 0.10)
    n_test  = int(n * 0.05)
    n_train = n - n_val - n_test

    train_data = processed[:n_train]
    val_data   = processed[n_train:n_train + n_val]
    test_data  = processed[n_train + n_val:]

    print(f"\n📊 Split:")
    print(f"   Train : {len(train_data):,}")
    print(f"   Val   : {len(val_data):,}")
    print(f"   Test  : {len(test_data):,}")

    # ── Save ──────────────────────────────────────────────
    ensure_dirs(output_dir)
    write_jsonl(train_data, os.path.join(output_dir, "train.jsonl"))
    write_jsonl(val_data,   os.path.join(output_dir, "val.jsonl"))
    write_jsonl(test_data,  os.path.join(output_dir, "test.jsonl"))

    # ── Sample preview ────────────────────────────────────
    print("\n👀 Sample (first accepted record):")
    print("─" * 64)
    ex = processed[0]
    print("CODE (first 3 lines):")
    print("\n".join(ex["original_code"].splitlines()[:3]))
    print("\nCLEAN DOC:")
    print(ex["clean_doc"][:200])
    print("\nTARGET:")
    print(ex["target"])
    print("─" * 64)
    print("\n✅ Preprocessing complete!\n")


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CodePassport AI — Preprocessing v3")
    parser.add_argument("--input",       required=True, help="Path to raw dataset.jsonl")
    parser.add_argument("--output_dir",  required=True, help="Directory for processed splits")
    parser.add_argument("--seed",        type=int, default=42)
    parser.add_argument("--max_samples", type=int, default=None,
                        help="Limit records for quick testing (e.g. 2000)")
    args = parser.parse_args()

    preprocess(
        input_path=args.input,
        output_dir=args.output_dir,
        seed=args.seed,
        max_samples=args.max_samples,
    )