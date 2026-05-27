"""
risk_engine.py — Rule-based risk detection for Python functions.
v2: Fixed false positives on floor division, return type consistency,
    and input validation detection.
"""

import ast
import textwrap
from dataclasses import dataclass, field
from typing import List
from src.static_analyzer import FunctionAnalysis


@dataclass
class Risk:
    level: str
    category: str
    description: str


def _get_func_def(code: str):
    """Parse code and return the outermost FunctionDef node."""
    try:
        tree = ast.parse(textwrap.dedent(code).strip())
    except SyntaxError:
        return None
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _is_user_controlled_var(node: ast.AST) -> bool:
    """
    Return True if a node looks like a variable that came from user input
    (i.e., it is a Name node referencing a parameter-like variable,
    not a literal constant we can reason about statically).
    """
    return isinstance(node, (ast.Name, ast.Subscript, ast.Attribute, ast.Call))


def _literal_value(node: ast.AST):
    """
    Return the Python value if node is a literal constant, else None.
    Handles both ast.Constant (Python 3.8+) and ast.Num (older).
    """
    if isinstance(node, ast.Constant):
        return node.value
    return None


def detect_risks(code: str, analysis: FunctionAnalysis) -> List[Risk]:
    """
    Run all risk detectors. Returns risks sorted HIGH → MEDIUM → LOW.
    """
    risks: List[Risk] = []

    if analysis.error:
        return [Risk("HIGH", "Parse Error",
                     f"Could not analyze function: {analysis.error}")]

    func_def = _get_func_def(code)
    if func_def is None:
        return risks

    # Keep a set of detected categories to avoid duplicate risks
    seen_categories = set()

    def add_risk(level, category, description):
        if category not in seen_categories:
            seen_categories.add(category)
            risks.append(Risk(level, category, description))

    # ── 1. Division by zero ────────────────────────────────
    # Only flag when:
    #   a) The right operand is the literal integer 0 (always crashes), OR
    #   b) The right operand is a user-controlled variable AND there is no
    #      try/except and no guard conditional checking it
    for node in ast.walk(func_def):
        if not isinstance(node, ast.BinOp):
            continue
        if not isinstance(node.op, (ast.Div, ast.Mod)):
            # FloorDiv (//) by a non-zero literal is never a risk.
            # Only flag FloorDiv if the divisor is literally 0.
            if isinstance(node.op, ast.FloorDiv):
                val = _literal_value(node.right)
                if val == 0:
                    add_risk("HIGH", "Division by Zero",
                             "Floor division by literal 0 — always raises ZeroDivisionError.")
            continue

        right_val = _literal_value(node.right)

        if right_val == 0:
            # Literal zero denominator — definite crash
            add_risk("HIGH", "Division by Zero",
                     f"Division by literal 0 detected — will always raise ZeroDivisionError.")
        elif right_val is None and _is_user_controlled_var(node.right):
            # Variable denominator — only warn if no protection
            if not analysis.has_try_except and analysis.num_conditionals == 0:
                divisor_name = ast.unparse(node.right) if hasattr(ast, 'unparse') else "variable"
                add_risk("MEDIUM", "Unguarded Division",
                         f"Division by '{divisor_name}' with no zero-check or try/except — "
                         f"may raise ZeroDivisionError.")
        # Any other case (division by a non-zero literal, etc.) is safe — no flag

    # ── 2. eval / exec / compile usage ────────────────────
    for node in ast.walk(func_def):
        if isinstance(node, ast.Call):
            try:
                name = ast.unparse(node.func)
            except Exception:
                continue
            if name in ("eval", "exec", "compile", "__import__"):
                add_risk("HIGH", f"Dangerous {name}()",
                         f"Use of {name}() can execute arbitrary code. "
                         f"Major security risk if any argument comes from user input.")

    # ── 3. Infinite loop ───────────────────────────────────
    for node in ast.walk(func_def):
        if not isinstance(node, ast.While):
            continue
        cond_val = _literal_value(node.test)
        is_true_literal = (cond_val is True or cond_val == 1)
        if is_true_literal:
            has_break  = any(isinstance(n, ast.Break)  for n in ast.walk(node))
            has_return = any(isinstance(n, ast.Return) for n in ast.walk(node))
            if not has_break and not has_return:
                add_risk("HIGH", "Infinite Loop",
                         "while True: loop with no break or return — function never terminates.")
            else:
                add_risk("LOW", "While True with Break/Return",
                         "Unbounded loop exits via break/return — verify all exit paths are reachable.")

    # ── 4. Inconsistent return types ──────────────────────
    # Only flag when the function returns values of genuinely different
    # Python types — not when it returns int in multiple forms like 0, -1, mid.
    if len(analysis.return_values) >= 2:
        return_types = set()
        for rv in analysis.return_values:
            # Classify return value type
            try:
                # Try to evaluate as a literal
                parsed = ast.parse(rv, mode='eval')
                if isinstance(parsed.body, ast.Constant):
                    return_types.add(type(parsed.body.value).__name__)
                elif isinstance(parsed.body, (ast.List, ast.ListComp)):
                    return_types.add("list")
                elif isinstance(parsed.body, (ast.Dict, ast.DictComp)):
                    return_types.add("dict")
                elif isinstance(parsed.body, ast.Tuple):
                    return_types.add("tuple")
                elif isinstance(parsed.body, ast.Constant) and parsed.body.value is None:
                    return_types.add("None")
                else:
                    # Non-literal expression — classify as 'expression'
                    return_types.add("expression")
            except Exception:
                return_types.add("expression")

        # Only warn if there are truly different categories
        # e.g. returning None AND a list/dict/expression is inconsistent
        # but returning -1 AND mid (both int/expression) is not
        none_types    = {"NoneType", "None"}
        numeric_types = {"int", "float"}
        has_none   = bool(return_types & none_types)
        has_numeric = bool(return_types & numeric_types)
        has_collection = bool(return_types & {"list", "dict", "tuple"})
        has_expression = "expression" in return_types

        genuinely_mixed = (
            (has_none and (has_collection or (has_expression and not has_numeric)))
            or (has_collection and has_numeric)
        )

        if genuinely_mixed:
            add_risk("MEDIUM", "Inconsistent Return Type",
                     f"Returns values of different types: {', '.join(str(t) for t in return_types)} — "
                     f"callers must handle multiple return types carefully.")

    # ── 5. No input validation ─────────────────────────────
    # Only flag if there are parameters AND no conditionals checking them
    # AND no try/except AND the function actually does something with parameters.
    # Binary search has conditionals on arr[mid] and target — so this should NOT fire.
    if analysis.parameters and not analysis.has_try_except:
        # Check if any conditional or assertion references a parameter name
        param_names = set(p.lstrip("*") for p in analysis.parameters)
        validation_found = False

        for node in ast.walk(func_def):
            if isinstance(node, ast.If):
                # Check if the condition references any parameter
                condition_src = ""
                try:
                    condition_src = ast.unparse(node.test)
                except Exception:
                    pass
                if any(p in condition_src for p in param_names):
                    validation_found = True
                    break
            elif isinstance(node, ast.Assert):
                validation_found = True
                break
            elif isinstance(node, ast.Raise):
                validation_found = True
                break

        if not validation_found and len(analysis.parameters) >= 2:
            # Only warn for functions with multiple parameters and complex operations
            if analysis.num_loops > 0 or analysis.complexity_score > 3:
                add_risk("LOW", "No Explicit Input Validation",
                         f"Parameters ({', '.join(list(param_names)[:3])}) are not explicitly validated — "
                         f"consider adding type checks or assertions for robustness.")

    # ── 6. Recursion stack risk ────────────────────────────
    if analysis.has_recursion:
        add_risk("MEDIUM", "Recursion Stack Risk",
                 f"Recursive call to '{analysis.name}' detected. Python's default recursion "
                 f"limit is 1000 frames — very deep inputs will raise RecursionError. "
                 f"Consider iterative implementation or sys.setrecursionlimit().")

    # ── 7. Missing docstring ───────────────────────────────
    if not analysis.has_docstring:
        add_risk("LOW", "No Docstring",
                 "Function has no docstring. Adding even a one-line description dramatically "
                 "improves maintainability and IDE support.")

    # ── 8. No type hints ──────────────────────────────────
    real_params = [p for p in analysis.parameters if not p.startswith("*")]
    if real_params and not analysis.param_annotations:
        add_risk("LOW", "No Type Annotations",
                 f"Parameters ({', '.join(real_params[:3])}) have no type hints. "
                 f"Type annotations improve IDE autocompletion and catch bugs early.")

    # ── 9. Global state ────────────────────────────────────
    if analysis.uses_global:
        add_risk("MEDIUM", "Global State Mutation",
                 "Function reads or writes global variables — creates hidden dependencies "
                 "that make testing and debugging significantly harder.")

    # ── 10. Bare except ───────────────────────────────────
    if analysis.has_try_except:
        for node in ast.walk(func_def):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                add_risk("MEDIUM", "Bare except Clause",
                         "bare 'except:' catches every exception including KeyboardInterrupt "
                         "and SystemExit. Use 'except Exception:' at minimum.")
                break

    # ── 11. Mutable default argument ──────────────────────
    for default in func_def.args.defaults + func_def.args.kw_defaults:
        if default is None:
            continue
        if isinstance(default, (ast.List, ast.Dict, ast.Set)):
            add_risk("HIGH", "Mutable Default Argument",
                     "A mutable object (list/dict/set) is used as a default argument. "
                     "It is shared across ALL calls to this function. "
                     "Use None as the default and assign inside the function body.")
            break

    # ── 12. Shadowing built-ins ────────────────────────────
    BUILTINS = {
        "list", "dict", "set", "tuple", "str", "int", "float", "bool",
        "type", "id", "input", "print", "open", "sum", "min", "max",
        "len", "range", "zip", "map", "filter", "sorted", "reversed",
    }
    shadowed = [p for p in analysis.parameters if p.lstrip("*") in BUILTINS]
    if shadowed:
        add_risk("LOW", "Built-in Name Shadowed",
                 f"Parameter(s) '{', '.join(shadowed)}' shadow Python built-in names. "
                 f"Rename to avoid confusion and potential bugs.")

    # ── Sort by severity ───────────────────────────────────
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    risks.sort(key=lambda r: order.get(r.level, 9))
    return risks