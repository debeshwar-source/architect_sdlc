"""
The Requirement agent's output as a document: shown on screen, and downloadable
as a Word file (.docx) or a PDF -- whichever the user prefers.

Plain words: the Requirement agent produces structured data (title, scope,
acceptance criteria...). This module lays that out as a proper document. (The agent's
"business goal" is deliberately NOT part of the document; it still goes to the later
agents, just not onto the page.) It first
builds ONE description of the document (document_model), then draws it three ways
(Markdown for the screen, .docx, .pdf), so the three can never disagree.

Nothing here needs an account or a credential, and nothing is sent anywhere: the
files are made on this computer.

The Word and PDF libraries (python-docx, reportlab) are imported only when a file
is actually made, so the rest of the app still works if they are not installed;
`build()` then raises MissingLibrary with the command that fixes it.
"""

import os
import re
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

from tools import fonts
from tools.fonts import MissingLibrary      # (kept importable from here: `requirement_doc.MissingLibrary`)

# fmt -> (label shown to the user, file extension, MIME type)
FORMATS = {
    "docx": ("Word document (.docx)", "docx",
             "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "pdf": ("PDF", "pdf", "application/pdf"),
}

# The pip package each format needs.
_REQUIRES = {"docx": "python-docx", "pdf": "reportlab"}


# ------------------------------------------------------------
# One description of the document
# ------------------------------------------------------------

# Control characters Word/XML cannot contain (everything below space except tab / newline).
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(value: Any) -> str:
    # Text that is safe for every format: no control characters, trimmed.
    return _CONTROL.sub("", str(value if value is not None else "")).strip()


def document_model(requirement: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The requirement as a plain description of a document:

        {"title": ..., "meta": [(label, value), ...],
         "sections": [{"heading": ..., "kind": "text" | "numbered" | "bullets" | "questions", ...}]}

    `meta` may carry run_id, date, knowledge_base and build_mode; whatever is
    given is shown under the title."""
    meta = meta or {}
    req = requirement or {}

    info: List[Tuple[str, str]] = []
    info.append(("Date", _clean(meta.get("date")) or datetime.now(timezone.utc).strftime("%d %B %Y")))
    if meta.get("run_id"):
        info.append(("Run", _clean(meta["run_id"])))
    mode = {"from_scratch": "Built from scratch", "extend_existing": "Extends an existing system"}.get(req.get("build_mode"))
    if mode:
        info.append(("Type", mode))
    if meta.get("knowledge_base") is not None:
        info.append(("Knowledge base", _clean(meta["knowledge_base"]) or "none"))

    criteria = [_clean(c) for c in req.get("acceptance_criteria") or [] if _clean(c)]
    out_of_scope = [_clean(c) for c in req.get("out_of_scope") or [] if _clean(c)]

    sections: List[Dict[str, Any]] = []
    sections.append({"heading": "Scope", "kind": "text",
                     "text": _clean(req.get("scope_summary")) or "Not stated."})
    sections.append({"heading": "Acceptance criteria", "kind": "numbered",
                     "items": [(f"AC-{i}", c) for i, c in enumerate(criteria, start=1)] or [("", "None stated.")]})
    sections.append({"heading": "Out of scope", "kind": "bullets", "items": out_of_scope or ["None stated."]})

    return {"title": _clean(req.get("title")) or "Requirement", "meta": info, "sections": sections}


def suggested_filename(requirement: Dict[str, Any], fmt: str) -> str:
    # e.g. "requirement-garments-website.pdf"
    slug = re.sub(r"[^a-z0-9]+", "-", _clean((requirement or {}).get("title")).lower()).strip("-")[:50]
    return f"requirement-{slug or 'document'}.{FORMATS[fmt][1]}"


# ------------------------------------------------------------
# Markdown (what is shown in the app)
# ------------------------------------------------------------

def to_markdown(requirement: Dict[str, Any], meta: Optional[Dict[str, Any]] = None, compact: bool = False) -> str:
    m = document_model(requirement, meta)
    # `compact` (used on screen) steps every heading down so the page is not shouted at.
    top, sub = ("###", "####") if compact else ("#", "##")
    lines = [f"{top} {m['title']}", "", " · ".join(f"{k}: {v}" for k, v in m["meta"]), "", "---", ""]
    for s in m["sections"]:
        lines += [f"{sub} {s['heading']}", ""]
        if s["kind"] == "text":
            lines += [s["text"], ""]
        elif s["kind"] == "numbered":
            lines += [f"- **{label}** {text}" if label else f"- {text}" for label, text in s["items"]] + [""]
        elif s["kind"] == "bullets":
            lines += [f"- {item}" for item in s["items"]] + [""]
        else:
            if not s["questions"]:
                lines += ["None. The agent found no material ambiguity to flag.", ""]
            for q in s["questions"]:
                lines.append(f"**{q['question']}**")
                if q["why"]:
                    lines.append(f"*Why it matters:* {q['why']}")
                lines += [f"- {o}{' **(recommended)**' if rec else ''}" for o, rec in q["options"]] + [""]
    return "\n".join(lines).rstrip() + "\n"


# ------------------------------------------------------------
# Word (.docx)
# ------------------------------------------------------------

def to_docx(requirement: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> bytes:
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from docx.shared import Pt, RGBColor
    except ImportError as exc:
        raise MissingLibrary(f"Word export needs python-docx. Install it with: pip install {_REQUIRES['docx']}") from exc

    m = document_model(requirement, meta)
    doc = Document()
    doc.core_properties.title = m["title"]
    doc.core_properties.author = "Blueprint"
    ink, accent = RGBColor(0x1F, 0x29, 0x37), RGBColor(0x37, 0x41, 0x51)

    def style_font(style, size, bold=False, color=ink, before=0, after=6):
        style.font.name = "Calibri"
        style.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "Calibri")
        for attr in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):      # drop the theme font so Calibri sticks
            style.element.rPr.rFonts.attrib.pop(qn(f"w:{attr}"), None)
        style.font.size = Pt(size)
        style.font.bold = bold
        style.font.color.rgb = color
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)

    style_font(doc.styles["Normal"], 10.5)
    doc.styles["Normal"].paragraph_format.line_spacing = 1.2
    style_font(doc.styles["Title"], 20, bold=True, after=2)
    style_font(doc.styles["Heading 1"], 12, bold=True, color=accent, before=16, after=4)
    doc.styles["Heading 1"].font.all_caps = True       # shown in capitals; the text itself is unchanged
    style_font(doc.styles["List Bullet"], 10.5, after=2)
    for margin in ("left_margin", "right_margin"):
        setattr(doc.sections[0], margin, Pt(66))

    doc.add_heading(m["title"], level=0)
    info = doc.add_paragraph()
    run = info.add_run("  ·  ".join(f"{k}: {v}" for k, v in m["meta"]))
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(0x6B, 0x72, 0x80)

    for s in m["sections"]:
        heading = doc.add_heading(s["heading"], level=1)
        bottom = OxmlElement("w:pBdr")                      # a hairline under each heading
        line = OxmlElement("w:bottom")
        for key, value in (("val", "single"), ("sz", "4"), ("space", "2"), ("color", "D1D5DB")):
            line.set(qn(f"w:{key}"), value)
        bottom.append(line)
        heading._p.get_or_add_pPr().append(bottom)
        if s["kind"] == "text":
            doc.add_paragraph(s["text"])
        elif s["kind"] == "numbered":
            for label, text in s["items"]:
                p = doc.add_paragraph()
                p.paragraph_format.left_indent = Pt(36)
                p.paragraph_format.first_line_indent = Pt(-36)
                if label:
                    p.add_run(f"{label}\t").bold = True
                p.add_run(text)
        elif s["kind"] == "bullets":
            for item in s["items"]:
                doc.add_paragraph(item, style="List Bullet")
        else:
            if not s["questions"]:
                doc.add_paragraph("None. The agent found no material ambiguity to flag.")
            for q in s["questions"]:
                doc.add_paragraph().add_run(q["question"]).bold = True
                if q["why"]:
                    why = doc.add_paragraph()
                    why.add_run("Why it matters: ").italic = True
                    why.add_run(q["why"])
                for option, recommended in q["options"]:
                    p = doc.add_paragraph(style="List Bullet")
                    p.add_run(option)
                    if recommended:
                        p.add_run("  (recommended)").bold = True

    # Footer: "Generated by Blueprint  ·  Page X of Y" (the numbers are Word fields).
    def field(paragraph, instruction: str) -> None:
        run = paragraph.add_run()
        for kind, text in (("begin", None), (None, instruction), ("end", None)):
            if kind:
                el = OxmlElement("w:fldChar")
                el.set(qn("w:fldCharType"), kind)
            else:
                el = OxmlElement("w:instrText")
                el.set(qn("xml:space"), "preserve")
                el.text = text
            run._r.append(el)
        run.font.size = Pt(9)

    footer = doc.sections[0].footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    lead = footer.add_run("Generated by Blueprint  ·  Page ")
    lead.font.size = Pt(9)
    field(footer, "PAGE")
    footer.add_run(" of ").font.size = Pt(9)
    field(footer, "NUMPAGES")

    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


# ------------------------------------------------------------
# PDF
# ------------------------------------------------------------

_pdf_fonts: Dict[str, Any] = {}


def _register_pdf_fonts() -> set:
    """Register the font family once; returns the set of characters it can draw."""
    if "glyphs" in _pdf_fonts:
        return _pdf_fonts["glyphs"]
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for name, style in (("Vera", "regular"), ("Vera-Bold", "bold"),
                        ("Vera-Italic", "italic"), ("Vera-BoldItalic", "bold-italic")):
        pdfmetrics.registerFont(TTFont(name, fonts.font_path(style)))
    pdfmetrics.registerFontFamily("Vera", normal="Vera", bold="Vera-Bold", italic="Vera-Italic",
                                  boldItalic="Vera-BoldItalic")
    _pdf_fonts["glyphs"] = fonts.drawable_glyphs()
    return _pdf_fonts["glyphs"]


def _pdf_text(value: str, glyphs: set) -> str:
    # Escape for reportlab's mini-markup, swap characters the font lacks, keep line breaks.
    return escape(fonts.stand_in(value)).replace("\t", "    ").replace("\n", "<br/>")


def to_pdf(requirement: Dict[str, Any], meta: Optional[Dict[str, Any]] = None) -> bytes:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer
    except ImportError as exc:
        raise MissingLibrary(f"PDF export needs reportlab. Install it with: pip install {_REQUIRES['pdf']}") from exc

    glyphs = _register_pdf_fonts()
    t = lambda value: _pdf_text(value, glyphs)
    m = document_model(requirement, meta)

    ink, grey, accent = colors.HexColor("#1B1F2A"), colors.HexColor("#6B7280"), colors.HexColor("#4F46E5")
    body = ParagraphStyle("body", fontName="Vera", fontSize=9.5, leading=14, textColor=ink, spaceAfter=4)
    title = ParagraphStyle("title", parent=body, fontName="Vera-Bold", fontSize=18, leading=23, spaceAfter=3)
    small = ParagraphStyle("small", parent=body, fontSize=8.5, leading=12, textColor=grey, spaceAfter=10)
    heading = ParagraphStyle("heading", parent=body, fontName="Vera-Bold", fontSize=11, leading=15,
                             textColor=colors.HexColor("#374151"), spaceBefore=16, spaceAfter=5)
    item = ParagraphStyle("item", parent=body, leftIndent=40, firstLineIndent=0, bulletIndent=4,
                          bulletFontName="Vera-Bold")
    bullet = ParagraphStyle("bullet", parent=body, leftIndent=18, bulletIndent=6)
    option = ParagraphStyle("option", parent=body, leftIndent=34, bulletIndent=22)
    question = ParagraphStyle("question", parent=body, fontName="Vera-Bold", spaceBefore=6)

    story: List[Any] = [Paragraph(t(m["title"]), title),
                        Paragraph(t("  ·  ".join(f"{k}: {v}" for k, v in m["meta"])), small)]
    for s in m["sections"]:
        story.append(Paragraph(t(s["heading"]), heading))
        story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#D1D5DB"), spaceAfter=6))
        if s["kind"] == "text":
            story.append(Paragraph(t(s["text"]), body))
        elif s["kind"] == "numbered":
            for label, text in s["items"]:
                # (bulletText is drawn as plain text, in the bold bullet font set above)
                story.append(Paragraph(t(text), item, bulletText=label or None))
        elif s["kind"] == "bullets":
            for text in s["items"]:
                story.append(Paragraph(t(text), bullet, bulletText="•"))
        else:
            if not s["questions"]:
                story.append(Paragraph("None. The agent found no material ambiguity to flag.", body))
            for q in s["questions"]:
                block = [Paragraph(t(q["question"]), question)]
                if q["why"]:
                    block.append(Paragraph(f"<i>Why it matters:</i> {t(q['why'])}", body))
                for text, recommended in q["options"]:
                    block.append(Paragraph(t(text) + ("  <b>(recommended)</b>" if recommended else ""),
                                           option, bulletText="•"))
                story.append(KeepTogether(block))

    def footer(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont("Vera", 8)
        canvas.setFillColor(grey)
        canvas.drawCentredString(A4[0] / 2, 1.1 * cm, f"Generated by Blueprint  ·  Page {doc.page}")
        canvas.restoreState()

    buffer = BytesIO()
    SimpleDocTemplate(buffer, pagesize=A4, title=m["title"], author="Blueprint",
                      leftMargin=2.2 * cm, rightMargin=2.2 * cm, topMargin=2 * cm, bottomMargin=2 * cm
                      ).build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


# ------------------------------------------------------------
# One entry point
# ------------------------------------------------------------

def build(requirement: Dict[str, Any], meta: Optional[Dict[str, Any]], fmt: str) -> Tuple[bytes, str, str]:
    """Make the document in `fmt` ("docx" or "pdf"). Returns (file bytes, suggested
    file name, MIME type). Raises MissingLibrary if the needed library is absent."""
    if fmt not in FORMATS:
        raise ValueError(f"Unknown format {fmt!r}; choose one of {sorted(FORMATS)}")
    data = to_docx(requirement, meta) if fmt == "docx" else to_pdf(requirement, meta)
    return data, suggested_filename(requirement, fmt), FORMATS[fmt][2]
