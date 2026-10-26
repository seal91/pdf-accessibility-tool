"""Layout analysis: turns extracted lines/figures into an ordered list of semantic blocks.

Pipeline per document:
  1. body-text size statistics
  2. running headers / footers / page numbers  -> artifacts
  3. per page: XY-cut reading order -> paragraph grouping -> classification
     (heading levels, list items, figures)
  4. every show/paint operator gets an owner (block or artifact)

Everything here is heuristic and deliberately conservative: when in doubt a
block stays a plain paragraph and gets flagged for review rather than guessed.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .extract import Figure, Line, PageData
from .tables import Table, detect_tables

BAND = 0.12                       # top/bottom fraction of the page that counts as header/footer zone
PAGE_NUM = re.compile(r"^\W*(page\s+|side\s+|p\.\s*|página\s+)?\d{1,4}(\s*(/|af|of|de)\s*\d{1,4})?\W*$", re.I)
BULLET = re.compile(r"^\s*[•▪◦●■◆▶►‣⁃·]\s*")
WEAK_MARK = re.compile(r"^\s*([–—-](?=\s)|\(?\d{1,2}[.)]|\(?[a-zA-Z][.)])\s+")
LIST_MARK = re.compile(r"^\s*([•▪◦●■◆▶►‣⁃·]\s*|[–—-](?=\s)\s+|\(?\d{1,2}[.)]\s+|\(?[a-zA-Z][.)]\s+)")
TOC_TITLE = re.compile(r"^\s*(indhold(sfortegnelse)?|contents?|table of contents?|table of content|índice|indice|tabla de contenidos?|sumario|sommaire)\s*$", re.I)
MARK_ONLY = re.compile(r"^\s*[•▪◦●■◆▶►‣⁃·–—-]\s*$")
SENT_END = re.compile(r"[.!?:;”\"’)]\s*$")


@dataclass
class Block:
    kind: str                      # H | P | LI | TOCI | Table | TH | TD | Figure | Artifact
    page: int
    lines: list[Line] = field(default_factory=list)
    fig: Figure | None = None
    table: Table | None = None
    level: int = 0                 # heading level
    subtype: str = ""              # artifact: Header | Footer | '' ; figure: image | decorative
    note: str = ""                 # review remark
    ops: set[int] = field(default_factory=set)

    @property
    def text(self) -> str:
        if self.lines:
            return re.sub(r"\s+", " ", " ".join(l.text for l in self.lines)).strip()
        return (self.fig.text if self.fig else "").strip()

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        if self.lines:
            return (min(l.x0 for l in self.lines), min(l.y0 for l in self.lines),
                    max(l.x1 for l in self.lines), max(l.y1 for l in self.lines))
        if self.table is not None:
            return self.table.bbox
        f = self.fig
        return (f.x0, f.y0, f.x1, f.y1) if f else (0, 0, 0, 0)

    @property
    def size(self) -> float:
        c: Counter = Counter()
        for l in self.lines:
            c[l.size] += len(l.chars)
        return c.most_common(1)[0][0] if c else 0.0

    @property
    def bold_share(self) -> float:
        b = sum(len(l.chars) for l in self.lines if l.bold)
        return b / max(1, sum(len(l.chars) for l in self.lines))

    @property
    def bold(self) -> bool:
        return self.bold_share > 0.6


@dataclass
class PageLayout:
    page: int
    blocks: list[Block]                          # reading order (artifacts included, at the end)
    owners: dict[int, tuple]                     # op index -> ('blk', id(block)) | ('art', subtype)
    review: list[str] = field(default_factory=list)


@dataclass
class DocStats:
    body_size: float
    heading_sizes: list[float]                   # descending; index+1 = level


# --------------------------------------------------------------------------- helpers

def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", t.lower())).strip()


def _band(y0: float, y1: float, height: float) -> str:
    yc = (y0 + y1) / 2
    if yc > height * (1 - BAND):
        return "Header"
    if yc < height * BAND:
        return "Footer"
    return ""


def body_size(pages: list[PageData]) -> float:
    c: Counter = Counter()
    for p in pages:
        for l in p.lines:
            if not _band(l.y0, l.y1, p.height):
                c[l.size] += len(l.chars)
    return c.most_common(1)[0][0] if c else 10.0


def running_elements(pages: list[PageData]) -> dict[int, dict[str, str]]:
    """{page: {'L<i>' or 'F<i>': subtype}} for lines/forms that are running headers/footers."""
    n = len(pages)
    seen: dict[tuple, set] = defaultdict(set)
    for p in pages:
        for i, l in enumerate(p.lines):
            b = _band(l.y0, l.y1, p.height)
            if b:
                seen[(_norm(l.text), b)].add(p.index)
        for i, f in enumerate(p.figures):
            if f.kind == "form" and f.text:
                b = _band(f.y0, f.y1, p.height)
                if b:
                    seen[(_norm(f.text), b)].add(p.index)
    need = 2 if n < 8 else 3
    repeated = {k for k, v in seen.items() if len(v) >= need}

    out: dict[int, dict[str, str]] = {}
    for p in pages:
        m: dict[str, str] = {}
        for i, l in enumerate(p.lines):
            b = _band(l.y0, l.y1, p.height)
            edge = (l.y0 + l.y1) / 2 > p.height * 0.93 or (l.y0 + l.y1) / 2 < p.height * 0.07
            is_num = bool(PAGE_NUM.match(l.text))
            if b and ((is_num and edge) or (not is_num and (_norm(l.text), b) in repeated)):
                m[f"L{i}"] = b
        for i, f in enumerate(p.figures):
            if f.kind == "form" and f.text:
                b = _band(f.y0, f.y1, p.height)
                is_num = bool(PAGE_NUM.match(f.text))
                shared = (not is_num) and ((_norm(f.text), b) in repeated if b else False)
                edge = (f.y0 + f.y1) / 2 > p.height * 0.93 or (f.y0 + f.y1) / 2 < p.height * 0.07
                if b and (shared or (is_num and edge)):
                    m[f"F{i}"] = b
        out[p.index] = m
    return out


# --------------------------------------------------------------------------- reading order (XY-cut)

def _median_width(ls: list[Line]) -> float:
    return statistics.median(l.x1 - l.x0 for l in ls)


def _v_split(ls: list[Line], body: float):
    gutter = max(10.0, 1.1 * body)
    srt = sorted(ls, key=lambda l: l.x0)
    run = srt[0].x1
    for i in range(1, len(srt)):
        if srt[i].x0 - run >= gutter:
            left, right = srt[:i], srt[i:]
            wl, wr = _median_width(left), _median_width(right)
            enough = lambda g, w: (len(g) >= 2 and w >= 7 * body) or w >= 12 * body   # noqa: E731
            if enough(left, wl) and enough(right, wr):
                return left, right
        run = max(run, srt[i].x1)
    return None


def _h_split(ls: list[Line], gap_min: float):
    srt = sorted(ls, key=lambda l: -l.y1)
    low = srt[0].y0
    for i in range(1, len(srt)):
        if low - srt[i].y1 >= gap_min:
            return srt[:i], srt[i:]
        low = min(low, srt[i].y0)
    return None


def _rows(ls: list[Line]) -> list[Line]:
    """Row-major order: top to bottom, left to right within a row."""
    srt = sorted(ls, key=lambda l: -(l.y0 + l.y1) / 2)
    rows: list[list[Line]] = []
    for l in srt:
        yc = (l.y0 + l.y1) / 2
        if rows and abs(yc - (rows[-1][0].y0 + rows[-1][0].y1) / 2) < 0.4 * (l.y1 - l.y0):
            rows[-1].append(l)
        else:
            rows.append([l])
    return [l for r in rows for l in sorted(r, key=lambda l: l.x0)]


def xy_order(ls: list[Line], body: float) -> list[Line]:
    if len(ls) <= 1:
        return list(ls)
    v = _v_split(ls, body)
    if v:
        return xy_order(v[0], body) + xy_order(v[1], body)
    h = _h_split(ls, 0.5 * statistics.median(l.y1 - l.y0 for l in ls))
    if h:
        return xy_order(h[0], body) + xy_order(h[1], body)
    return _rows(ls)


# --------------------------------------------------------------------------- paragraphs

def _same_style(a: Line, b: Line) -> bool:
    return abs(a.size - b.size) <= 0.06 * max(a.size, b.size) and a.bold == b.bold


def _continues(blk: list[Line], ln: Line) -> bool:
    prev = blk[-1]
    if ln.toc:
        return False
    if blk[0].toc:                                   # wrapped title of a contents row
        pitch = prev.y0 - ln.y0
        return (abs(ln.x0 - blk[0].tx0) <= 0.7 * ln.size and 0.4 * ln.size < pitch <= 1.65 * ln.size
                and abs(ln.size - blk[0].size) <= 0.06 * blk[0].size)
    if not _same_style(prev, ln):
        # a bold lead-in sentence that wraps into regular text is one paragraph, not a heading + body:
        # the bold line runs to the same right edge as the text below it and does not end a sentence
        run_in = (abs(prev.size - ln.size) <= 0.06 * max(prev.size, ln.size) and prev.bold and not ln.bold
                  and abs(prev.x1 - ln.x1) <= 1.5 * ln.size and not SENT_END.search(prev.text))
        if not run_in:
            return False
    if LIST_MARK.match(ln.text) and not LIST_MARK.match(prev.text):
        return False
    if LIST_MARK.match(ln.text) and LIST_MARK.match(prev.text):
        return False
    size = ln.size
    pitch = prev.y0 - ln.y0
    if not (0.4 * size < pitch <= 1.65 * size):      # not below, or a paragraph gap
        return False
    bx0 = min(l.x0 for l in blk)
    ov = min(prev.x1, ln.x1) - max(prev.x0, ln.x0)
    if ov < 0.3 * min(prev.x1 - prev.x0, ln.x1 - ln.x0):
        return False
    left_ok = (bx0 - 0.6 * size) <= ln.x0 <= (bx0 + 3.5 * size)
    centre_ok = abs((ln.x0 + ln.x1) / 2 - (prev.x0 + prev.x1) / 2) <= 1.5 * size
    right_ok = abs(ln.x1 - prev.x1) <= 1.0 * size
    if not (left_ok or centre_ok or right_ok):
        return False
    # first-line indent after a finished sentence starts a new paragraph
    if ln.x0 - bx0 >= 0.9 * size and SENT_END.search(prev.text) and len(blk) >= 1 and ln.x0 > prev.x0 + 0.9 * size:
        return False
    return True


def group_paragraphs(ordered: list[Line]) -> list[list[Line]]:
    blocks: list[list[Line]] = []
    for ln in ordered:
        best, best_d = None, 1e9
        for b in blocks[-6:]:                      # only recent blocks can still be open
            if _continues(b, ln):
                d = b[-1].y0 - ln.y0
                if d < best_d:
                    best, best_d = b, d
        if best is not None:
            best.append(ln)
        else:
            blocks.append([ln])
    return blocks


def _merge_markers(lines: list[Line]) -> list[Line]:
    """A bullet drawn as its own line joins the text line beside it."""
    out: list[Line] = []
    used: set[int] = set()
    for i, m in enumerate(lines):
        if not MARK_ONLY.match(m.text):
            continue
        best, bd = None, 1e9
        for j, t in enumerate(lines):
            if j == i or j in used or MARK_ONLY.match(t.text):
                continue
            same_row = abs((m.y0 + m.y1) / 2 - (t.y0 + t.y1) / 2) < 0.5 * max(m.y1 - m.y0, t.y1 - t.y0)
            dx = t.x0 - m.x1
            if same_row and -2 <= dx <= 3 * max(m.size, t.size) and dx < bd:
                best, bd = j, dx
        if best is not None:
            t = lines[best]
            lines[best] = Line(m.text.strip() + " " + t.text, min(m.x0, t.x0), min(m.y0, t.y0), max(m.x1, t.x1),
                               max(m.y1, t.y1), t.size, t.bold, t.font, m.chars + t.chars)
            used.add(i)
    return [l for i, l in enumerate(lines) if i not in used]


def _merge_toc_rows(lines: list[Line], body: float) -> list[Line]:
    """On a contents page, glue each row's pieces (label, title, page number) into one line."""
    nums = [l for l in lines if re.fullmatch(r"\d{1,4}([–-]\d{1,4})?", l.text.strip()) and l.size <= 1.5 * body]
    used: set[int] = set()
    rows: list[Line] = []
    for n in nums:
        pieces = [l for l in lines if l is not n and id(l) not in used
                  and abs((l.y0 + l.y1) / 2 - (n.y0 + n.y1) / 2) < 0.5 * max(l.y1 - l.y0, n.y1 - n.y0)
                  and l.x1 < n.x0 - 1.5 * l.size and not re.fullmatch(r"\d{1,4}", l.text.strip())]
        if not pieces:
            continue
        pieces.sort(key=lambda l: l.x0)
        title = max(pieces, key=lambda l: l.x1 - l.x0)
        chars = [c for l in pieces + [n] for c in l.chars]
        row = Line(" ".join(l.text for l in pieces + [n]), min(l.x0 for l in pieces), min(l.y0 for l in pieces + [n]),
                   n.x1, max(l.y1 for l in pieces + [n]), title.size, title.bold, title.font, chars,
                   toc=True, tx0=title.x0)
        rows.append(row)
        used.update(id(l) for l in pieces)
        used.add(id(n))
    return [l for l in lines if id(l) not in used] + rows


def _validate_weak_lists(blocks: list[Block]) -> None:
    """Numbered/lettered/dashed lists count only when at least two consecutive items agree."""
    i = 0
    while i < len(blocks):
        if blocks[i].kind != "LI?":
            i += 1
            continue
        j = i
        while j + 1 < len(blocks) and blocks[j + 1].kind == "LI?" and abs(blocks[j + 1].bbox[0] - blocks[i].bbox[0]) < 6:
            j += 1
        run = blocks[i:j + 1]
        ok = len(run) >= 2
        if ok and re.match(r"^\s*\(?\d", run[0].lines[0].text):
            nums = [int(re.search(r"\d+", b.lines[0].text).group()) for b in run]
            ok = all(b - a == 1 for a, b in zip(nums, nums[1:]))
        for b in run:
            b.kind = "LI" if ok else "P"
        i = j + 1


# --------------------------------------------------------------------------- classification

def _is_heading_style(b: Block, stats_body: float) -> bool:
    if not b.lines or len(b.lines) > 4:
        return False
    text = b.text
    if len(text) > 160 or sum(ch.isalpha() for ch in text) < 2:
        return False
    if LIST_MARK.match(text):
        return False
    if b.size >= 1.3 * stats_body:
        return True
    multi_sentence = re.search(r"[.!?]\s+[A-ZÁÉÍÓÚÑÆØÅ¿¡]", text) is not None
    return (b.bold_share >= 0.9 and b.size >= 0.97 * stats_body and len(b.lines) <= 2
            and not SENT_END.search(text[-3:]) and not multi_sentence)


def heading_levels(blocks_by_page: list[list[Block]], body: float) -> list[float]:
    """Cluster heading sizes into levels. A size earns its own level if it is used by
    at least two blocks (or is the largest); sizes within 12% of a level's top size merge into it."""
    sizes: Counter = Counter()
    for blocks in blocks_by_page:
        for b in blocks:
            if b.kind == "P" and b.lines and _is_heading_style(b, body):
                sizes[round(b.size * 2) / 2] += 1
    if not sizes:
        return []
    biggest = max(sizes)
    levels: list[float] = []
    for sz in sorted(sizes, reverse=True):
        if sizes[sz] < 2 and sz != biggest:
            continue
        if not levels or sz < levels[-1] * TOL:
            levels.append(sz)
    return levels[:6]


TOL = 0.88


def _level_for(size: float, levels: list[float]) -> int:
    for i, l in enumerate(levels):
        if size >= l * TOL:
            return i + 1
    return len(levels)


# --------------------------------------------------------------------------- per page

def analyse_page(page: PageData, body: float, running: dict[str, str], levels: list[float] | None,
                 toc_mode: bool = False) -> PageLayout:
    review: list[str] = []
    art_lines = {int(k[1:]): v for k, v in running.items() if k[0] == "L"}
    art_figs = {int(k[1:]): v for k, v in running.items() if k[0] == "F"}

    art_ids = {id(l) for i, l in enumerate(page.lines) if i in art_lines}
    tables: list[Table] = []
    if not toc_mode and not any(TOC_TITLE.match(l.text) and l.size >= 1.3 * body for l in page.lines):
        tables = [t for t in detect_tables(page, body) if not any(id(l) in art_ids for r in t.cells for c in r for l in c.lines)]
    table_ids = {id(l) for t in tables for r in t.cells for c in r for l in c.lines}
    table_blocks: list[Block] = []
    for t in tables:
        tb = Block("Table", page.index, table=t)
        for r in t.cells:
            for c in r:
                if c.lines:
                    c.block = Block("TH" if c.header else "TD", page.index, lines=c.lines)
        table_blocks.append(tb)
    content_lines = [l for i, l in enumerate(page.lines) if i not in art_lines and id(l) not in table_ids]
    content_lines = _merge_markers(content_lines)
    if toc_mode or any(TOC_TITLE.match(l.text) and l.size >= 1.3 * body for l in content_lines):
        content_lines = _merge_toc_rows(content_lines, body)
    # huge stand-alone numerals (chapter openers) would wreck column detection: set aside
    numerals = [l for l in content_lines if len(l.text) <= 3 and l.size >= 3 * body and re.fullmatch(r"\d{1,2}", l.text)]
    content_lines = [l for l in content_lines if l not in numerals]
    ordered = xy_order(content_lines, body) if content_lines else []
    groups = group_paragraphs(ordered)
    numeral_blocks = [Block("P", page.index, lines=[n]) for n in numerals]

    blocks: list[Block] = []
    for g in groups:
        b = Block("P", page.index, lines=g)
        if g[0].toc:
            b.kind = "TOCI"
        elif BULLET.match(g[0].text) and not _is_heading_style(b, body):
            b.kind = "LI"
        elif WEAK_MARK.match(g[0].text) and not _is_heading_style(b, body):
            b.kind = "LI?"
        blocks.append(b)
    _validate_weak_lists(blocks)
    blocks = numeral_blocks + blocks

    # figures (in rough reading position: after text blocks above them)
    page_area = page.width * page.height
    fig_blocks: list[Block] = []
    for i, f in enumerate(page.figures):
        if f.op < 0:
            continue
        if i in art_figs:
            fig_blocks.append(Block("Artifact", page.index, fig=f, subtype=art_figs[i]))
            continue
        area = max(0, f.x1 - f.x0) * max(0, f.y1 - f.y0)
        if f.kind == "form":
            b = Block("P", page.index, fig=f)
            b.note = "text inside a form XObject, tagged as one unit"
            fig_blocks.append(b)
        elif f.kind == "graphic":
            fig_blocks.append(Block("Artifact", page.index, fig=f, subtype="graphic"))
        else:  # image
            if area >= 0.85 * page_area:
                fig_blocks.append(Block("Artifact", page.index, fig=f, subtype="background",
                                        note="full-page image treated as decorative background: verify"))
            elif (f.x1 - f.x0) < 6 or (f.y1 - f.y0) < 6:
                fig_blocks.append(Block("Artifact", page.index, fig=f, subtype="decorative"))
            else:
                fig_blocks.append(Block("Figure", page.index, fig=f))

    # headings
    if levels is not None:
        for b in blocks:
            if b.kind == "P" and b.lines and _is_heading_style(b, body):
                b.kind = "H"
                b.level = _level_for(b.size, levels)

    # a huge stand-alone numeral (chapter opener) joins the heading beside it
    merged: list[Block] = []
    skip: set[int] = set()
    for i, b in enumerate(blocks):
        if id(b) in skip:
            continue
        if (b.lines and len(b.text) <= 2 and b.size >= 3 * body and re.fullmatch(r"\d{1,2}", b.text)):
            for c in blocks[i + 1:i + 3]:
                if c.kind == "H" and c.bbox[0] >= b.bbox[0] and min(c.bbox[3], b.bbox[3]) - max(c.bbox[1], b.bbox[1]) > 0:
                    c.lines = b.lines + c.lines
                    skip.add(id(b))
                    break
            else:
                b.kind = "H"
                b.level = 1 if levels else 0
        if id(b) not in skip:
            merged.append(b)
    blocks = merged

    # stacked pieces of one heading ("01" / "El problema:" / "Un sector clave...") become one heading
    joined: list[Block] = []
    for b in blocks:
        a = joined[-1] if joined else None
        if (a is not None and a.kind == "H" and b.kind == "H" and a.level == b.level and a.lines and b.lines
                and 0 <= a.bbox[1] - b.bbox[3] <= 0.8 * a.size and abs(a.bbox[0] - b.bbox[0]) <= max(a.size, b.size)):
            a.lines = a.lines + b.lines
        else:
            joined.append(b)
    blocks = joined

    # a non-bold label directly above a larger heading is a kicker/eyebrow, not a heading
    for a, c in zip(blocks, blocks[1:]):
        if a.kind == "H" and c.kind == "H" and a.level > c.level and not a.bold:
            a.kind, a.level = "P", 0

    # interleave figures into reading order by their top edge
    ordered_blocks: list[Block] = []
    body_blocks = [b for b in blocks]
    figs_content = [b for b in fig_blocks if b.kind != "Artifact"] + table_blocks
    for fb in sorted(figs_content, key=lambda b: -b.bbox[3]):
        placed = False
        for i, b in enumerate(body_blocks):
            if b.bbox[3] < fb.bbox[3] - 1 and b.bbox[0] >= fb.bbox[0] - 5 and b.bbox[2] <= fb.bbox[2] + 5:
                body_blocks.insert(i, fb)
                placed = True
                break
        if not placed:
            body_blocks.append(fb)
    ordered_blocks = body_blocks

    artifacts = [b for b in fig_blocks if b.kind == "Artifact"]
    for i, l in enumerate(page.lines):
        if i in art_lines:
            artifacts.append(Block("Artifact", page.index, lines=[l], subtype=art_lines[i]))

    # ---- operator ownership --------------------------------------------------
    owners: dict[int, tuple] = {}
    votes: dict[int, Counter] = defaultdict(Counter)
    owning = list(ordered_blocks) + artifacts
    for b in ordered_blocks:
        if b.kind == "Table":
            owning.extend(c.block for r in b.table.cells for c in r if c.block is not None)
    for b in owning:
        if b.kind == "Table":
            continue
        key = ("art", b.subtype) if b.kind == "Artifact" else ("blk", id(b))
        if b.fig is not None and not b.lines:
            votes[b.fig.op][key] += max(1, len(b.fig.chars)) * 1000
        for l in b.lines:
            for c in l.chars:
                votes[c.op][key] += 1
    for op, cnt in votes.items():
        owners[op] = cnt.most_common(1)[0][0]
    by_id = {id(b): b for b in owning}
    for op, key in owners.items():
        if key[0] == "blk":
            by_id[key[1]].ops.add(op)

    return PageLayout(page.index, ordered_blocks + artifacts, owners, review)


def _toc_pages(pages: list[PageData], body: float, running) -> set[int]:
    """Pages holding a table of contents: the titled page, plus following pages that keep the pattern."""
    def titled(p):
        return any(TOC_TITLE.match(l.text) and l.size >= 1.3 * body for l in p.lines)

    def rows(p):
        art = {int(k[1:]) for k in running[p.index] if k[0] == "L"}
        lines = [l for i, l in enumerate(p.lines) if i not in art]
        return sum(1 for l in _merge_toc_rows(lines, body) if l.toc)

    out: set[int] = set()
    prev = False
    for p in pages:
        if titled(p) and rows(p) >= 3:
            out.add(p.index); prev = True
        elif prev and rows(p) >= 4:
            out.add(p.index)
        else:
            prev = False
    return out


def analyse_document(pages: list[PageData]):
    body = body_size(pages)
    running = running_elements(pages)
    toc = _toc_pages(pages, body, running)
    # pass 1 (levels unknown) to collect heading style candidates
    first = [analyse_page(p, body, running[p.index], None, p.index in toc) for p in pages]
    levels = heading_levels([[b for b in pl.blocks if b.kind == "P"] for pl in first], body)
    layouts = [analyse_page(p, body, running[p.index], levels, p.index in toc) for p in pages]
    return layouts, DocStats(body, levels)
