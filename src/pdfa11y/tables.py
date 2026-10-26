"""Table detection from vector rules, filled bands and text alignment.

Designed for what InDesign/Word actually draw: per-cell horizontal rules, filled header bands,
occasional full grids. The breaks between rule segments reveal the column boundaries.
Only ruled/banded tables with >= 2 rows and >= 2 columns are detected; anything less certain is
left as ordinary paragraphs rather than guessed.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from .extract import Line, PageData, Shape

YTOL = 0.9          # pt: edges closer than this are the same rule
XTOL = 2.0          # pt: column boundaries closer than this are the same
MARKER_ONLY = re.compile(r"^\s*([•▪◦●■◆▶►‣⁃·–—-]|\(?\d{1,2}[.)]|\(?[a-zA-Z][.)])\s*$")
NUM = re.compile(r"^[\s\W]*[-+−]?\d[\d\s.,%:/–-]*\W*$")


@dataclass
class Cell:
    row: int
    col: int
    lines: list[Line] = field(default_factory=list)
    header: bool = False
    scope: str | None = None            # 'Column' | 'Row'
    colspan: int = 1
    covered: bool = False               # swallowed by a column-spanning cell to its left
    block: object = None                # layout Block owning this cell's operators (set by layout)

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(l.text for l in self.lines)).strip()


@dataclass
class Table:
    x0: float
    y0: float
    x1: float
    y1: float
    row_edges: list[float]              # descending y, len = rows + 1
    col_edges: list[float]              # ascending x, len = cols + 1
    cells: list[list[Cell]] = field(default_factory=list)
    header_rows: int = 0
    header_cols: int = 0
    note: str = ""

    @property
    def rows(self) -> int:
        return len(self.cells)

    @property
    def cols(self) -> int:
        return len(self.cells[0]) if self.cells else 0

    @property
    def bbox(self):
        return (self.x0, self.y0, self.x1, self.y1)


# --------------------------------------------------------------------------- colours

def luma(c) -> float | None:
    if not c:
        return None
    try:
        if len(c) == 1:
            return float(c[0])
        if len(c) == 3:
            r, g, b = c
        elif len(c) == 4:
            cy, m, y, k = c
            r, g, b = (1 - cy) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)
        else:
            return None
        return 0.299 * r + 0.587 * g + 0.114 * b
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- edges

def _edges(shapes: list[Shape]):
    """Horizontal (y, x0, x1) and vertical (x, y0, y1) edges plus filled bands (x0,y0,x1,y1,luma)."""
    h: list[tuple[float, float, float]] = []
    v: list[tuple[float, float, float]] = []
    bands: list[tuple[float, float, float, float, float | None]] = []
    for s in shapes:
        if s.kind == "line":
            if s.h <= 1.0 and s.w >= 8:
                h.append(((s.y0 + s.y1) / 2, s.x0, s.x1))
            elif s.w <= 1.0 and s.h >= 8:
                v.append(((s.x0 + s.x1) / 2, s.y0, s.y1))
        elif s.kind == "rect":
            if s.h <= 1.5 and s.w >= 8:                   # a rule drawn as a thin rectangle
                h.append(((s.y0 + s.y1) / 2, s.x0, s.x1))
            elif s.w <= 1.5 and s.h >= 8:
                v.append(((s.x0 + s.x1) / 2, s.y0, s.y1))
            elif s.fill and 5 <= s.h <= 90 and s.w >= 20:  # filled band / cell background
                h.append((s.y0, s.x0, s.x1))
                h.append((s.y1, s.x0, s.x1))
                bands.append((s.x0, s.y0, s.x1, s.y1, luma(s.fill_color)))
            elif s.stroke and not s.fill and s.h >= 8 and s.w >= 20:   # outlined cell
                h.append((s.y0, s.x0, s.x1))
                h.append((s.y1, s.x0, s.x1))
                v.append((s.x0, s.y0, s.y1))
                v.append((s.x1, s.y0, s.y1))
    return h, v, bands


def _levels(h_edges):
    """Merge horizontal edges into y-levels; each level keeps merged runs and interior break points."""
    srt = sorted(h_edges, key=lambda e: -e[0])
    levels: list[dict] = []
    for y, x0, x1 in srt:
        if levels and abs(levels[-1]["y"] - y) <= YTOL:
            levels[-1]["segs"].append((x0, x1))
        else:
            levels.append({"y": y, "segs": [(x0, x1)]})
    for lv in levels:
        segs = sorted(lv["segs"])
        runs, breaks = [], []
        cur = list(segs[0])
        for a, b in segs[1:]:
            if a <= cur[1] + 2.5:
                if a > cur[0] + 1 and abs(a - cur[1]) <= 2.5 and cur[1] - cur[0] > 1:
                    breaks.append((a + cur[1]) / 2)          # two segments meet here: column boundary
                cur[1] = max(cur[1], b)
            else:
                runs.append(tuple(cur))
                cur = [a, b]
        runs.append(tuple(cur))
        lv["runs"], lv["breaks"] = runs, breaks
        lv["x0"], lv["x1"] = runs[0][0], runs[-1][1]
    return levels


def _overlap_ratio(a0, a1, b0, b1) -> float:
    """Overlap relative to the NARROWER extent: a partial-width rule inside the table still links."""
    inter = min(a1, b1) - max(a0, b0)
    narrow = min(a1 - a0, b1 - b0)
    return inter / narrow if narrow > 0 else 0.0


def _cluster(values: list[float], tol: float) -> list[float]:
    out: list[list[float]] = []
    for x in sorted(values):
        if out and x - out[-1][-1] <= tol:
            out[-1].append(x)
        else:
            out.append([x])
    return [statistics.mean(g) for g in out]


# --------------------------------------------------------------------------- detection

def detect_tables(page: PageData, body: float, blocked: list[tuple] | None = None) -> list[Table]:
    h, v, bands = _edges(page.shapes)
    if len(h) < 3:
        return []
    levels = _levels(h)

    # group consecutive levels into candidate stacks
    groups: list[list[dict]] = []
    for lv in levels:
        if groups:
            prev = groups[-1][-1]
            gap = prev["y"] - lv["y"]
            linked = gap <= 110 or any(
                min(prev["x1"], lv["x1"]) >= x >= max(prev["x0"], lv["x0"]) - 3 and y1 >= prev["y"] - 8 and y0 <= lv["y"] + 8
                for x, y0, y1 in v)
            gx0 = min(l["x0"] for l in groups[-1])
            gx1 = max(l["x1"] for l in groups[-1])
            if 0 < gap and linked and _overlap_ratio(gx0, gx1, lv["x0"], lv["x1"]) >= 0.6:
                groups[-1].append(lv)
                continue
        groups.append([lv])

    tables: list[Table] = []
    for g in groups:
        if len(g) < 3:
            continue
        for t in _tables_in_group(page, g, v, bands, body):
            if blocked and any(_overlaps(t.bbox, b) > 0.5 for b in blocked):
                continue
            tables.append(t)
    return tables


def _score(t: Table) -> float:
    """How table-like: share of rows with >= 2 filled cells, plus a bonus for a detected header."""
    rows = [[c for c in r if c.lines] for r in t.cells]
    multi = sum(1 for r in rows if len(r) >= 2) / max(1, len(rows))
    return multi + (0.5 if t.header_rows else 0.0)


def _tables_in_group(page: PageData, g: list[dict], v, bands, body: float, depth: int = 0) -> list[Table]:
    """The group as one table, or - when a shaded box / caption / second table is stacked against it -
    the most table-like sub-stack plus whatever can be found in the leftover above or below."""
    t = _table_from_group(page, g, v, bands, body)
    if t is not None:
        return [t]
    if len(g) <= 3 or depth >= 4:
        return []
    best = None                                        # (score, rows, kind, k, table)
    for k in range(1, len(g) - 2):
        for kind, sub in (("top", g[k:]), ("bottom", g[:len(g) - k])):
            t = _table_from_group(page, sub, v, bands, body)
            if t is not None:
                cand = (_score(t), t.rows, kind, k, t)
                if best is None or cand[:2] > best[:2]:
                    best = cand
    if best is None:
        return []
    _, _, kind, k, t = best
    if kind == "top":
        above = g[:k + 1]
        return (_tables_in_group(page, above, v, bands, body, depth + 1) if len(above) >= 3 else []) + [t]
    below = g[len(g) - k - 1:]
    return [t] + (_tables_in_group(page, below, v, bands, body, depth + 1) if len(below) >= 3 else [])


def _table_from_group(page: PageData, g: list[dict], v, bands, body: float) -> Table | None:
    gx0, gx1 = min(l["x0"] for l in g), max(l["x1"] for l in g)
    if gx1 - gx0 < 60:
        return None
    top, bottom = g[0]["y"], g[-1]["y"]
    ys = [l["y"] for l in g]

    # columns: run boundaries + vertical rules inside the stack
    cols = [gx0, gx1]
    brk = [b for l in g for b in l["breaks"]]
    cols += [x for x in _cluster(brk, XTOL) if gx0 + 8 < x < gx1 - 8 and sum(1 for b in brk if abs(b - x) <= XTOL) >= 2]
    vx = [x for x, y0, y1 in v if gx0 - 3 <= x <= gx1 + 3 and min(y1, top) - max(y0, bottom) > 0.5 * (top - bottom)]
    cols += [x for x in _cluster(vx, XTOL) if gx0 + 8 < x < gx1 - 8]
    col_edges = _cluster(cols, XTOL)
    col_edges[0], col_edges[-1] = gx0, gx1

    if not any(len(l["runs"]) == 1 and (l["x1"] - l["x0"]) >= 0.9 * (gx1 - gx0) for l in g):
        return None                                    # separate boxes, not one table
    inside = [l for l in page.lines if gx0 - 3 <= (l.x0 + l.x1) / 2 <= gx1 + 3 and bottom - 1 <= (l.y0 + l.y1) / 2 <= top + 1]
    if not inside:
        return None
    if len(col_edges) < 3:                             # no rule breaks: try text alignment
        col_edges = _text_columns(inside, gx0, gx1, ys, body)
        if len(col_edges) < 3:
            return None

    edges = [ys[0]]                                    # rows: merge slivers (< 5pt) into the row above
    for y in ys[1:]:
        if edges[-1] - y < 8:
            continue
        edges.append(y)
    if len(edges) < 3:
        return None
    return _build(page, inside, edges, col_edges, bands)


def _overlaps(a, b) -> float:
    ix = min(a[2], b[2]) - max(a[0], b[0])
    iy = min(a[3], b[3]) - max(a[1], b[1])
    if ix <= 0 or iy <= 0:
        return 0.0
    return ix * iy / max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))


def _text_columns(lines: list[Line], x0: float, x1: float, ys: list[float], body: float) -> list[float]:
    """Columns from where text starts, when the rules give no boundaries."""
    starts = _cluster([l.x0 for l in lines], 6.0)
    keep = [s for s in starts if sum(1 for l in lines if abs(l.x0 - s) <= 6.0) >= max(2, 0.3 * (len(ys) - 1))]
    if len(keep) < 2:
        return [x0, x1]
    edges = [x0]
    for a, b in zip(keep, keep[1:]):
        left_end = max((l.x1 for l in lines if abs(l.x0 - a) <= 6.0), default=a)
        edges.append(max(a + 1, min(b - 1, (left_end + b) / 2)))
    edges.append(x1)
    return edges


def _build(page: PageData, lines: list[Line], row_edges: list[float], col_edges: list[float], bands) -> Table | None:
    nr, nc = len(row_edges) - 1, len(col_edges) - 1
    cells = [[Cell(r, c) for c in range(nc)] for r in range(nr)]

    def row_of(l: Line):
        yc = (l.y0 + l.y1) / 2
        return next((i for i in range(nr) if row_edges[i] + 1 >= yc >= row_edges[i + 1] - 1), None)

    def col_at(x: float):
        return next((i for i in range(nc) if col_edges[i] - 1 <= x <= col_edges[i + 1] + 1), None)

    placed = [(l, row_of(l)) for l in lines]
    per_row: dict[int, int] = {}
    for _, r in placed:
        if r is not None:
            per_row[r] = per_row.get(r, 0) + 1
    interior = col_edges[1:-1]
    for l, r in placed:
        if r is None:
            continue
        crosses = any(l.x0 < e - 2 and l.x1 > e + 2 for e in interior)
        if crosses and per_row[r] == 1:                       # a lone line across columns: a spanning cell
            c = col_at(l.x0 + 1)
            if c is None:
                continue
            cells[r][c].lines.append(l)
            cells[r][c].colspan = nc - c
            for k in range(c + 1, nc):
                cells[r][k].covered = True
            continue
        c = col_at(l.x0 + 1 if crosses else (l.x0 + l.x1) / 2)
        if c is None:
            continue
        cells[r][c].lines.append(l)
    for row in cells:
        for cell in row:
            cell.lines.sort(key=lambda l: (-l.y1, l.x0))

    # a "column" made only of bullet/number markers is a list in a shaded box, not a table
    for c in range(nc):
        col_txt = [cells[r][c].text for r in range(nr) if cells[r][c].lines]
        if len(col_txt) >= 2 and sum(1 for t in col_txt if MARKER_ONLY.match(t)) >= 0.75 * len(col_txt):
            return None
    nonempty_rows = [r for r in cells if any(c.lines for c in r)]
    filled = sum(1 for r in cells for c in r if c.lines)
    if len(nonempty_rows) < 2 or filled < 4 or len(nonempty_rows) < 0.6 * nr:
        return None
    # more than a title row spanning the width means wide text lines in a box, not table rows
    spanning = sum(1 for r in cells for c in r if c.colspan > 1)
    if spanning > max(1, 0.2 * len(nonempty_rows)):
        return None
    # a real table has several filled cells per row; a shaded text box has one wide line per band
    multi = sum(1 for r in nonempty_rows if sum(1 for c in r if c.lines) >= 2)
    if multi < 0.4 * len(nonempty_rows):
        return None
    # text running across an interior column boundary (other than spanning cells) means not separate columns
    if interior and lines:
        cross_lines = [(l, r) for l, r in placed if r is not None and any(l.x0 < e - 2 and l.x1 > e + 2 for e in interior)]
        lone = [1 for l, r in cross_lines if per_row.get(r, 0) == 1]
        crossing = len(cross_lines) - min(1, len(lone))      # one spanning title row is fine
        if crossing > 0.25 * len(lines):
            return None
    for c in range(nc):                                  # every real column carries text in a fair share of rows
        used = sum(1 for r in range(nr) if cells[r][c].lines or cells[r][c].covered)
        if used < (1 if len(nonempty_rows) <= 3 else max(2, 0.25 * len(nonempty_rows))):
            return None
    # a lone spanning line at the top/bottom (section heading, source note) is not part of the table
    # unless it sits on a dark band (then it is the table's own title row)
    def dark_band(ri: int) -> bool:
        h = row_edges[ri] - row_edges[ri + 1]
        return any(b[4] is not None and b[4] < 0.6 and min(b[3], row_edges[ri]) - max(b[1], row_edges[ri + 1]) >= 0.7 * h
                   for b in bands)

    def lone_span(ri: int) -> bool:
        return sum(1 for c in cells[ri] if c.lines) == 1 and any(c.colspan > 1 and c.lines for c in cells[ri])

    while len(cells) > 2 and lone_span(0) and not dark_band(0):
        cells, row_edges = cells[1:], row_edges[1:]
    while len(cells) > 2 and lone_span(len(cells) - 1) and not dark_band(len(cells) - 1):
        cells, row_edges = cells[:-1], row_edges[:-1]
    # trim empty rows at both ends
    first = next(i for i, r in enumerate(cells) if any(c.lines for c in r))
    last = max(i for i, r in enumerate(cells) if any(c.lines for c in r))
    cells = cells[first:last + 1]
    row_edges = row_edges[first:last + 2]
    for i, row in enumerate(cells):
        for c in row:
            c.row = i

    t = Table(col_edges[0], row_edges[-1], col_edges[-1], row_edges, col_edges, cells) if False else         Table(col_edges[0], row_edges[-1], col_edges[-1], row_edges[0], row_edges, col_edges, cells)

    # ---- header rows: leading rows with a dark band or mostly bold text --------------------------
    def row_is_header(i: int) -> bool:
        h = row_edges[i] - row_edges[i + 1]

        def covers(b):
            return min(b[3], row_edges[i]) - max(b[1], row_edges[i + 1]) >= 0.7 * h

        band = [b for b in bands if covers(b)]
        if any(b[4] is not None and b[4] < 0.6 for b in band):
            return True
        ls = [l for c in cells[i] for l in c.lines]
        if ls and sum(1 for l in ls if l.bold) >= 0.6 * len(ls):
            return True
        if i == 0 and band and ls:                       # tinted first row over text that is not just numbers
            return not (sum(1 for c in cells[0] if c.lines and NUM.match(c.text)) >= 0.6 * sum(1 for c in cells[0] if c.lines))
        return False

    hdr = 0
    while hdr < min(3, len(cells) - 1) and row_is_header(hdr):
        hdr += 1
    t.header_rows = hdr
    if not hdr and len(cells) >= 3:
        txt = [c for c in cells[0] if c.lines]
        numeric_first = txt and sum(1 for c in txt if NUM.match(c.text)) >= 0.6 * len(txt)
        body_numeric = [NUM.match(c.text) is not None for row in cells[1:] for c in row if c.lines]
        if not numeric_first and body_numeric and sum(body_numeric) >= 0.4 * len(body_numeric):
            t.header_rows = 1
            t.note = "header row assumed (first row is text over numeric rows)"

    # ---- header column: text labels beside numeric data ---------------------------------------
    if t.cols >= 2 and len(cells) - t.header_rows >= 2:
        body_rows = cells[t.header_rows:]
        first_col = [r[0] for r in body_rows if r[0].lines]
        others = [c for r in body_rows for c in r[1:] if c.lines]
        label_like = first_col and sum(1 for c in first_col if not NUM.match(c.text)) >= 0.8 * len(first_col)
        data_numeric = others and sum(1 for c in others if NUM.match(c.text)) >= 0.6 * len(others)
        if label_like and data_numeric:
            t.header_cols = 1

    for r in cells[:t.header_rows]:
        for c in r:
            if not c.covered:
                c.header, c.scope = True, "Column"
    if t.header_cols:
        for r in cells[t.header_rows:]:
            if not r[0].covered:
                r[0].header, r[0].scope = True, "Row"
    return t
