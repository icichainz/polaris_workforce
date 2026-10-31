"""A small PDF writer for the payslip: text, lines and filled boxes on A4 pages.

Why not a library: WeasyPrint needs Pango (not installed here), ReportLab and fpdf2 pull
in Pillow and fonttools (several MB at ~14 KB/s, shared by five agents' environments).
A payslip is a fixed layout of text, rules and boxes, which PDF can describe directly.
This writer uses the standard Helvetica fonts every PDF reader has, so nothing is
embedded and a payslip is a few KB (good for WhatsApp and slow links).

Limits: text is WinAnsi (Latin-1 plus €, –, —, “”, …): enough for English and French.
Other characters lose their accent (ŵ becomes w) or become "?" (ɔ).
Coordinates are in points (1/72 inch) from the **top-left** corner.
"""

from __future__ import annotations

import unicodedata
import zlib

A4 = (595.28, 841.89)

# Helvetica and Helvetica-Bold advance widths (1/1000 em) for ASCII 32–126, from the
# Adobe Core 14 AFM files.
_REGULAR = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
]  # fmt: skip
_BOLD = [
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
    975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
    333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,
]  # fmt: skip
# A few non-ASCII WinAnsi glyphs (same width in both weights).
_EXTRA = {
    "\xa0": 278, " ": 278, "·": 278, "½": 834, "–": 556, "—": 1000, "€": 556,
    "×": 584, "’": 222, "‘": 222, "“": 333, "”": 333, "…": 1000, "°": 400, "•": 350,
}  # fmt: skip


def _encodable(char: str) -> str:
    """The character itself if WinAnsi has it, else a transliteration or "?"."""
    if char in "\u202f\u2009":
        return " "
    if char == "\u2212":  # minus sign → en dash (WinAnsi has no minus)
        return "–"
    try:
        char.encode("cp1252")
        return char
    except UnicodeEncodeError:
        base = unicodedata.normalize("NFKD", char).encode("ascii", "ignore").decode()
        return base or "?"


def clean(text: str) -> str:
    return "".join(_encodable(c) for c in str(text))


def char_width(char: str, bold: bool) -> int:
    code = ord(char)
    if 32 <= code <= 126:
        return (_BOLD if bold else _REGULAR)[code - 32]
    if char in _EXTRA:
        return _EXTRA[char]
    base = unicodedata.normalize("NFKD", char)[:1]
    if base and 32 <= ord(base) <= 126:
        return (_BOLD if bold else _REGULAR)[ord(base) - 32]
    return 556


def text_width(text: str, size: float, bold: bool = False) -> float:
    return sum(char_width(c, bold) for c in clean(text)) * size / 1000


def _escape(text: str) -> bytes:
    data = clean(text).encode("cp1252")
    return data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _rgb(color) -> str:
    """``"#16191D"`` or an (r, g, b) tuple of 0–255 → PDF colour operands."""
    if isinstance(color, str):
        color = color.lstrip("#")
        color = tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))
    return " ".join(f"{c / 255:.3f}" for c in color)


def _n(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


class Document:
    """Pages of drawing operations, serialised by ``to_bytes()``."""

    def __init__(self, size=A4, *, title: str = "", author: str = ""):
        self.width, self.height = size
        self.title = title
        self.author = author
        self.pages: list[list[str]] = []
        self.add_page()

    @property
    def _ops(self) -> list[str]:
        return self.pages[-1]

    def add_page(self) -> None:
        self.pages.append([])

    def _y(self, y: float) -> float:
        return self.height - y

    def text(
        self,
        x: float,
        y: float,
        text: str,
        *,
        size: float = 10,
        bold: bool = False,
        color="#16191D",
        align: str = "left",
    ) -> float:
        """Draw one line of text with its baseline at ``y``; return its width."""
        width = text_width(text, size, bold)
        if align == "right":
            x -= width
        elif align == "center":
            x -= width / 2
        font = "F2" if bold else "F1"
        self._ops.append(
            f"BT /{font} {_n(size)} Tf {_rgb(color)} rg {_n(x)} {_n(self._y(y))} Td ("
            + _escape(text).decode("latin-1")
            + ") Tj ET"
        )
        return width

    def line(self, x1, y1, x2, y2, *, width: float = 0.5, color="#E3E5E9") -> None:
        self._ops.append(
            f"{_rgb(color)} RG {_n(width)} w {_n(x1)} {_n(self._y(y1))} m "
            f"{_n(x2)} {_n(self._y(y2))} l S"
        )

    def rect(self, x, y, w, h, *, fill=None, stroke=None, width: float = 0.5) -> None:
        """A box with its top-left corner at (x, y)."""
        parts = []
        if fill is not None:
            parts.append(f"{_rgb(fill)} rg")
        if stroke is not None:
            parts.append(f"{_rgb(stroke)} RG {_n(width)} w")
        parts.append(f"{_n(x)} {_n(self._y(y + h))} {_n(w)} {_n(h)} re")
        parts.append("B" if fill is not None and stroke is not None else "f" if fill else "S")
        self._ops.append(" ".join(parts))

    def to_bytes(self, *, compress: bool = True) -> bytes:
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        font_regular = add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
        )
        font_bold = add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
            b"/Encoding /WinAnsiEncoding >>"
        )
        pages_id = len(objects) + 1 + 2 * len(self.pages)  # after every page and content
        page_ids = []
        for ops in self.pages:
            stream = "\n".join(ops).encode("latin-1")
            if compress:
                stream = zlib.compress(stream)
                header = f"<< /Length {len(stream)} /Filter /FlateDecode >>".encode()
            else:
                header = f"<< /Length {len(stream)} >>".encode()
            content_id = add(header + b"\nstream\n" + stream + b"\nendstream")
            page_ids.append(
                add(
                    (
                        f"<< /Type /Page /Parent {pages_id} 0 R "
                        f"/MediaBox [0 0 {_n(self.width)} {_n(self.height)}] "
                        f"/Resources << /Font << /F1 {font_regular} 0 R /F2 {font_bold} 0 R >> >> "
                        f"/Contents {content_id} 0 R >>"
                    ).encode()
                )
            )
        kids = " ".join(f"{i} 0 R" for i in page_ids)
        if add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()) != pages_id:
            raise RuntimeError("PDF object numbering is out of step")
        info = add(
            b"<< /Title ("
            + _escape(self.title)
            + b") /Author ("
            + _escape(self.author)
            + b") /Producer (Polaris Workforce) >>"
        )
        catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode())

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        for offset in offsets:
            out += f"{offset:010d} 00000 n \n".encode()
        out += (
            f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode()
        return bytes(out)
