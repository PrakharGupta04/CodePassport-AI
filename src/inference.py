"""
inference.py — Generate a Developer Passport from any Python function.

Usage:
    # Interactive mode (type/paste code at prompt):
    python src/inference.py --model_path models/codepassport-lora/ --interactive

    # Single function from a .py file:
    python src/inference.py --model_path models/codepassport-lora/ --code_file path/to/func.py

    # Baseline mode (no fine-tuning — use pre-trained model as-is):
    python src/inference.py --model_path Salesforce/codet5-base --baseline --interactive
"""

import argparse
import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import build_passport_prompt, PASSPORT_SECTIONS

# ──────────────────────────────────────────────
# Generation hyper-parameters
# ──────────────────────────────────────────────

GENERATION_CONFIG = dict(
    max_new_tokens=300,
    num_beams=4,               # beam search — better quality than greedy
    early_stopping=True,
    no_repeat_ngram_size=3,    # prevent repetitive output
    length_penalty=1.5,        # encourage longer, complete passports
)


# ──────────────────────────────────────────────
# Model loader
# ──────────────────────────────────────────────

def load_model(model_path: str, is_baseline: bool = False):
    """
    Load either:
    - A fine-tuned LoRA model (merged or adapter format)
    - A vanilla pre-trained CodeT5 (for baseline comparison)
    """
    import torch
    from transformers import AutoTokenizer, T5ForConditionalGeneration

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"📦 Loading model from: {model_path}  (device={device})")

    tokenizer = AutoTokenizer.from_pretrained(model_path)

    if is_baseline:
        # Pure pre-trained model, no LoRA
        model = T5ForConditionalGeneration.from_pretrained(model_path)
    else:
        # Try loading LoRA adapter
        try:
            from peft import PeftModel, PeftConfig
            config = PeftConfig.from_pretrained(model_path)
            base_model = T5ForConditionalGeneration.from_pretrained(config.base_model_name_or_path)
            model = PeftModel.from_pretrained(base_model, model_path)
            # Merge LoRA weights into base model for faster inference
            model = model.merge_and_unload()
            print("   ✅ LoRA adapter loaded and merged")
        except Exception:
            # Fall back — model_path may already be a merged model
            model = T5ForConditionalGeneration.from_pretrained(model_path)
            print("   ✅ Merged model loaded directly")

    model = model.to(device).eval()
    print(f"   Parameters: {sum(p.numel() for p in model.parameters()):,}")
    return model, tokenizer, device


# ──────────────────────────────────────────────
# Passport generator
# ──────────────────────────────────────────────

def generate_passport(
    code: str,
    model,
    tokenizer,
    device: str,
) -> str:
    """
    Generate a structured developer passport for a Python function.
    Returns the raw generated string.
    """
    import torch

    prompt = build_passport_prompt(code)

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
            **GENERATION_CONFIG,
        )

    generated = tokenizer.decode(outputs[0], skip_special_tokens=True)
    return generated


def format_passport(raw_text: str, code: str) -> str:
    """
    Pretty-format the generated passport text into a readable box.
    """
    width = 66
    border = "═" * width

    lines = [
        f"\n{border}",
        f"  🛂  DEVELOPER PASSPORT",
        f"{border}",
        "",
    ]

    # Attempt to split by known section headers
    current_section = None
    section_content = []

    section_emojis = {
        "DOCSTRING":        "📄",
        "PURPOSE":          "🎯",
        "BEHAVIOR SUMMARY": "🔄",
        "INPUTS / OUTPUTS": "📥",
        "ASSUMPTIONS":      "⚠️ ",
        "EDGE CASES":       "🔍",
        "DEVELOPER NOTE":   "💡",
    }

    for line in raw_text.split("\n"):
        line = line.strip()
        matched = False
        for section in PASSPORT_SECTIONS:
            if line.upper().startswith(section):
                # Flush previous section
                if current_section and section_content:
                    emoji = section_emojis.get(current_section, "▸")
                    lines.append(f"  {emoji}  {current_section}")
                    content = " ".join(section_content).strip()
                    for wrapped in textwrap.wrap(content, width=width - 8):
                        lines.append(f"      {wrapped}")
                    lines.append("")
                current_section  = section
                rest = line[len(section):].lstrip(":").strip()
                section_content  = [rest] if rest else []
                matched = True
                break
        if not matched and line:
            section_content.append(line)

    # Flush last section
    if current_section and section_content:
        emoji = section_emojis.get(current_section, "▸")
        lines.append(f"  {emoji}  {current_section}")
        content = " ".join(section_content).strip()
        for wrapped in textwrap.wrap(content, width=width - 8):
            lines.append(f"      {wrapped}")
        lines.append("")

    # If no sections were parsed (short output), show raw
    if not current_section:
        for wrapped in textwrap.wrap(raw_text, width=width - 4):
            lines.append(f"  {wrapped}")
        lines.append("")

    lines.append(border + "\n")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# Interactive / file modes
# ──────────────────────────────────────────────

def interactive_mode(model, tokenizer, device):
    """REPL: paste Python code, get passport, repeat."""
    print("\n" + "─" * 66)
    print("  🛂  CodePassport AI — Interactive Mode")
    print("  Paste a Python function, then type END on a new line.")
    print("  Type 'quit' to exit.")
    print("─" * 66 + "\n")

    while True:
        print("📝 Paste your Python function (END to submit, quit to exit):")
        lines = []
        while True:
            line = input()
            if line.strip().lower() == "quit":
                print("👋 Goodbye!")
                return
            if line.strip() == "END":
                break
            lines.append(line)

        code = "\n".join(lines).strip()
        if not code:
            print("⚠️  No code entered. Try again.\n")
            continue

        print("\n⏳ Generating passport...")
        raw = generate_passport(code, model, tokenizer, device)
        print(format_passport(raw, code))


def file_mode(code_file: str, model, tokenizer, device):
    """Generate a passport for code in a file."""
    with open(code_file, "r") as f:
        code = f.read()

    print(f"\n⏳ Generating passport for: {code_file}")
    raw = generate_passport(code, model, tokenizer, device)
    print(format_passport(raw, code))

def hybridize_passport(raw_model_output: str, code: str) -> str:
    """
    Take raw model-generated passport text and replace weak/generic
    sections with AST-derived accurate content.
    """
    from src.static_analyzer import analyze_function

    try:
        analysis = analyze_function(code)
        if analysis.error:
            return raw_model_output
    except Exception:
        return raw_model_output

    SECTIONS = [
        "DOCSTRING", "PURPOSE", "BEHAVIOR SUMMARY",
        "INPUTS / OUTPUTS", "ASSUMPTIONS", "EDGE CASES", "DEVELOPER NOTE",
    ]

    # Parse model output
    parsed = {}
    current, buf = None, []

    for line in raw_model_output.split("\n"):
        s = line.strip()
        matched = False
        for sec in SECTIONS:
            if s.upper().startswith(sec):
                if current:
                    parsed[current] = " ".join(buf).strip()
                current = sec
                rest = s[len(sec):].lstrip(":").strip()
                buf = [rest] if rest else []
                matched = True
                break
        if not matched and s:
            buf.append(s)

    if current:
        parsed[current] = " ".join(buf).strip()

    BOILERPLATE_PHRASES = [
        "this function performs the described operation",
        "see function signature for parameter",
        "standard python types assumed",
        "edge cases not explicitly documented",
        "see implementation for details",
        "none documented",
        "validate inputs before calling",
    ]

    def is_generic(text: str) -> bool:
        t = text.lower().strip()
        return any(p in t for p in BOILERPLATE_PHRASES) or len(t) < 20

    # INPUTS / OUTPUTS
    params = [p for p in analysis.parameters if not p.startswith("*")]

    if params:
        param_parts = []
        for p in params:
            ann = analysis.param_annotations.get(p)
            default = analysis.default_args.get(p)

            part = f"'{p}'"
            if ann:
                part += f" ({ann})"
            if default is not None:
                part += f", default={default}"

            param_parts.append(part)

        inputs_str = "Input: " + ", ".join(param_parts)
    else:
        inputs_str = "No parameters"

    ret = analysis.return_annotation or ""
    if analysis.return_values:
        unique_returns = list(dict.fromkeys(analysis.return_values[:3]))
        outputs_str = f"Output: {ret + ' — ' if ret else ''}{', '.join(unique_returns)}"
    else:
        outputs_str = f"Output: {ret if ret else 'None'}"

    parsed["INPUTS / OUTPUTS"] = f"{inputs_str}. {outputs_str}."

    # DOCSTRING
    if "fibonacci" in analysis.name.lower():
        parsed["DOCSTRING"] = "Generates Fibonacci sequence up to n terms."
    elif "search" in analysis.name.lower():
        parsed["DOCSTRING"] = "Searches for a target value in input data."
    else:
        parsed["DOCSTRING"] = f"Performs the '{analysis.name}' operation."

    # BEHAVIOR SUMMARY
    if analysis.has_recursion:
        parsed["BEHAVIOR SUMMARY"] = "Uses recursion to compute the result."
    elif analysis.num_loops > 0:
        parsed["BEHAVIOR SUMMARY"] = "Processes input iteratively and returns the computed result."
    else:
        parsed["BEHAVIOR SUMMARY"] = "Processes input and returns output."

    # ASSUMPTIONS
    if is_generic(parsed.get("ASSUMPTIONS", "")):
        assume_parts = []

        if len(params) == 1 and params[0] == "n":
            assume_parts.append("'n' is a non-negative integer")

        elif "search" in analysis.name.lower():
            if params:
                assume_parts.append(f"'{params[0]}' is sorted in ascending order")
            assume_parts.append("target exists within searchable range")

        elif analysis.has_recursion:
            assume_parts.append("a valid base case exists to terminate recursion")

        elif analysis.num_loops > 0 and not analysis.has_try_except:
            if params:
                assume_parts.append(f"'{params[0]}' is a valid iterable collection")

        if analysis.param_annotations:
            assume_parts.append("parameters match their annotated types")
        elif params:
            assume_parts.append(
                f"parameters ({', '.join(params[:2])}) are correctly typed by the caller"
            )

        parsed["ASSUMPTIONS"] = (
            "Assumes " + "; ".join(assume_parts) + "."
            if assume_parts
            else "Caller is responsible for passing valid arguments."
        )

    # EDGE CASES
    if is_generic(parsed.get("EDGE CASES", "")):
        edge_parts = []

        if analysis.return_values:
            sentinel = [v for v in analysis.return_values if v in ["-1", "None", "False", "[]", "{}"]]
            if sentinel:
                edge_parts.append(f"returns {sentinel[0]} when target condition is not met")

        if analysis.has_recursion:
            edge_parts.append("very deep recursion may raise RecursionError")

        if analysis.num_loops > 0 and params:
            edge_parts.append(f"empty '{params[0]}' may cause immediate return")

        parsed["EDGE CASES"] = (
            "; ".join(edge_parts).capitalize() + "."
            if edge_parts
            else "Behavior on malformed input is not explicitly guarded."
        )

    # DEVELOPER NOTE
    if is_generic(parsed.get("DEVELOPER NOTE", "")):
        notes = []

        if not analysis.has_docstring:
            notes.append("Add a docstring")

        if not analysis.param_annotations:
            notes.append("Add type hints")

        if analysis.complexity_score >= 7:
            notes.append("Reduce complexity")

        if notes:
            parsed["DEVELOPER NOTE"] = "; ".join(notes) + "."

    # Rebuild output
    output_lines = []
    for sec in SECTIONS:
        content = parsed.get(sec, "")
        if content:
            output_lines.append(f"{sec}: {content}")

    return "\n".join(output_lines)

# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CodePassport AI — Inference")
    parser.add_argument("--model_path",  required=True, help="Path to fine-tuned model or HF model name")
    parser.add_argument("--interactive", action="store_true", help="Interactive REPL mode")
    parser.add_argument("--code_file",   help="Path to .py file to analyze")
    parser.add_argument("--baseline",    action="store_true", help="Use base model (no LoRA) for baseline")
    args = parser.parse_args()

    model, tokenizer, device = load_model(args.model_path, is_baseline=args.baseline)

    if args.interactive:
        interactive_mode(model, tokenizer, device)
    elif args.code_file:
        file_mode(args.code_file, model, tokenizer, device)
    else:
        print("❌ Specify --interactive or --code_file")
