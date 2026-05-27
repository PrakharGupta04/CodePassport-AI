"""
static_analyzer.py — AST-based static analysis of Python functions.
v2: Fixed scope leakage, false division detection, try/except scoping.
"""

import ast
import textwrap
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class FunctionAnalysis:
    name: str = ""
    parameters: List[str] = field(default_factory=list)
    return_annotation: str = ""
    param_annotations: dict = field(default_factory=dict)
    has_docstring: bool = False
    docstring_text: str = ""
    num_loops: int = 0
    loop_types: List[str] = field(default_factory=list)
    num_conditionals: int = 0
    num_return_stmts: int = 0
    return_values: List[str] = field(default_factory=list)
    has_try_except: bool = False
    exception_types: List[str] = field(default_factory=list)
    has_recursion: bool = False
    calls_made: List[str] = field(default_factory=list)
    uses_comprehension: bool = False
    num_lines: int = 0
    complexity_score: int = 1
    is_generator: bool = False
    has_default_args: bool = False
    default_args: dict = field(default_factory=dict)
    raises_exceptions: List[str] = field(default_factory=list)
    nested_functions: List[str] = field(default_factory=list)
    uses_global: bool = False
    uses_lambda: bool = False
    error: str = ""


def _walk_body_only(node):
    """
    Walk AST nodes but do NOT descend into nested function definitions
    or class definitions. This prevents scope leakage where inner
    functions' properties get attributed to the outer function.
    """
    for child in ast.iter_child_nodes(node):
        # Stop at nested function/class boundaries
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield child   # yield the node itself but do not recurse into it
            continue
        yield child
        yield from _walk_body_only(child)


def analyze_function(code: str) -> FunctionAnalysis:
    """
    Parse and analyze a Python function string using AST.
    Scope-safe: does not attribute nested function properties to outer function.
    """
    result = FunctionAnalysis()

    code = textwrap.dedent(code).strip()
    result.num_lines = len([l for l in code.splitlines() if l.strip()])

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        result.error = f"Syntax error: {e}"
        return result

    # Find the outermost function definition only
    func_def = None
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_def = node
            break

    # If not at top level, do a full walk but take the first one
    if func_def is None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_def = node
                break

    if func_def is None:
        result.error = "No function definition found"
        return result

    # ── Name ──────────────────────────────────────────────
    result.name = func_def.name

    # ── Parameters ────────────────────────────────────────
    args = func_def.args
    for arg in args.args + args.posonlyargs + args.kwonlyargs:
        if arg.arg == "self" or arg.arg == "cls":
            continue
        result.parameters.append(arg.arg)
        if arg.annotation:
            try:
                result.param_annotations[arg.arg] = ast.unparse(arg.annotation)
            except Exception:
                pass

    # vararg (*args) and kwarg (**kwargs)
    if args.vararg:
        result.parameters.append(f"*{args.vararg.arg}")
    if args.kwarg:
        result.parameters.append(f"**{args.kwarg.arg}")

    # Default values
    all_args = args.args + args.posonlyargs
    defaults = args.defaults
    if defaults:
        result.has_default_args = True
        defaulted_args = all_args[-len(defaults):]
        for a, d in zip(defaulted_args, defaults):
            try:
                result.default_args[a.arg] = ast.unparse(d)
            except Exception:
                pass

    # Return annotation
    if func_def.returns:
        try:
            result.return_annotation = ast.unparse(func_def.returns)
        except Exception:
            pass

    # ── Docstring ─────────────────────────────────────────
    docstring = ast.get_docstring(func_def)
    if docstring:
        result.has_docstring = True
        result.docstring_text = docstring

    # ── Walk ONLY the direct body (scope-safe) ────────────
    # We collect nested function names separately so we don't
    # count their internals as belonging to the outer function.
    nested_func_nodes = set()
    for node in ast.iter_child_nodes(func_def):
        for child in ast.walk(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child is not func_def:
                nested_func_nodes.add(id(child))
                result.nested_functions.append(child.name)

    def in_nested(node):
        """Check if a node is inside a nested function."""
        return id(node) in nested_func_nodes

    # Use _walk_body_only to stay in scope
    for node in _walk_body_only(func_def):
        # Skip if this is a nested function node (already catalogued)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not func_def:
            continue

        # Loops
        if isinstance(node, ast.For):
            result.num_loops += 1
            result.complexity_score += 1
            if "for" not in result.loop_types:
                result.loop_types.append("for")

        elif isinstance(node, ast.While):
            result.num_loops += 1
            result.complexity_score += 1
            if "while" not in result.loop_types:
                result.loop_types.append("while")

        # Conditionals
        elif isinstance(node, ast.If):
            result.num_conditionals += 1
            result.complexity_score += 1

        # Returns
        elif isinstance(node, ast.Return):
            result.num_return_stmts += 1
            if node.value is not None:
                try:
                    result.return_values.append(ast.unparse(node.value))
                except Exception:
                    pass

        # Try/Except — ONLY count if it's directly in the outer function body
        elif isinstance(node, ast.Try):
            result.has_try_except = True
            for handler in node.handlers:
                if handler.type:
                    try:
                        result.exception_types.append(ast.unparse(handler.type))
                    except Exception:
                        pass

        # Raise
        elif isinstance(node, ast.Raise):
            if node.exc:
                try:
                    result.raises_exceptions.append(ast.unparse(node.exc))
                except Exception:
                    pass

        # Function calls
        elif isinstance(node, ast.Call):
            try:
                call_name = ast.unparse(node.func)
                if call_name == result.name:
                    result.has_recursion = True
                result.calls_made.append(call_name)
            except Exception:
                pass

        # Yield → generator
        elif isinstance(node, (ast.Yield, ast.YieldFrom)):
            result.is_generator = True

        # Comprehensions
        elif isinstance(node, (ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp)):
            result.uses_comprehension = True

        # Global statement
        elif isinstance(node, ast.Global):
            result.uses_global = True

        # Lambda
        elif isinstance(node, ast.Lambda):
            result.uses_lambda = True

    # Deduplicate calls
    seen, clean = set(), []
    for c in result.calls_made:
        if c not in seen and len(c) < 60:
            seen.add(c)
            clean.append(c)
    result.calls_made = clean

    return result


def format_analysis_summary(analysis: FunctionAnalysis) -> dict:
    """
    Convert FunctionAnalysis into display-ready dict.
    Never shows raw False/True — always human-readable strings.
    """
    if analysis.error:
        return {"error": analysis.error}

    param_parts = []
    for p in analysis.parameters:
        ann     = analysis.param_annotations.get(p, "")
        default = analysis.default_args.get(p, "")
        part    = p
        if ann:
            part += f": {ann}"
        if default:
            part += f" = {default}"
        param_parts.append(part)

    loops_str = (
        f"{analysis.num_loops} ({', '.join(analysis.loop_types)})"
        if analysis.num_loops else "0"
    )

    try_str = "Yes" if analysis.has_try_except else "No"
    if analysis.has_try_except and analysis.exception_types:
        try_str += f" — catches: {', '.join(analysis.exception_types)}"

    nested_str = (
        ", ".join(analysis.nested_functions)
        if analysis.nested_functions else "none"
    )

    calls_display = analysis.calls_made[:6]

    return {
        "Function Name":      analysis.name,
        "Parameters":         ", ".join(param_parts) if param_parts else "none",
        "Return Type":        analysis.return_annotation or "not annotated",
        "Lines of Code":      str(analysis.num_lines),
        "Loops":              loops_str,
        "Conditionals":       str(analysis.num_conditionals),
        "Return Statements":  str(analysis.num_return_stmts),
        "Return Values":      ", ".join(analysis.return_values[:4]) or "none",
        "Has Try/Except":     try_str,
        "Raises":             ", ".join(analysis.raises_exceptions) or "none",
        "Recursion":          "Yes ⚠️" if analysis.has_recursion else "No",
        "Is Generator":       "Yes" if analysis.is_generator else "No",
        "Comprehension Used": "Yes" if analysis.uses_comprehension else "No",
        "Has Docstring":      "Yes" if analysis.has_docstring else "No ⚠️",
        "Has Default Args":   "Yes — " + ", ".join(f"{k}={v}" for k,v in analysis.default_args.items()) if analysis.has_default_args else "No",
        "Nested Functions":   nested_str,
        "Cyclomatic Complexity": str(analysis.complexity_score),
        "External Calls":     ", ".join(calls_display) if calls_display else "none",
        "Uses Global":        "Yes ⚠️" if analysis.uses_global else "No",
    }