"""
utils.py — Shared helpers for CodePassport AI
v3: Fixed label quality, removed boilerplate placeholders,
    improved docstring cleaning, AST-enhanced targets.
"""

import json
import os
import re
from pathlib import Path

# ──────────────────────────────────────────────
# Passport section labels
# ──────────────────────────────────────────────
PASSPORT_SECTIONS = [
    "DOCSTRING",
    "PURPOSE",
    "BEHAVIOR SUMMARY",
    "INPUTS / OUTPUTS",
    "ASSUMPTIONS",
    "EDGE CASES",
    "DEVELOPER NOTE",
]


def build_passport_prompt(code: str) -> str:
    """
    Build the input prompt fed to the model during training and inference.
    """
    prompt = (
        "Generate a structured developer passport for the following Python function.\n"
        "Include these sections: DOCSTRING, PURPOSE, BEHAVIOR SUMMARY, "
        "INPUTS / OUTPUTS, ASSUMPTIONS, EDGE CASES, DEVELOPER NOTE.\n\n"
        f"### Python Function:\n{code.strip()}\n\n"
        "### Developer Passport:\n"
    )
    return prompt


# ──────────────────────────────────────────────
# Docstring cleaning
# ──────────────────────────────────────────────

# Patterns that mark the start of structured metadata blocks
# Everything from these markers to the next blank line or end is stripped
_METADATA_SECTION_RE = re.compile(
    r"""
    ^[ \t]*                          # optional leading whitespace
    (?:
        Args?|Arguments?|            # Args:
        Returns?|                    # Returns:
        Raises?|                     # Raises:
        Parameters?|                 # Parameters:
        Params?|                     # Params:
        Attributes?|                 # Attributes:
        Yields?|                     # Yields:
        Examples?|                   # Examples:
        Notes?|                      # Notes:
        References?|                 # References:
        See\ Also|                   # See Also:
        Todo                         # Todo:
    )
    \s*:                             # followed by colon
    """,
    re.VERBOSE | re.MULTILINE | re.IGNORECASE,
)

# Sphinx/NumPy/Google style inline param tags
_INLINE_TAG_RE = re.compile(
    r"""
    ^\s*
    (?:
        :param\s+\w+:|               # :param name:
        :type\s+\w+:|                # :type name:
        :returns?:|                  # :returns:
        :rtype:|                     # :rtype:
        :raises?\s+\w+:|             # :raises ExcType:
        @param\s+|                   # @param (javadoc style)
        @return\s+|                  # @return
        @type\s+|                    # @type
        \$\w+|                       # $variable (shell-like)
        \*\*\w+\s*--                 # **param -- (numpy style)
    )
    """,
    re.VERBOSE | re.MULTILINE | re.IGNORECASE,
)

# Lines that look like grammar/parser rules or symbol-heavy junk
_GRAMMAR_LIKE_RE = re.compile(
    r"""
    (?:
        [A-Z_]{2,}\s*\|             # IDENT | STRING
        |\bSTRING\b                  # literal "STRING"
        |\bNUMBER\b                  # literal "NUMBER"
        |\bIDENT\b                   # literal "IDENT"
        |\bTOKEN\b                   # literal "TOKEN"
        |::=                         # BNF rule
        |<\w+>                       # <nonterminal>
        |%[a-z]+                     # %token etc
    )
    """,
    re.VERBOSE,
)

# Symbol density check: too many non-alphanumeric chars → junk
def _symbol_density(text: str) -> float:
    if not text:
        return 0.0
    non_alpha = sum(1 for c in text if not c.isalnum() and not c.isspace())
    return non_alpha / len(text)


def clean_docstring(raw_doc: str) -> str:
    """
    Strip structured metadata (Args/Returns/Raises blocks, :param: tags, etc.)
    from a raw docstring and return only the natural-language summary portion.

    Strategy:
    1. Split into lines
    2. Find the first metadata section marker — everything before it is the summary
    3. Strip inline :param:/:type: tags line by line
    4. Collapse whitespace and return clean text
    """
    if not raw_doc:
        return ""

    lines = raw_doc.strip().splitlines()

    # ── Pass 1: Find where metadata begins ──────────────────
    # Walk lines and stop at the first metadata section header
    summary_lines = []
    for line in lines:
        # Check for section header like "Args:", "Returns:", etc.
        if _METADATA_SECTION_RE.match(line):
            break
        # Check for inline Sphinx tags
        if _INLINE_TAG_RE.match(line):
            break
        # Check for numpy-style "param_name : type" separator lines
        # These look like: "    x : int" with at least one space around the colon
        if re.match(r'^\s+\w[\w\s]*\s:\s', line):
            # Only break if we already have some content (avoid false positives on first line)
            if summary_lines:
                break
        summary_lines.append(line)

    # ── Pass 2: Strip any remaining inline tags ─────────────
    cleaned = []
    for line in summary_lines:
        # Skip lines that are purely a metadata tag
        if _INLINE_TAG_RE.match(line):
            continue
        # Remove inline tags embedded mid-line (rare but happens)
        line = re.sub(r':(?:param|type|returns?|rtype|raises?)\s+\w+:', '', line)
        cleaned.append(line)

    # ── Pass 3: Collapse and clean up ───────────────────────
    # Join, normalize whitespace, remove leading/trailing blank lines
    text = " ".join(
        l.strip() for l in cleaned if l.strip()
    )

    # Remove trailing punctuation artifacts
    text = re.sub(r'\s{2,}', ' ', text)
    text = text.strip().strip(".")

    # Re-add a single period if the text doesn't end with punctuation
    if text and text[-1] not in ".!?":
        text += "."

    return text


# ──────────────────────────────────────────────
# Sentence utilities
# ──────────────────────────────────────────────

def split_sentences(text: str) -> list:
    """
    Split text into sentences properly.
    Handles abbreviations, decimals, and common edge cases better
    than a simple split on '.'.
    """
    if not text:
        return []

    # Protect common abbreviations
    protected = re.sub(
        r'\b(e\.g|i\.e|etc|vs|fig|Dr|Mr|Mrs|Ms|Prof|Sr|Jr|St)\.',
        lambda m: m.group(0).replace('.', '\x00'),
        text
    )

    # Split on sentence-ending punctuation followed by whitespace and uppercase
    parts = re.split(r'(?<=[.!?])\s+(?=[A-Z])', protected)

    result = []
    for part in parts:
        # Restore protected dots
        part = part.replace('\x00', '.').strip()
        if part:
            result.append(part)

    return result if result else [text.strip()]


def _extract_func_name_and_params(code: str) -> tuple:
    """
    Extract function name and parameter list from code using simple regex.
    Returns (name, [param_names]) or ("", []) on failure.
    Falls back gracefully if AST is unavailable.
    """
    try:
        import ast
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                params = []
                for arg in node.args.args:
                    if arg.arg not in ("self", "cls"):
                        params.append(arg.arg)
                return node.name, params
    except Exception:
        pass

    # Regex fallback
    m = re.search(r'def\s+(\w+)\s*\(([^)]*)\)', code)
    if m:
        name = m.group(1)
        raw_params = m.group(2)
        params = []
        for p in raw_params.split(","):
            p = p.strip().split(":")[0].split("=")[0].strip().lstrip("*")
            if p and p not in ("self", "cls") and re.match(r'^\w+$', p):
                params.append(p)
        return name, params

    return "", []


def _get_return_values(code: str) -> list:
    """Extract distinct return value representations from code."""
    try:
        import ast
        tree = ast.parse(code)
        values = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Return) and node.value is not None:
                try:
                    values.append(ast.unparse(node.value))
                except Exception:
                    pass
        # Deduplicate preserving order
        seen, unique = set(), []
        for v in values:
            if v not in seen:
                seen.add(v)
                unique.append(v)
        return unique[:4]
    except Exception:
        return []


def _has_loops(code: str) -> tuple:
    """Return (has_for, has_while)."""
    try:
        import ast
        tree = ast.parse(code)
        has_for   = any(isinstance(n, ast.For)   for n in ast.walk(tree))
        has_while = any(isinstance(n, ast.While) for n in ast.walk(tree))
        return has_for, has_while
    except Exception:
        has_for   = "for "   in code or "\tfor " in code
        has_while = "while " in code
        return has_for, has_while


def _has_recursion(code: str, func_name: str) -> bool:
    """Check if the function calls itself."""
    if not func_name:
        return False
    try:
        import ast
        tree = ast.parse(code)
        func_def = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_def = node
                break
        if func_def is None:
            return False
        for node in ast.walk(func_def):
            if isinstance(node, ast.Call):
                try:
                    if ast.unparse(node.func) == func_name:
                        return True
                except Exception:
                    pass
        return False
    except Exception:
        # Regex fallback
        pattern = rf'\b{re.escape(func_name)}\s*\('
        lines = code.splitlines()
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("def "):
                continue
            if re.search(pattern, stripped):
                return True
        return False


# ──────────────────────────────────────────────
# Core: build_passport_target
# ──────────────────────────────────────────────

# Placeholder phrases to detect and replace
_GENERIC_PHRASES = {
    "standard python types assumed",
    "standard python types and valid inputs assumed",
    "see implementation for details",
    "see function signature for parameter and return",
    "see function signature for parameter and return type details",
    "edge cases not explicitly documented",
    "edge cases depend on caller",
    "validate inputs before calling",
    "validate inputs before use",
    "this function performs the described operation",
    "none documented",
    "caller is responsible",
    "no edge cases for well-typed inputs",
    "refer to source code for implementation details",
}


def _is_generic(text: str) -> bool:
    """Return True if the text is a known generic placeholder."""
    if not text:
        return True
    t = text.lower().strip().rstrip(".")
    return t in _GENERIC_PHRASES or len(t.split()) < 5


def _make_docstring(sentences: list, clean_doc: str) -> str:
    """
    Build the DOCSTRING field: the single clearest summary sentence.
    Prefers the first well-formed sentence that does not start with a verb
    in an action-list style.
    """
    for s in sentences[:3]:
        s = s.strip()
        if len(s.split()) >= 5 and not s.startswith(("Args", "Return", "Param", "Note")):
            # Ensure it ends with a period
            if s and s[-1] not in ".!?":
                s += "."
            return s
    return (clean_doc[:120].strip() + ".") if clean_doc else ""


def _make_purpose(sentences: list, func_name: str, params: list) -> str:
    """
    Build the PURPOSE field: why this function exists.
    Must be DIFFERENT from DOCSTRING — uses second sentence or
    constructs purpose from function name + params.
    """
    # Try sentences[1] or sentences[2] if they exist and are different
    for s in sentences[1:4]:
        s = s.strip()
        if (
            len(s.split()) >= 5
            and not s.startswith(("Args", "Return", "Param", "Note", ":"))
            and not _is_generic(s)
        ):
            if s[-1] not in ".!?":
                s += "."
            return s

    # Construct from function name when sentences are insufficient
    if func_name:
        readable_name = re.sub(r'([a-z])([A-Z])', r'\1 \2',
                               func_name.replace("_", " ")).lower()
        if params:
            param_str = ", ".join(f"'{p}'" for p in params[:3])
            return f"Provides functionality to {readable_name} given {param_str}."
        return f"Implements the '{readable_name}' operation."

    return ""


def _make_behavior_summary(
    sentences: list,
    func_name: str,
    params: list,
    return_values: list,
    has_for: bool,
    has_while: bool,
    is_recursive: bool,
) -> str:
    """
    Build the BEHAVIOR SUMMARY field: HOW the function works mechanically.
    Must be different from both DOCSTRING and PURPOSE.
    Prioritizes AST-derived facts over docstring sentences.
    """
    parts = []

    # Algorithmic pattern detection
    if is_recursive:
        parts.append(
            f"Uses recursion — '{func_name}' calls itself to break the problem "
            f"into smaller subproblems until a base case is reached."
        )
    if has_while and not is_recursive:
        parts.append(
            "Iterates using a while loop, updating state each iteration "
            "until a termination condition is met."
        )
    if has_for and not is_recursive:
        parts.append(
            "Iterates over a collection using a for loop, "
            "processing each element in sequence."
        )

    # Return behavior
    if return_values:
        unique = list(dict.fromkeys(return_values))
        if len(unique) == 1:
            parts.append(f"Returns {unique[0]} upon completion.")
        elif len(unique) == 2:
            parts.append(
                f"Returns {unique[0]} on success and {unique[1]} "
                f"when the operation fails or the target is not found."
            )
        elif len(unique) >= 3:
            parts.append(
                f"May return one of several values depending on execution path: "
                f"{', '.join(unique[:3])}."
            )

    if parts:
        return " ".join(parts)

    # Fall back to a later sentence from the docstring that hasn't been used yet
    for s in sentences[2:6]:
        s = s.strip()
        if len(s.split()) >= 6 and not _is_generic(s):
            if s[-1] not in ".!?":
                s += "."
            return s

    # Last resort: construct from available facts
    if func_name and params:
        return (
            f"Accepts {len(params)} argument(s) ({', '.join(params[:3])}) "
            f"and performs the '{func_name.replace('_', ' ')}' operation, "
            f"returning a computed result."
        )

    return ""


def _make_inputs_outputs(params: list, return_values: list, code: str) -> str:
    """
    Build INPUTS/OUTPUTS from actual AST facts — never from docstring text.
    This is always derivable and should never be a placeholder.
    """
    try:
        import ast as _ast

        tree   = _ast.parse(code)
        func   = None
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                func = node
                break

        if func:
            # Build typed parameter string
            param_parts = []
            args      = func.args
            all_args  = args.posonlyargs + args.args + args.kwonlyargs
            defaults  = args.defaults

            # Map defaults to the last N args
            default_map = {}
            if defaults:
                defaulted = all_args[-len(defaults):]
                for a, d in zip(defaulted, defaults):
                    try:
                        default_map[a.arg] = _ast.unparse(d)
                    except Exception:
                        pass

            for arg in all_args:
                if arg.arg in ("self", "cls"):
                    continue
                part = arg.arg
                if arg.annotation:
                    try:
                        part += f": {_ast.unparse(arg.annotation)}"
                    except Exception:
                        pass
                if arg.arg in default_map:
                    part += f" = {default_map[arg.arg]}"
                param_parts.append(part)

            if args.vararg:
                part = f"*{args.vararg.arg}"
                if args.vararg.annotation:
                    try:
                        part += f": {_ast.unparse(args.vararg.annotation)}"
                    except Exception:
                        pass
                param_parts.append(part)

            if args.kwarg:
                param_parts.append(f"**{args.kwarg.arg}")

            # Return type
            ret_annotation = ""
            if func.returns:
                try:
                    ret_annotation = _ast.unparse(func.returns)
                except Exception:
                    pass

            inputs_str = (
                f"Input: {', '.join(param_parts)}"
                if param_parts
                else "Input: none"
            )

            if ret_annotation:
                outputs_str = f"Output: {ret_annotation}"
            elif return_values:
                unique = list(dict.fromkeys(return_values))
                outputs_str = f"Output: {', '.join(unique[:3])}"
            else:
                outputs_str = "Output: None"

            return f"{inputs_str}. {outputs_str}."
    except Exception:
        pass

    # Pure regex fallback (no AST available)
    if params:
        inputs_str = f"Input: {', '.join(params[:5])}"
    else:
        inputs_str = "Input: none"

    if return_values:
        outputs_str = f"Output: {', '.join(return_values[:3])}"
    else:
        outputs_str = "Output: not specified"

    return f"{inputs_str}. {outputs_str}."


def _make_assumptions(
    params: list,
    func_name: str,
    has_for: bool,
    has_while: bool,
    is_recursive: bool,
    clean_doc: str,
) -> str:
    """
    Build ASSUMPTIONS dynamically from code facts.
    Never returns a generic placeholder.
    """
    parts = []

    # Sorted input assumption — detectable from docstring keywords
    doc_lower = clean_doc.lower()
    if "sorted" in doc_lower or "ascending" in doc_lower or "descending" in doc_lower:
        parts.append("input collection is sorted in the expected order")

    # Non-empty collection assumption
    if (has_for or has_while) and params:
        first_param = params[0]
        parts.append(f"'{first_param}' is a non-empty iterable")

    # Recursion base case assumption
    if is_recursive:
        parts.append("a valid base case exists to terminate recursion")

    # Index-based access assumption
    if params and (
        "index" in func_name.lower()
        or "search" in func_name.lower()
        or any(p in ("arr", "lst", "array", "list", "nums", "items") for p in params)
    ):
        parts.append("indexed elements support comparison operators")

    # Parameter type assumption
    if params and not parts:
        parts.append(
            f"caller passes correctly typed arguments for {', '.join(params[:3])}"
        )

    if parts:
        return "Assumes " + "; ".join(parts) + "."

    # If we can say something from the doc
    if clean_doc:
        doc_sentences = split_sentences(clean_doc)
        for s in doc_sentences:
            lower = s.lower()
            if any(w in lower for w in ("assume", "expect", "must", "should", "require", "valid", "non-empty")):
                return s if s[-1] in ".!?" else s + "."

    # Dynamic fallback using function name
    if func_name:
        readable = func_name.replace("_", " ")
        return f"Input to '{readable}' is expected to be well-formed and within the function's documented contract."

    return f"Caller is responsible for passing valid and well-typed arguments."


def _make_edge_cases(
    params: list,
    return_values: list,
    func_name: str,
    has_for: bool,
    has_while: bool,
    is_recursive: bool,
    clean_doc: str,
) -> str:
    """
    Build EDGE CASES dynamically from code facts.
    Never returns a generic placeholder.
    """
    parts = []
    doc_lower = clean_doc.lower()

    # Sentinel return values → failure/miss edge case
    sentinel_map = {
        "-1":    "returns -1 if the target is not found",
        "None":  "returns None if no result is available",
        "False": "returns False on failure",
        "[]":    "returns an empty list when no elements match",
        "{}":    "returns an empty dict when no entries match",
        "0":     "returns 0 in the base case or when input is empty",
    }
    for rv in return_values:
        if rv in sentinel_map and sentinel_map[rv] not in " ".join(parts):
            parts.append(sentinel_map[rv])

    # Empty input edge case
    if (has_for or has_while) and params:
        first = params[0]
        parts.append(f"empty '{first}' causes immediate return or loop to be skipped")

    # Recursion depth edge case
    if is_recursive:
        parts.append(
            "very deep input (> ~1000 levels) may raise RecursionError "
            "due to Python's default recursion limit"
        )

    # Doc-mentioned edge cases
    for s in split_sentences(clean_doc):
        lower = s.lower()
        if any(w in lower for w in (
            "none", "empty", "zero", "null", "negative", "overflow",
            "underflow", "invalid", "missing", "not found", "edge",
            "boundary", "limit", "maximum", "minimum"
        )):
            cleaned = s.strip()
            if cleaned and not _is_generic(cleaned):
                if cleaned[-1] not in ".!?":
                    cleaned += "."
                parts.append(cleaned)
                break

    if parts:
        # Capitalize first, join with semicolons
        result_parts = []
        for i, p in enumerate(parts[:3]):
            p = p.strip()
            if i == 0:
                p = p[0].upper() + p[1:]
            result_parts.append(p)
        return "; ".join(result_parts) + "."

    # Dynamic fallback
    if params:
        return (
            f"Behavior is undefined for null or incorrectly typed '{params[0]}'; "
            f"callers should validate input before calling '{func_name}'."
        )

    return f"Behavior on unexpected input to '{func_name}' is not explicitly handled."


def _make_developer_note(
    func_name: str,
    params: list,
    return_values: list,
    is_recursive: bool,
    has_for: bool,
    has_while: bool,
    sentences: list,
    code: str,
) -> str:
    """
    Build DEVELOPER NOTE with practical, function-specific guidance.
    Never returns a generic placeholder.
    """
    parts = []

    # Recursion note
    if is_recursive:
        parts.append(
            f"Recursive implementation — for large inputs consider an iterative "
            f"version using an explicit stack to avoid RecursionError."
        )

    # Complexity hint based on structure
    has_nested_loop = False
    try:
        import ast as _ast
        tree = _ast.parse(code)
        loop_count = sum(
            1 for n in _ast.walk(tree)
            if isinstance(n, (_ast.For, _ast.While))
        )
        if loop_count >= 2:
            has_nested_loop = True
    except Exception:
        pass

    if has_nested_loop:
        parts.append(
            "Contains multiple loops — verify time complexity is acceptable "
            "for the expected input size."
        )

    # No type hints note
    try:
        import ast as _ast
        tree = _ast.parse(code)
        func = None
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                func = node
                break
        if func and func.args.args and not any(
            a.annotation for a in func.args.args if a.arg not in ("self", "cls")
        ):
            parts.append(
                f"Consider adding type hints to improve IDE support: "
                f"def {func_name}({', '.join(params[:2])}: <type>...)."
            )
    except Exception:
        pass

    # Last doc sentence as a practical note (if it's good)
    if len(sentences) >= 3:
        last = sentences[-1].strip()
        if (
            len(last.split()) >= 6
            and not _is_generic(last)
            and last not in (sentences[0], sentences[1] if len(sentences) > 1 else "")
        ):
            if last[-1] not in ".!?":
                last += "."
            parts.append(last)

    if parts:
        return " ".join(parts[:2])

    # Dynamic fallback using function name
    if func_name:
        readable = func_name.replace("_", " ")
        return (
            f"Ensure '{readable}' is called with validated inputs. "
            f"Add unit tests covering normal, boundary, and failure cases."
        )

    return (
        "Add unit tests covering normal operation, boundary conditions, "
        "and expected failure modes."
    )


def build_passport_target(doc: str, code: str = "") -> str:
    """
    Build a structured passport target for a (doc, code) pair.

    Design principles:
    - Clean the docstring first (strip Args/Returns/Raises blocks)
    - DOCSTRING, PURPOSE, BEHAVIOR SUMMARY must be distinct sentences
    - INPUTS/OUTPUTS derived from AST, not docstring
    - ASSUMPTIONS and EDGE CASES derived from code structure + doc keywords
    - DEVELOPER NOTE uses code analysis for practical advice
    - Zero hardcoded generic placeholders
    """
    # ── Step 1: Clean the docstring ──────────────────────────
    clean_doc = clean_docstring(doc)

    # ── Step 2: Extract code facts ───────────────────────────
    func_name, params = _extract_func_name_and_params(code) if code else ("", [])
    return_values     = _get_return_values(code) if code else []
    has_for, has_while = _has_loops(code) if code else (False, False)
    is_recursive      = _has_recursion(code, func_name) if code and func_name else False

    # ── Step 3: Split clean doc into distinct sentences ──────
    sentences = split_sentences(clean_doc)

    # ── Step 4: Build each section ───────────────────────────
    docstring = _make_docstring(sentences, clean_doc)
    purpose   = _make_purpose(sentences, func_name, params)
    behavior  = _make_behavior_summary(
        sentences, func_name, params, return_values,
        has_for, has_while, is_recursive
    )
    io_text   = _make_inputs_outputs(params, return_values, code) if code else (
        f"Input: {', '.join(params)}. Output: not specified."
        if params else "Input: none. Output: not specified."
    )
    assumptions = _make_assumptions(
        params, func_name, has_for, has_while, is_recursive, clean_doc
    )
    edge_cases  = _make_edge_cases(
        params, return_values, func_name,
        has_for, has_while, is_recursive, clean_doc
    )
    dev_note    = _make_developer_note(
        func_name, params, return_values,
        is_recursive, has_for, has_while, sentences, code
    )

    # ── Step 5: Deduplicate across sections ──────────────────
    # If PURPOSE ended up identical to DOCSTRING, regenerate it
    if purpose.lower().strip() == docstring.lower().strip():
        if func_name:
            readable = func_name.replace("_", " ")
            purpose = f"Designed to {readable} efficiently and correctly."
        else:
            purpose = behavior[:100] + "." if behavior else docstring

    # If BEHAVIOR is too similar to either, fall back
    def _too_similar(a: str, b: str) -> bool:
        a_words = set(a.lower().split())
        b_words = set(b.lower().split())
        if not a_words or not b_words:
            return False
        overlap = len(a_words & b_words) / len(a_words | b_words)
        return overlap > 0.75

    if _too_similar(behavior, docstring) or _too_similar(behavior, purpose):
        # Force a behavior statement from code facts
        fact_parts = []
        if is_recursive:
            fact_parts.append(f"Implements '{func_name}' using recursion.")
        if has_for:
            fact_parts.append("Processes elements sequentially using a for loop.")
        if has_while:
            fact_parts.append("Maintains a control loop using while.")
        if return_values:
            fact_parts.append(f"Produces {return_values[0]} as its primary output.")
        if not fact_parts:
            fact_parts.append(
                f"Executes the '{func_name.replace('_', ' ')}' "
                f"operation across {len(params)} parameter(s)."
                if func_name and params else
                "Performs a computation and returns the result."
            )
        behavior = " ".join(fact_parts)

    # ── Step 6: Assemble ─────────────────────────────────────
    # Only include sections that have real content
    sections = []
    if docstring:
        sections.append(f"DOCSTRING: {docstring}")
    if purpose:
        sections.append(f"PURPOSE: {purpose}")
    if behavior:
        sections.append(f"BEHAVIOR SUMMARY: {behavior}")
    if io_text:
        sections.append(f"INPUTS / OUTPUTS: {io_text}")
    if assumptions:
        sections.append(f"ASSUMPTIONS: {assumptions}")
    if edge_cases:
        sections.append(f"EDGE CASES: {edge_cases}")
    if dev_note:
        sections.append(f"DEVELOPER NOTE: {dev_note}")

    return "\n".join(sections)


# ──────────────────────────────────────────────
# JSONL I/O helpers
# ──────────────────────────────────────────────

def read_jsonl(path: str) -> list:
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def write_jsonl(records: list, path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"✅ Saved {len(records)} records → {path}")


def ensure_dirs(*dirs):
    for d in dirs:
        Path(d).mkdir(parents=True, exist_ok=True)