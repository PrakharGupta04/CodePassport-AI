"""
pdf_export.py — Export the full Developer Passport as a PDF.
Uses only the 'reportlab' library (pip install reportlab).
No external API calls needed.
"""

import io
from datetime import datetime
from typing import List, Optional

from src.static_analyzer import FunctionAnalysis, format_analysis_summary
from src.risk_engine import Risk
from src.health_score import HealthResult


def generate_pdf(code, passport_text, analysis, risks, health):
    import io
    from datetime import datetime
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm, mm
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table,
        TableStyle, HRFlowable, KeepTogether, PageBreak,
    )

    buffer = io.BytesIO()
    PAGE_W, PAGE_H = A4
    M = 1.6 * cm
    W = PAGE_W - 2 * M

    # ── Palette ───────────────────────────────────────────
    P = {
        "bg":      colors.HexColor("#0d1117"),
        "surf":    colors.HexColor("#161b22"),
        "surf2":   colors.HexColor("#1c2128"),
        "border":  colors.HexColor("#30363d"),
        "border2": colors.HexColor("#21262d"),
        "cyan":    colors.HexColor("#39d0f0"),
        "green":   colors.HexColor("#3fb950"),
        "red":     colors.HexColor("#f85149"),
        "amber":   colors.HexColor("#d29922"),
        "blue":    colors.HexColor("#58a6ff"),
        "purple":  colors.HexColor("#bc8cff"),
        "text":    colors.HexColor("#e6edf3"),
        "muted":   colors.HexColor("#8b949e"),
        "white":   colors.HexColor("#ffffff"),
        "risk_h":  colors.HexColor("#2d1515"),
        "risk_m":  colors.HexColor("#2d2215"),
        "risk_l":  colors.HexColor("#15222d"),
    }

    grade_color = colors.HexColor(health.color)

    # ── Style factory ─────────────────────────────────────
    def st(name, font="Helvetica", size=9, color=None, align=TA_LEFT,
           leading=None, bold=False, space_before=0, space_after=0):
        return ParagraphStyle(
            name,
            fontName="Helvetica-Bold" if bold else font,
            fontSize=size,
            textColor=color or P["text"],
            alignment=align,
            leading=leading or size * 1.45,
            spaceBefore=space_before,
            spaceAfter=space_after,
            wordWrap="LTR",
        )

    # Pre-built common styles
    Styles = {
        "title":     st("title",  size=22, color=P["white"],  bold=True,  align=TA_CENTER),
        "subtitle":  st("sub",    size=8,  color=P["cyan"],   align=TA_CENTER),
        "h2":        st("h2",     size=11, color=P["cyan"],   bold=True),
        "h3":        st("h3",     size=9,  color=P["muted"],  bold=True),
        "body":      st("body",   size=9,  color=P["text"],   leading=15),
        "body_m":    st("bodym",  size=9,  color=P["muted"],  leading=15),
        "mono":      st("mono",   font="Courier", size=8, color=P["text"], leading=12),
        "mono_ln":   st("monoln", font="Courier", size=7, color=P["muted"], align=TA_RIGHT),
        "label":     st("label",  size=7,  color=P["muted"],  bold=True),
        "sec_label": st("slabel", size=7,  color=P["cyan"],   bold=True),
        "chip_val":  st("cpv",    size=18, color=P["white"],  bold=True, align=TA_CENTER),
        "chip_sub":  st("cps",    size=7,  color=P["muted"],  align=TA_CENTER),
        "footer":    st("footer", size=7,  color=P["muted"],  align=TA_CENTER),
    }

    # ── Helpers ───────────────────────────────────────────

    def _p(text, style_key="body", **overrides):
        """Create a Paragraph with optional style overrides."""
        s = Styles[style_key]
        if overrides:
            s = ParagraphStyle(
                style_key + "_ov",
                parent=s,
                **{k: v for k, v in overrides.items()}
            )
        safe = (str(text)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))
        return Paragraph(safe, s)

    def _p_raw(text, style_key="body"):
        """Create a Paragraph without XML-escaping (for bold/color tags)."""
        return Paragraph(str(text), Styles[style_key])

    def _sp(h_mm=3):
        return Spacer(1, h_mm * mm)

    def _hr(color=None, thickness=0.5):
        return HRFlowable(
            width="100%", thickness=thickness,
            color=color or P["border"], spaceAfter=0, spaceBefore=0
        )

    def _tbl_style(rows=None, bg=None, header_bg=None, zebra=True,
                   border_color=None, pad=5):
        bc   = border_color or P["border2"]
        hbg  = header_bg    or P["surf"]
        bbg  = bg           or P["bg"]
        s = [
            ("BACKGROUND",    (0, 0), (-1, 0),  hbg),
            ("GRID",          (0, 0), (-1, -1), 0.4, bc),
            ("TOPPADDING",    (0, 0), (-1, -1), pad),
            ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
            ("LEFTPADDING",   (0, 0), (-1, -1), pad + 3),
            ("RIGHTPADDING",  (0, 0), (-1, -1), pad),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ]
        if zebra and rows:
            for i in range(1, rows):
                bg_c = P["bg"] if i % 2 == 1 else P["surf2"]
                s.append(("BACKGROUND", (0, i), (-1, i), bg_c))
        return TableStyle(s)

    def section_header(icon, title, accent=None):
        """Full-width colored section header bar."""
        ac = accent or P["cyan"]
        data = [[
            _p_raw(
                f'<font color="#{ac.hexval()[2:]}" size="10"><b>{icon} {title.upper()}</b></font>',
                "h2"
            )
        ]]
        t = Table(data, colWidths=[W])
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), P["surf"]),
            ("LINEBELOW",     (0, 0), (-1, -1), 2.5, ac),
            ("TOPPADDING",    (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING",   (0, 0), (-1, -1), 12),
        ]))
        return t

    def card(content_rows, accent=None, bg=None, pad=8):
        """
        Wrap any list of Paragraphs/Tables in a bordered card.
        content_rows: list of [flowable] rows
        """
        ac = accent or P["border"]
        bg_ = bg or P["surf"]
        inner = [[row] for row in content_rows]
        t = Table(inner, colWidths=[W - 6 * mm])
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), bg_),
            ("LINEBEFORE",    (0, 0), (0, -1),  3, ac),
            ("BOX",           (0, 0), (-1, -1), 0.5, P["border"]),
            ("TOPPADDING",    (0, 0), (-1, -1), pad),
            ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
            ("LEFTPADDING",   (0, 0), (-1, -1), pad + 4),
            ("RIGHTPADDING",  (0, 0), (-1, -1), pad),
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ]))
        return t

    # ── Parse passport sections ───────────────────────────
    SEC_META = [
        ("DOCSTRING",        "📄", P["blue"]),
        ("PURPOSE",          "🎯", P["red"]),
        ("BEHAVIOR SUMMARY", "🔄", P["purple"]),
        ("INPUTS / OUTPUTS", "📥", P["amber"]),
        ("ASSUMPTIONS",      "⚠️",  P["red"]),
        ("EDGE CASES",       "🔍", P["blue"]),
        ("DEVELOPER NOTE",   "💡", P["green"]),
    ]
    sec_names = [s[0] for s in SEC_META]
    parsed_passport = {}
    cur, buf = None, []
    for line in passport_text.split("\n"):
        s = line.strip()
        matched = False
        for nm in sec_names:
            if s.upper().startswith(nm):
                if cur:
                    parsed_passport[cur] = " ".join(buf).strip()
                cur = nm
                rest = s[len(nm):].lstrip(":").strip()
                buf = [rest] if rest else []
                matched = True
                break
        if not matched and s:
            buf.append(s)
    if cur:
        parsed_passport[cur] = " ".join(buf).strip()

    from src.static_analyzer import format_analysis_summary
    sa_summary = format_analysis_summary(analysis)
    func_name  = analysis.name or "function"
    now_str    = datetime.now().strftime("%Y-%m-%d  %H:%M")

    RISK_ACCENT = {"HIGH": P["red"], "MEDIUM": P["amber"], "LOW": P["blue"]}
    RISK_BG     = {"HIGH": P["risk_h"], "MEDIUM": P["risk_m"], "LOW": P["risk_l"]}

    # ── Document ──────────────────────────────────────────
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=M, leftMargin=M,
        topMargin=M, bottomMargin=M + 4 * mm,
        title=f"Developer Passport — {func_name}",
        author="CodePassport AI",
    )

    story = []

    # ════════════════════════════════════════════════════
    # PAGE 1 — COVER
    # ════════════════════════════════════════════════════

    # Full-width cover block
    cover_inner = [
        [_p_raw('<font size="28">🛂</font>', "body")],
        [_sp(2)],
        [_p_raw(
            '<font color="#ffffff" size="22"><b>DEVELOPER PASSPORT</b></font>',
            "title"
        )],
        [_p_raw(
            '<font color="#39d0f0" size="8">CODE PASSPORT AI  ·  '
            'GENERATIVE INTELLIGENCE PLATFORM</font>',
            "subtitle"
        )],
        [_sp(4)],
        [_hr(color=P["cyan"], thickness=1.5)],
        [_sp(3)],
        [_p(f"Function:  {func_name}", "body",
            textColor=P["white"], fontSize=11,
            fontName="Helvetica-Bold", alignment=TA_CENTER)],
        [_p(f"Generated: {now_str}", "body_m", alignment=TA_CENTER)],
        [_sp(3)],
        [_hr(color=P["border"], thickness=0.5)],
    ]
    cover_tbl = Table(cover_inner, colWidths=[W])
    cover_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), P["bg"]),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
    ]))
    story.append(cover_tbl)
    story.append(_sp(5))

    # ── Health Score Banner ────────────────────────────────
    high_c   = sum(1 for r in risks if r.level == "HIGH")
    med_c    = sum(1 for r in risks if r.level == "MEDIUM")
    low_c    = sum(1 for r in risks if r.level == "LOW")
    risk_col = P["red"] if high_c else P["amber"] if med_c else P["green"]

    def chip(value, label, color):
        inner = Table([
            [_p(str(value), "chip_val", textColor=color)],
            [_p(label.upper(), "chip_sub")],
        ], colWidths=[W / 3 - 6 * mm])
        inner.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), P["surf"]),
            ("TOPPADDING",    (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
        ]))
        return inner

    risk_label = f"{len(risks)} Risks  ({high_c}H {med_c}M {low_c}L)"
    banner = Table(
        [[
            chip(f"{health.total}/100", "Health Score", grade_color),
            chip(f"Grade {health.grade}", "Overall Grade", grade_color),
            chip(str(len(risks)), risk_label, risk_col),
        ]],
        colWidths=[W / 3, W / 3, W / 3],
    )
    banner.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), P["surf"]),
        ("BOX",           (0, 0), (-1, -1), 0.5, P["border"]),
        ("LINEBEFORE",    (1, 0), (1, 0),   0.5, P["border"]),
        ("LINEBEFORE",    (2, 0), (2, 0),   0.5, P["border"]),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
    ]))
    story.append(KeepTogether([banner]))
    story.append(_sp(5))

    # ── Health Score Breakdown ────────────────────────────
    story.append(KeepTogether([
        section_header("💯", "Health Score Breakdown", P["green"]),
        _sp(3),
    ]))

    hb_rows = [[
        _p("Category",   "sec_label"),
        _p("Score",      "sec_label"),
        _p("/ Max",      "sec_label"),
        _p("% Achieved", "sec_label"),
    ]]
    for cat, (earned, max_pts) in health.breakdown.items():
        pct = int(earned / max_pts * 100) if max_pts else 0
        col = P["green"] if pct >= 70 else P["amber"] if pct >= 40 else P["red"]
        hb_rows.append([
            _p(cat, "body"),
            _p(str(earned), "body", textColor=col, fontName="Helvetica-Bold"),
            _p(f"/{max_pts}", "body_m"),
            _p(f"{pct}%",    "body", textColor=col),
        ])
    # Total row
    hb_rows.append([
        _p("TOTAL", "body", fontName="Helvetica-Bold", textColor=P["white"]),
        _p(str(health.total), "body",
           fontName="Helvetica-Bold", textColor=grade_color, fontSize=10),
        _p("/100", "body_m"),
        _p(f"Grade  {health.grade}", "body",
           fontName="Helvetica-Bold", textColor=grade_color),
    ])

    hb_tbl = Table(
        hb_rows,
        colWidths=[W * 0.40, W * 0.15, W * 0.15, W * 0.30],
    )
    hb_style = _tbl_style(rows=len(hb_rows), zebra=True)
    hb_style.add("BACKGROUND",  (0, len(hb_rows) - 1), (-1, -1), P["surf"])
    hb_style.add("LINEABOVE",   (0, len(hb_rows) - 1), (-1, -1), 1.2, P["cyan"])
    hb_style.add("FONTNAME",    (0, len(hb_rows) - 1), (-1, -1), "Helvetica-Bold")
    hb_tbl.setStyle(hb_style)
    story.append(hb_tbl)
    story.append(_sp(5))

    # ════════════════════════════════════════════════════
    # PAGE 2 — AI-GENERATED PASSPORT
    # ════════════════════════════════════════════════════
    story.append(PageBreak())
    story.append(KeepTogether([
        section_header("🛂", "AI-Generated Developer Passport", P["cyan"]),
        _sp(3),
    ]))

    for sec_name, emoji, accent in SEC_META:
        content = parsed_passport.get(sec_name, "")
        if not content:
            continue

        sec_block = [
            _p_raw(
                f'<font color="#{accent.hexval()[2:]}" size="7">'
                f'<b>{emoji}  {sec_name}</b></font>',
                "label"
            ),
            _sp(1),
            _p(content, "body"),
        ]
        story.append(KeepTogether([
            card(sec_block, accent=accent, bg=P["bg"]),
            _sp(2),
        ]))

    story.append(_sp(3))

    # ════════════════════════════════════════════════════
    # PAGE 3 — STATIC ANALYSIS
    # ════════════════════════════════════════════════════
    story.append(PageBreak())
    story.append(KeepTogether([
        section_header("🔬", "Static Analysis", P["blue"]),
        _sp(3),
    ]))

    if "error" not in sa_summary:
        items   = list(sa_summary.items())
        mid     = (len(items) + 1) // 2
        col1    = items[:mid]
        col2    = items[mid:]
        max_r   = max(len(col1), len(col2))

        def sa_cell_pair(k, v):
            warn = "⚠️" in str(v)
            vc   = P["amber"] if warn else P["text"]
            return [
                _p(str(k), "label"),
                _p(str(v), "body", textColor=vc),
            ]

        sa_header = [
            _p("Property", "sec_label"),
            _p("Value",    "sec_label"),
            _p("Property", "sec_label"),
            _p("Value",    "sec_label"),
        ]
        sa_rows = [sa_header]
        empty   = [_p("", "body"), _p("", "body")]
        for i in range(max_r):
            left  = sa_cell_pair(*col1[i]) if i < len(col1) else empty
            right = sa_cell_pair(*col2[i]) if i < len(col2) else empty
            sa_rows.append(left + right)

        cw = W / 4
        sa_tbl = Table(
            sa_rows,
            colWidths=[cw * 0.9, cw * 1.1, cw * 0.9, cw * 1.1],
        )
        sa_style = _tbl_style(rows=len(sa_rows), zebra=True)
        sa_style.add("LINEAFTER", (1, 0), (1, -1), 0.8, P["border"])
        sa_tbl.setStyle(sa_style)
        story.append(sa_tbl)
    else:
        story.append(card(
            [_p(f"Analysis error: {sa_summary['error']}", "body",
                textColor=P["red"])],
            accent=P["red"]
        ))

    story.append(_sp(5))

    # ════════════════════════════════════════════════════
    # RISK INTELLIGENCE
    # ════════════════════════════════════════════════════
    story.append(KeepTogether([
        section_header("⚠️", "Risk Intelligence", P["red"]),
        _sp(3),
    ]))

    if not risks:
        story.append(card(
            [_p("✅  No risks detected for this function.", "body",
                textColor=P["green"])],
            accent=P["green"], bg=P["surf"]
        ))
    else:
        for risk in risks:
            ac  = RISK_ACCENT.get(risk.level, P["muted"])
            bg_ = RISK_BG.get(risk.level,   P["surf"])
            badge_color = ac.hexval()[2:]
            block = [
                _p_raw(
                    f'<font color="#{badge_color}" size="7">'
                    f'<b>[{risk.level}]  {risk.category}</b></font>',
                    "label"
                ),
                _sp(1),
                _p(risk.description, "body", textColor=P["text"]),
            ]
            story.append(KeepTogether([
                card(block, accent=ac, bg=bg_),
                _sp(2),
            ]))

    story.append(_sp(5))

    # ════════════════════════════════════════════════════
    # IMPROVEMENT SUGGESTIONS
    # ════════════════════════════════════════════════════
    if health.suggestions:
        story.append(KeepTogether([
            section_header("💡", "Improvement Suggestions", P["amber"]),
            _sp(3),
        ]))

        sug_rows = []
        for i, sug in enumerate(health.suggestions, 1):
            sug_rows.append([
                _p(str(i), "body",
                   fontName="Helvetica-Bold",
                   textColor=P["amber"], alignment=TA_CENTER),
                _p(sug, "body"),
            ])

        sug_tbl = Table(sug_rows, colWidths=[8 * mm, W - 8 * mm])
        sug_style = _tbl_style(rows=len(sug_rows), zebra=True)
        sug_style.add("LINEBEFORE", (1, 0), (1, -1), 2.5, P["amber"])
        sug_tbl.setStyle(sug_style)
        story.append(sug_tbl)
        story.append(_sp(5))

    # ════════════════════════════════════════════════════
    # PAGE 4 — SOURCE CODE
    # ════════════════════════════════════════════════════
    story.append(PageBreak())
    story.append(KeepTogether([
        section_header("📄", "Source Code", P["muted"]),
        _sp(3),
    ]))

    code_lines = code.strip().splitlines()
    if len(code_lines) > 60:
        code_lines = code_lines[:60] + ["# ... (truncated at 60 lines)"]

    # Syntax-like coloring (basic keyword detection, no external lib needed)
    KEYWORDS = {
        "def", "return", "if", "elif", "else", "for", "while", "in",
        "not", "and", "or", "import", "from", "class", "try", "except",
        "finally", "with", "as", "pass", "break", "continue", "yield",
        "lambda", "raise", "True", "False", "None",
    }
    KW_COLOR   = "58a6ff"   # blue
    STR_COLOR  = "a5d6ff"   # light blue
    CMT_COLOR  = "8b949e"   # muted gray
    NUM_COLOR  = "79c0ff"   # lighter blue

    def _colorize_line(raw_line: str) -> str:
        """
        Minimal syntax coloring without external libraries.
        Handles: comments, string literals, keywords, numbers.
        Returns a ReportLab XML-safe string with font color tags.
        """
        import re

        # Escape XML first
        line = (raw_line
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))

        # Full-line comment
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            safe   = raw_line[indent:].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return (
                "\u00a0" * indent +
                f'<font color="#{CMT_COLOR}">{safe}</font>'
            )

        # Inline comment detection
        comment_part = ""
        if " #" in raw_line:
            idx          = raw_line.index(" #")
            comment_part = raw_line[idx:]
            raw_line     = raw_line[:idx]
            comment_part = (comment_part
                            .replace("&", "&amp;")
                            .replace("<", "&lt;")
                            .replace(">", "&gt;"))
            comment_part = f'<font color="#{CMT_COLOR}">{comment_part}</font>'

        # Rebuild with indentation preserved
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        nbsp   = "\u00a0" * indent
        rest   = raw_line.strip()

        # String literal coloring (single and double quoted)
        def color_strings(s: str) -> str:
            return re.sub(
                r'(\"\"\".*?\"\"\"|\'\'\'.*?\'\'\'|\"[^\"]*\"|\'[^\']*\')',
                lambda m: f'<font color="#{STR_COLOR}">{m.group(0)}</font>',
                s
            )

        # Number coloring
        def color_numbers(s: str) -> str:
            return re.sub(
                r'\b(\d+\.?\d*)\b',
                lambda m: f'<font color="#{NUM_COLOR}">{m.group(0)}</font>',
                s
            )

        # Keyword coloring
        def color_keywords(s: str) -> str:
            pattern = r'\b(' + '|'.join(re.escape(k) for k in KEYWORDS) + r')\b'
            return re.sub(
                pattern,
                lambda m: f'<font color="#{KW_COLOR}"><b>{m.group(0)}</b></font>',
                s
            )

        rest_escaped = (rest
                        .replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;"))
        rest_colored = color_strings(rest_escaped)
        rest_colored = color_numbers(rest_colored)
        rest_colored = color_keywords(rest_colored)

        return nbsp + rest_colored + comment_part

    code_rows = []
    for i, raw_line in enumerate(code_lines, 1):
        colored = _colorize_line(raw_line)
        code_rows.append([
            Paragraph(str(i),    Styles["mono_ln"]),
            Paragraph(colored,   Styles["mono"]),
        ])

    code_tbl = Table(code_rows, colWidths=[8 * mm, W - 8 * mm])
    code_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), P["bg"]),
        ("BACKGROUND",    (0, 0), (0, -1),  P["surf2"]),
        ("BOX",           (0, 0), (-1, -1), 0.5, P["border"]),
        ("LINEAFTER",     (0, 0), (0, -1),  0.5, P["border"]),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING",   (0, 0), (0, -1),  4),
        ("LEFTPADDING",   (1, 0), (1, -1),  6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(code_tbl)
    story.append(_sp(6))

    # ════════════════════════════════════════════════════
    # FOOTER on last page
    # ════════════════════════════════════════════════════
    footer_data = [[
        _p("CodePassport AI", "footer", alignment=TA_LEFT),
        _p("LoRA Fine-tuned CodeT5  ·  AST Analysis Engine",
           "footer", alignment=TA_CENTER),
        _p(now_str, "footer", alignment=TA_RIGHT),
    ]]
    footer_tbl = Table(footer_data, colWidths=[W / 3, W / 3, W / 3])
    footer_tbl.setStyle(TableStyle([
        ("LINEABOVE",     (0, 0), (-1, -1), 0.5, P["border"]),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
    ]))
    story.append(footer_tbl)

    # ── Build PDF ─────────────────────────────────────────

    def on_page(canvas, doc_obj):
        """Draw page number and background on every page."""
        canvas.saveState()
        # Full-page dark background
        canvas.setFillColor(P["bg"])
        canvas.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
        # Page number
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(P["muted"])
        canvas.drawCentredString(
            PAGE_W / 2, 10 * mm,
            f"Page {doc_obj.page}  ·  CodePassport AI  ·  {func_name}"
        )
        canvas.restoreState()

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    buffer.seek(0)
    return buffer.getvalue()