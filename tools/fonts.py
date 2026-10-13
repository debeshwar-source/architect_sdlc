"""
The one font the app draws with in PDFs and diagrams, and what to do with characters
it cannot draw.

Plain words: PDFs and the diagram images are drawn by this program, so they need a
font file. reportlab ships one (Bitstream Vera) that covers English and accented Latin
text. A character it lacks (an arrow, a tick, the rupee sign, Hindi...) would show as an
empty box, so `stand_in()` swaps it for readable text (`->`, `[x]`, `Rs.`) or `?`.
(Word files and Confluence pages are not affected: they use the reader's own fonts.)
"""

import os
from typing import Dict, Set


class MissingLibrary(Exception):
    """A library needed to make a document or diagram is not installed (the message says
    which, and the command that installs it)."""

_FILES = {"regular": "Vera.ttf", "bold": "VeraBd.ttf", "italic": "VeraIt.ttf", "bold-italic": "VeraBI.ttf"}

# Readable stand-ins for characters the font lacks.
STAND_INS: Dict[str, str] = {
    "→": "->", "←": "<-", "↔": "<->", "✓": "[x]", "✔": "[x]", "✗": "[ ]", "✘": "[ ]",
    "₹": "Rs.", "€": "EUR ", "£": "GBP ", "×": "x", "−": "-", "≈": "~", "≠": "!=",
    " ": " ", "​": "", " ": " ", " ": " ",
}

_glyphs: Set[int] = set()


def font_path(style: str = "regular") -> str:
    """The path of the font file for "regular", "bold", "italic" or "bold-italic"."""
    try:
        import reportlab
    except ImportError as exc:
        raise MissingLibrary("PDFs and diagrams need reportlab (it ships the font they use). "
                             "Install it with: pip install reportlab") from exc
    return os.path.join(os.path.dirname(reportlab.__file__), "fonts", _FILES[style])


def drawable_glyphs() -> Set[int]:
    """Code points the font can draw (read once, then remembered)."""
    if not _glyphs:
        path = font_path("regular")                    # raises MissingLibrary with the fix if reportlab is absent
        try:
            from reportlab.pdfbase.ttfonts import TTFont
        except ImportError as exc:
            raise MissingLibrary("PDFs and diagrams need reportlab (it ships the font they use). "
                                 "Install it with: pip install reportlab") from exc
        _glyphs.update(TTFont("VeraProbe", path).face.charToGlyph)
    return _glyphs


def stand_in(text: str) -> str:
    """`text` with every character the font cannot draw replaced by a stand-in or "?".
    Line breaks and tabs are kept."""
    glyphs = drawable_glyphs()
    return "".join(
        ch if ch in "\n\t" or ord(ch) in glyphs else STAND_INS.get(ch, "?")
        for ch in text
    )
