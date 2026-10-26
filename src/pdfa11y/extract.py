"""Page content extraction with operator provenance.

pdfminer tells us *what* is on a page and *where*. To tag a PDF we also need to
know *which content-stream operator* painted each character, image and form, so
that marked-content markers can be inserted around exactly those operators.

We get that by wrapping pdfminer's content parser to count operators. Anything
painted while a page-level operator executes (including everything inside a
Form XObject that operator invokes) is attributed to that page-level operator.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf
from pdfminer import pdfinterp
from pdfminer.converter import PDFPageAggregator
from pdfminer.layout import (LAParams, LTAnno, LTChar, LTCurve, LTFigure, LTImage, LTLine, LTPage,
                             LTRect, LTTextLine)
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pdfminer.psparser import PSEOF, PSKeyword, keyword_name

BOLD_HINTS = ("bold", "black", "heavy", "semibold", "demi", "extrabold")


class _Tracker:
    def __init__(self) -> None:
        self.stack: list = []
        self.page_op = -1
        self.names: list[str] = []


TRACK = _Tracker()


class _CountingParser(pdfinterp.PDFContentParser):
    """Counts operators; records the current page-level operator index."""

    def __init__(self, streams):
        super().__init__(streams)
        self._n = -1
        self._depth = len(TRACK.stack)
        TRACK.stack.append(self)

    def nextobject(self):
        try:
            pos, obj = super().nextobject()
        except PSEOF:
            if TRACK.stack and TRACK.stack[-1] is self:
                TRACK.stack.pop()
            raise
        if isinstance(obj, PSKeyword):
            self._n += 1
            if self._depth == 0:
                TRACK.page_op = self._n
                TRACK.names.append(keyword_name(obj))
        return pos, obj


class _Device(PDFPageAggregator):
    def render_char(self, *args, **kwargs):
        w = super().render_char(*args, **kwargs)
        objs = self.cur_item._objs
        if objs and isinstance(objs[-1], LTChar):
            objs[-1].opidx = TRACK.page_op
        return w

    def begin_figure(self, name, bbox, matrix):
        super().begin_figure(name, bbox, matrix)
        self.cur_item.opidx = TRACK.page_op


# --------------------------------------------------------------------------- data model

@dataclass
class Char:
    op: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str


@dataclass
class Line:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    bold: bool
    font: str
    chars: list[Char] = field(default_factory=list)
    toc: bool = False              # a table-of-contents row (title + page number merged)
    tx0: float = 0.0               # left edge of the title part of a TOC row

    @property
    def ops(self) -> set[int]:
        return {c.op for c in self.chars}


@dataclass
class Figure:
    """An image or form XObject painted by page-level operator `op`."""
    op: int
    x0: float
    y0: float
    x1: float
    y1: float
    kind: str                      # 'image' | 'form' (has text) | 'graphic' (vector only)
    text: str = ""                 # text painted inside (forms only)
    chars: list[Char] = field(default_factory=list)
    xid: tuple | None = None       # objgen of the XObject, to spot forms reused across pages


@dataclass
class Shape:
    """A painted vector shape (rule, box, band). Colours are grey/RGB/CMYK tuples or None."""
    x0: float
    y0: float
    x1: float
    y1: float
    stroke: bool
    fill: bool
    linewidth: float
    fill_color: tuple | None
    stroke_color: tuple | None
    kind: str                      # 'line' | 'rect' | 'curve'

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0


@dataclass
class PageData:
    index: int                     # 0-based
    width: float
    height: float
    lines: list[Line]
    figures: list[Figure]
    ops: list                      # pikepdf content-stream instructions
    aligned: bool                  # operator indexes match pikepdf's parse
    align_note: str = ""
    shapes: list[Shape] = field(default_factory=list)


def _is_bold(fontname: str) -> bool:
    n = fontname.lower()
    return any(h in n for h in BOLD_HINTS)


def _line_from(lt: LTTextLine) -> Line | None:
    chars: list[Char] = []
    sizes: dict[float, int] = {}
    fonts: dict[str, int] = {}
    parts: list[str] = []
    for o in lt:
        if isinstance(o, LTChar):
            op = getattr(o, "opidx", None)
            if op is None:
                continue
            chars.append(Char(op, o.x0, o.y0, o.x1, o.y1, o.get_text()))
            s = round(o.size, 1)
            sizes[s] = sizes.get(s, 0) + 1
            fonts[o.fontname] = fonts.get(o.fontname, 0) + 1
            parts.append(o.get_text())
        elif isinstance(o, LTAnno):
            parts.append(o.get_text())
    text = "".join(parts).strip("\n")
    if not chars or not text.strip():
        return None
    size = max(sizes, key=sizes.get)
    font = max(fonts, key=fonts.get)
    return Line(text.strip(), lt.x0, lt.y0, lt.x1, lt.y1, size, _is_bold(font), font, chars)


def _color(c):
    if c is None:
        return None
    try:
        return tuple(round(float(x), 3) for x in (c if isinstance(c, (list, tuple)) else [c]))
    except Exception:  # noqa: BLE001
        return None


def _collect_shapes(container, shapes: list[Shape]) -> None:
    for o in container:
        if isinstance(o, (LTLine, LTRect, LTCurve)):
            kind = "line" if isinstance(o, LTLine) else ("rect" if isinstance(o, LTRect) else "curve")
            shapes.append(Shape(o.x0, o.y0, o.x1, o.y1, bool(o.stroke), bool(o.fill), float(o.linewidth or 0),
                                _color(o.non_stroking_color) if o.fill else None,
                                _color(o.stroking_color) if o.stroke else None, kind))
        elif isinstance(o, LTFigure):
            _collect_shapes(o, shapes)


def _collect(container, lines: list[Line], figures: list[Figure]) -> None:
    for o in container:
        if isinstance(o, LTFigure):
            chars = [Char(getattr(o, "opidx", -1), c.x0, c.y0, c.x1, c.y1, c.get_text())
                     for c in _walk_chars(o)]
            text = "".join(c.text for c in chars).strip()
            if text:
                kind = "form"
                x0, y0 = min(c.x0 for c in chars), min(c.y0 for c in chars)
                x1, y1 = max(c.x1 for c in chars), max(c.y1 for c in chars)
            else:
                kind = "image" if _has_image(o) else "graphic"
                x0, y0, x1, y1 = o.x0, o.y0, o.x1, o.y1
            figures.append(Figure(getattr(o, "opidx", -1), x0, y0, x1, y1, kind, text, chars))
        elif isinstance(o, LTTextLine):
            ln = _line_from(o)
            if ln:
                lines.append(ln)
        elif hasattr(o, "__iter__") and not isinstance(o, (LTChar, LTAnno)):
            _collect(o, lines, figures)


def _has_image(fig) -> bool:
    for o in fig:
        if isinstance(o, LTImage) or (isinstance(o, LTFigure) and _has_image(o)):
            return True
    return False


def _walk_chars(fig):
    for o in fig:
        if isinstance(o, LTChar):
            yield o
        elif isinstance(o, LTFigure):
            yield from _walk_chars(o)


# --------------------------------------------------------------------------- public

def extract_pages(path: str | Path, pdf: pikepdf.Pdf | None = None, only: set[int] | None = None):
    """Yield PageData for each page. `only` = 0-based page numbers to process."""
    path = Path(path)
    own = pdf is None
    if own:
        pdf = pikepdf.open(path)
    original = pdfinterp.PDFContentParser
    pdfinterp.PDFContentParser = _CountingParser
    try:
        with open(path, "rb") as fh:
            doc = PDFDocument(PDFParser(fh))
            rsrc = PDFResourceManager()
            laparams = LAParams(char_margin=1.5, line_margin=0.5, boxes_flow=None, all_texts=False)
            device = _Device(rsrc, laparams=laparams)
            interp = PDFPageInterpreter(rsrc, device)
            for i, page in enumerate(PDFPage.create_pages(doc)):
                if only is not None and i not in only:
                    continue
                TRACK.stack.clear()
                TRACK.names = []
                TRACK.page_op = -1
                lines: list[Line] = []
                figures: list[Figure] = []
                try:
                    interp.process_page(page)
                    layout: LTPage = device.get_result()
                    _collect(layout, lines, figures)
                    shapes: list[Shape] = []
                    _collect_shapes(layout, shapes)
                except Exception as e:  # noqa: BLE001
                    yield PageData(i, 0, 0, [], [], [], False, f"pdfminer failed: {e}")
                    continue
                pk_page = pdf.pages[i]
                ops = pikepdf.parse_content_stream(pk_page.obj)
                pk_names = [str(o.operator) for o in ops]
                ok = TRACK.names == pk_names
                note = ""
                if not ok:
                    note = f"operator mismatch: pdfminer {len(TRACK.names)} vs pikepdf {len(pk_names)}"
                try:
                    xobjs = pk_page.obj.Resources.get("/XObject", {})
                    for fig in figures:
                        if 0 <= fig.op < len(ops) and str(ops[fig.op].operator) == "Do":
                            xo = xobjs.get(ops[fig.op].operands[0])
                            if xo is not None and xo.is_indirect:
                                fig.xid = xo.objgen
                except Exception:  # noqa: BLE001
                    pass
                mb = pk_page.obj.MediaBox
                w, h = float(mb[2]) - float(mb[0]), float(mb[3]) - float(mb[1])
                yield PageData(i, w, h, lines, figures, ops, ok, note, shapes)
    finally:
        pdfinterp.PDFContentParser = original
        if own:
            pdf.close()
