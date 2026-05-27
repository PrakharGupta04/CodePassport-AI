"""
health_score.py — Code Health Score calculator (0–100).
Combines static analysis and risk data into a meaningful score.
"""

from dataclasses import dataclass
from typing import List
from src.static_analyzer import FunctionAnalysis
from src.risk_engine import Risk


@dataclass
class HealthResult:
    total: int                  # 0–100
    grade: str                  # A/B/C/D/F
    breakdown: dict             # category → points earned/max
    suggestions: List[str]      # actionable improvement list
    color: str                  # hex color for UI


def calculate_health(analysis: FunctionAnalysis, risks: List[Risk]) -> HealthResult:
    """
    Score the function across 6 dimensions, each worth different points.
    """
    breakdown = {}
    suggestions = []
    total = 0

    if analysis.error:
        return HealthResult(0, "F", {}, ["Fix syntax errors first"], "#ff4444")

    # ── 1. Documentation (25 pts) ─────────────────────────
    doc_score = 0
    if analysis.has_docstring:
        doc_score += 15
        if len(analysis.docstring_text) > 80:
            doc_score += 10   # detailed docstring
        else:
            suggestions.append("Expand your docstring with parameter descriptions and return info.")
    else:
        suggestions.append("Add a docstring — even one line dramatically improves maintainability.")
    breakdown["Documentation"] = (doc_score, 25)
    total += doc_score

    # ── 2. Type Safety (20 pts) ────────────────────────────
    type_score = 0
    if analysis.param_annotations:
        covered = len(analysis.param_annotations) / max(len(analysis.parameters), 1)
        type_score += int(covered * 12)
    if analysis.return_annotation:
        type_score += 8
    else:
        suggestions.append("Add a return type annotation (e.g., -> int) for better IDE support.")
    if not analysis.param_annotations and analysis.parameters:
        suggestions.append(
            f"Add type hints to parameters: "
            f"{', '.join(analysis.parameters[:3])}."
        )
    breakdown["Type Safety"] = (type_score, 20)
    total += type_score

    # ── 3. Error Handling (20 pts) ─────────────────────────
    err_score = 0
    high_risks = [r for r in risks if r.level == "HIGH"]

    if analysis.has_try_except:
        err_score += 12
        # Penalize bare except
        has_bare = any("Bare except" in r.category for r in risks)
        if has_bare:
            err_score -= 4
            suggestions.append("Replace bare 'except:' with 'except Exception:' to avoid masking system errors.")
    elif analysis.parameters:
        suggestions.append("Consider adding try/except for operations that could fail on bad input.")

    if not high_risks:
        err_score += 8
    else:
        suggestions.append(f"Fix {len(high_risks)} HIGH risk issue(s): " +
                           "; ".join(r.category for r in high_risks[:2]) + ".")
    breakdown["Error Handling"] = (err_score, 20)
    total += err_score

    # ── 4. Readability (15 pts) ────────────────────────────
    read_score = 0
    # Reasonable length
    if 5 <= analysis.num_lines <= 50:
        read_score += 6
    elif analysis.num_lines > 50:
        suggestions.append(
            f"Function is {analysis.num_lines} lines — consider breaking it into smaller helpers."
        )

    # Low complexity
    if analysis.complexity_score <= 5:
        read_score += 6
    elif analysis.complexity_score <= 10:
        read_score += 3
        suggestions.append(
            f"Cyclomatic complexity is {analysis.complexity_score} — simplify branching logic."
        )
    else:
        suggestions.append(
            f"High complexity ({analysis.complexity_score}) — reduce nested conditions/loops."
        )

    # Name not too short
    if len(analysis.name) >= 3:
        read_score += 3
    else:
        suggestions.append(f"Function name '{analysis.name}' is too short — use a descriptive name.")
    breakdown["Readability"] = (read_score, 15)
    total += read_score

    # ── 5. Safety (10 pts) ────────────────────────────────
    safe_score = 10
    for r in risks:
        if "eval" in r.category.lower() or "exec" in r.category.lower():
            safe_score -= 8
        if "Mutable Default" in r.category:
            safe_score -= 4
        if "Global" in r.category:
            safe_score -= 3
    safe_score = max(safe_score, 0)
    breakdown["Safety"] = (safe_score, 10)
    total += safe_score

    # ── 6. Completeness (10 pts) ──────────────────────────
    complete_score = 0
    if analysis.num_return_stmts > 0:
        complete_score += 5
    if not analysis.uses_global:
        complete_score += 3
    if not analysis.nested_functions or len(analysis.nested_functions) <= 2:
        complete_score += 2
    breakdown["Completeness"] = (complete_score, 10)
    total += complete_score

    total = min(max(total, 0), 100)

    # ── Grade ─────────────────────────────────────────────
    if total >= 85:
        grade, color = "A", "#3fb950"
    elif total >= 70:
        grade, color = "B", "#58a6ff"
    elif total >= 55:
        grade, color = "C", "#ffa657"
    elif total >= 40:
        grade, color = "D", "#f0883e"
    else:
        grade, color = "F", "#ff4444"

    # Keep top 5 most important suggestions
    suggestions = suggestions[:5]

    return HealthResult(
        total=total,
        grade=grade,
        breakdown=breakdown,
        suggestions=suggestions,
        color=color,
    )