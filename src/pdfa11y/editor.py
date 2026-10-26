"""Tag editor core (no GUI).

Reads the structure tree of a working PDF into per-page *blocks* with locations, and applies edits as a
replayable list of operations. Undo/redo = replay the list on a fresh copy of the base file, so the base
file is never modified and edits can be saved, reloaded and undone without limit.

Operations reference structure elements by their PDF object number in the BASE file, which is stable for
the life of the session (object numbers are only renumbered when a file is written out).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name

from .audit import _is_mcr_or_objr, _kids, _name
from .extract import extract_pages
from .figures import (_artifact_ranges, _detach, _mcid_op_ranges, _mcids_of, _null_parent_entries,
                      apply_decisions_to_pdf)
from .fix import _renumber_headings, _rows, _set_scope, _Tree

BLOCK_ROLES = {"P", "H", "H1", "H2", "H3", "H4", "H5", "H6", "LBody", "Lbl", "TD", "TH", "Caption", "Figure",
               "Formula", "TOCI", "BlockQuote", "Title", "Form", "Note"}
HEADINGS = {"H", "H1", "H2", "H3", "H4", "H5", "H6"}
TEXT_ROLES = {"P", "H1", "H2", "H3", "H4", "H5", "H6", "Caption", "BlockQuote"}   # roles a text block can be switched between


class EditError(Exception):
    """An edit that cannot be applied; the message is meant for the person using the app."""


@dataclass
class Block:
    ref: tuple[int, int]
    role: str
    page: int
    bbox: tuple[float, float, float, float] | None
    text: str
    order: int
    mcids: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class Node:
    ref: tuple[int, int]
    role: str
    text: str = ""
    is_block: bool = False
    children: list["Node"] = field(default_factory=list)


@dataclass
class Geom:
    width: float
    height: float
    left: float
    top: float
    mcid_box: dict[int, tuple[float, float, float, float]]
    mcid_text: dict[int, str]


# --------------------------------------------------------------------------- helpers

def _ref(el) -> tuple[int, int]:
    return tuple(el.objgen)                                   # type: ignore[return-value]


def _get(pdf: pikepdf.Pdf, ref) -> pikepdf.Object:
    try:
        obj = pdf.get_object((int(ref[0]), int(ref[1])))
    except Exception as e:  # noqa: BLE001
        raise EditError("That element no longer exists.") from e
    if not isinstance(obj, Dictionary):
        raise EditError("That element no longer exists.")
    return obj


def _parent(el):
    p = el.get("/P")
    if p is None:
        raise EditError("This element has no parent in the tag tree.")
    return p


def _index_in(parent, el) -> int:
    k = parent.get("/K")
    if isinstance(k, Array):
        for i, x in enumerate(k):
            if isinstance(x, Dictionary) and x.is_indirect and x.objgen == el.objgen:
                return i
    return -1


def _has_objr(pdf, el, depth: int = 0) -> bool:
    for k in _kids(el):
        if isinstance(k, Dictionary):
            if k.get("/Type") == Name.OBJR:
                return True
            if not _is_mcr_or_objr(k) and depth < 30 and _has_objr(pdf, k, depth + 1):
                return True
    return False


def _prune_empty_ancestors(parent) -> None:
    """After removing content, drop containers left with nothing in them (never the Document or root)."""
    while parent is not None:
        if parent.get("/K") is not None and len(_kids(parent)) > 0:
            return
        role = _name(parent.get("/S", ""))
        up = parent.get("/P")
        if role in ("Document", "", "TD", "TH") or up is None:      # an empty table cell must stay: rows need equal widths
            return
        _detach(up, parent)
        parent = up


# --------------------------------------------------------------------------- operations (all take an open pdf)

def op_set_role(pdf, ref, role: str) -> None:
    el = _get(pdf, ref)
    if role not in TEXT_ROLES:
        raise EditError(f"“{role}” is not a valid type for a text block.")
    tree = _Tree(pdf)
    if tree.role(el) not in TEXT_ROLES | {"Note", "TOCI", "LBody", "Title", "H"}:
        raise EditError("Only text blocks can be switched between paragraph, heading and caption.")
    el["/S"] = Name("/" + role)


def op_artifact(pdf, ref) -> None:
    el = _get(pdf, ref)
    tree = _Tree(pdf)
    if tree.role(el) == "Figure":
        raise EditError("For images use “Decorative” (in the Images tab or the block panel).")
    if tree.role(el) in ("TD", "TH"):
        raise EditError("A single table cell cannot be hidden: every row needs the same number of columns. "
                        "Hide the whole row or table instead, or hide the text inside the cell.")
    if _has_objr(pdf, el):
        raise EditError("This block contains a link. Remove the link first, or keep the block: "
                        "hiding it would leave the link untagged.")
    mcids = _mcids_of(pdf, el)
    parent = _parent(el)
    _artifact_ranges(pdf, mcids)
    _null_parent_entries(pdf, mcids)
    _detach(parent, el)
    _prune_empty_ancestors(parent)


def op_move(pdf, ref, delta: int) -> None:
    el = _get(pdf, ref)
    parent = _parent(el)
    k = parent.get("/K")
    i = _index_in(parent, el)
    if i < 0:
        raise EditError("This block has no siblings to move past.")
    j = i + delta
    if not 0 <= j < len(k):
        raise EditError("It is already at the " + ("top." if delta < 0 else "bottom."))
    a, b = k[i], k[j]
    k[i], k[j] = b, a


def op_merge_next(pdf, ref) -> None:
    el = _get(pdf, ref)
    tree = _Tree(pdf)
    if tree.role(el) not in TEXT_ROLES | {"LBody"}:
        raise EditError("Only text blocks can be merged.")
    parent = _parent(el)
    k = parent.get("/K")
    i = _index_in(parent, el)
    if i < 0 or i + 1 >= len(k):
        raise EditError("There is no block after this one in the same group.")
    nxt = k[i + 1]
    if not isinstance(nxt, Dictionary) or tree.role(nxt) not in TEXT_ROLES | {"LBody"}:
        raise EditError("The next block is not a text block.")
    own = el.get("/K")
    new_k = Array(list(own)) if isinstance(own, Array) else Array([own] if own is not None else [])
    el_pg = el.get("/Pg")
    nxt_pg = nxt.get("/Pg")
    moved_mcids: list[tuple[int, int]] = []
    pages = {p.obj.objgen: i2 for i2, p in enumerate(pdf.pages)}
    for kid in _kids(nxt):
        if isinstance(kid, int):
            if nxt_pg is not None and (el_pg is None or el_pg.objgen != nxt_pg.objgen):
                kid = Dictionary(Type=Name.MCR, Pg=nxt_pg, MCID=kid)
            elif nxt_pg is not None:
                pass
            if nxt_pg is not None and nxt_pg.objgen in pages:
                moved_mcids.append((pages[nxt_pg.objgen], int(kid if isinstance(kid, int) else kid.MCID)))
        elif isinstance(kid, Dictionary):
            if kid.get("/Type") == Name.MCR:
                if "/Pg" not in kid and nxt_pg is not None:
                    kid["/Pg"] = nxt_pg
                pg = kid.get("/Pg")
                if pg is not None and pg.objgen in pages:
                    moved_mcids.append((pages[pg.objgen], int(kid.MCID)))
            elif kid.get("/Type") != Name.OBJR:
                kid["/P"] = el
        new_k.append(kid)
    if el_pg is None and nxt_pg is not None:
        el["/Pg"] = nxt_pg
    el["/K"] = new_k
    # marked content that used to belong to `nxt` now belongs to `el`
    pt = pdf.Root.StructTreeRoot.get("/ParentTree")
    if pt is not None:
        nt = pikepdf.NumberTree(pt)
        for pi, mcid in moved_mcids:
            key = pdf.pages[pi].obj.get("/StructParents")
            if key is None:
                continue
            arr = nt[int(key)]
            if isinstance(arr, Array) and 0 <= mcid < len(arr):
                arr[mcid] = el
    del k[i + 1]
    if len(k) == 0:
        del parent["/K"]


def _remove_scope(el) -> None:
    a = el.get("/A")
    items = list(a) if isinstance(a, Array) else ([a] if a is not None else [])
    for it in items:
        if isinstance(it, Dictionary) and "/Scope" in it:
            del it["/Scope"]


def op_cell(pdf, ref, kind: str) -> None:
    el = _get(pdf, ref)
    if _Tree(pdf).role(el) not in ("TD", "TH"):
        raise EditError("Select a table cell first.")
    if kind == "td":
        el["/S"] = Name.TD
        _remove_scope(el)
    elif kind in ("th_col", "th_row"):
        el["/S"] = Name.TH
        _set_scope(el, "Column" if kind == "th_col" else "Row")
    else:
        raise EditError("Unknown cell type.")


def op_table_headers(pdf, ref, header_row: bool, header_col: bool) -> None:
    el = _get(pdf, ref)
    tree = _Tree(pdf)
    if tree.role(el) != "Table":
        raise EditError("Select a table first.")
    rows = _rows(tree, el)
    if not rows:
        raise EditError("This table has no rows.")

    def cells(tr):
        return [c for c in tree.children(tr) if tree.role(c) in ("TD", "TH")]

    for ri, (tr, _) in enumerate(rows):
        for ci, c in enumerate(cells(tr)):
            if ri == 0:
                if header_row:
                    c["/S"] = Name.TH
                    _set_scope(c, "Column")
                elif tree.role(c) == "TH":
                    c["/S"] = Name.TD
                    _remove_scope(c)
            elif ci == 0:
                if header_col:
                    c["/S"] = Name.TH
                    _set_scope(c, "Row")
                elif tree.role(c) == "TH":
                    c["/S"] = Name.TD
                    _remove_scope(c)


def op_renumber_headings(pdf) -> None:
    if not _renumber_headings(_Tree(pdf), force=True):
        raise EditError("Heading levels are already consistent.")


OPS = {"set_role": op_set_role, "artifact": op_artifact, "move": op_move, "merge_next": op_merge_next,
       "cell": op_cell, "table_headers": op_table_headers, "renumber_headings": op_renumber_headings}


# --------------------------------------------------------------------------- the editor

class Editor:
    def __init__(self, base: str | Path):
        self.base = Path(base)
        self.ops: list[dict] = []
        self.redo_stack: list[dict] = []
        self._pristine = pikepdf.open(self.base)                 # never mutated: source of geometry
        self._page_of = {p.obj.objgen: i for i, p in enumerate(self._pristine.pages)}
        self._geom: dict[int, Geom] = {}
        self.pdf: pikepdf.Pdf | None = None
        self.warnings: list[str] = []
        self._replay()

    # ------------------------------------------------------------------ lifecycle
    def close(self) -> None:
        for p in (self.pdf, self._pristine):
            try:
                if p is not None:
                    p.close()
            except Exception:  # noqa: BLE001
                pass
        self.pdf = None

    def _fresh(self) -> pikepdf.Pdf:
        pdf = pikepdf.open(self.base)
        self.warnings = []
        for op in self.ops:
            try:
                OPS[op["op"]](pdf, *op.get("args", []))
            except EditError as e:
                self.warnings.append(str(e))
        return pdf

    def _replay(self) -> None:
        if self.pdf is not None:
            self.pdf.close()
        self.pdf = self._fresh()

    # ------------------------------------------------------------------ history
    @property
    def can_undo(self) -> bool:
        return bool(self.ops)

    @property
    def can_redo(self) -> bool:
        return bool(self.redo_stack)

    def do(self, name: str, *args) -> None:
        """Apply one edit. Raises EditError (and changes nothing) if it cannot be done."""
        OPS[name](self.pdf, *args)                           # fail fast on the live copy...
        self.ops.append({"op": name, "args": list(args)})
        self.redo_stack.clear()
        # ...and rebuild from scratch only if the live copy could have been half-modified
        # (ops raise before mutating, so the live copy stays consistent)

    def undo(self) -> bool:
        if not self.ops:
            return False
        self.redo_stack.append(self.ops.pop())
        self._replay()
        return True

    def redo(self) -> bool:
        if not self.redo_stack:
            return False
        op = self.redo_stack.pop()
        self.ops.append(op)
        self._replay()
        return True

    def load_ops(self, ops: list[dict]) -> None:
        self.ops = [o for o in ops if o.get("op") in OPS]
        self.redo_stack = []
        self._replay()

    # ------------------------------------------------------------------ geometry (from the pristine file)
    def geometry(self, pi: int) -> Geom:
        if pi in self._geom:
            return self._geom[pi]
        ranges = _mcid_op_ranges(self._pristine.pages[pi])
        pd_ = next(iter(extract_pages(self.base, self._pristine, only={pi})))
        chars_by_op = defaultdict(list)
        for l in pd_.lines:
            for c in l.chars:
                chars_by_op[c.op].append(c)
        for f in pd_.figures:
            for c in f.chars:
                chars_by_op[c.op].append(c)
        fig_by_op = {f.op: f for f in pd_.figures}
        box: dict[int, tuple] = {}
        text: dict[int, str] = {}
        for mcid, (a, b) in ranges.items():
            xs0, ys0, xs1, ys1, parts = [], [], [], [], []
            for op in range(a, b + 1):
                for c in chars_by_op.get(op, ()):
                    xs0.append(c.x0); ys0.append(c.y0); xs1.append(c.x1); ys1.append(c.y1); parts.append(c.text)
                f = fig_by_op.get(op)
                if f is not None:
                    xs0.append(f.x0); ys0.append(f.y0); xs1.append(f.x1); ys1.append(f.y1)
            if xs0:
                box[mcid] = (min(xs0), min(ys0), max(xs1), max(ys1))
                text[mcid] = re.sub(r"\s+", " ", "".join(parts)).strip()
        mb = self._pristine.pages[pi].obj.MediaBox
        left, bottom, right, top = (float(v) for v in mb)
        g = Geom(right - left, top - bottom, left, top, box, text)
        self._geom[pi] = g
        return g

    # ------------------------------------------------------------------ reading the tree
    def _own_mcids(self, el, pi: int) -> list[int]:
        out = []
        own_pg = el.get("/Pg")
        own_page = self._page_of.get(own_pg.objgen) if own_pg is not None else None
        for k in _kids(el):
            if isinstance(k, int):
                if own_page == pi:
                    out.append(k)
            elif isinstance(k, Dictionary) and k.get("/Type") == Name.MCR and "/MCID" in k:
                pg = k.get("/Pg")
                page = self._page_of.get(pg.objgen) if pg is not None else own_page
                if page == pi:
                    out.append(int(k.MCID))
        return out

    def page_blocks(self, pi: int) -> list[Block]:
        """Content blocks on a page in reading (tag tree) order."""
        pdf = self.pdf
        tree = _Tree(pdf)
        g = self.geometry(pi)
        owners: dict[tuple, list[int]] = {}
        order: list[tuple] = []
        roles: dict[tuple, str] = {}
        stack = [(k, []) for k in reversed(tree.children(tree.st))]
        seen = set()
        while stack:
            el, anc = stack.pop()
            if el.is_indirect:
                if el.objgen in seen:
                    continue
                seen.add(el.objgen)
            role = tree.role(el)
            mcs = self._own_mcids(el, pi)
            if mcs:
                owner_el, owner_role = el, role
                if role not in BLOCK_ROLES:
                    for a_el, a_role in reversed(anc):
                        if a_role in BLOCK_ROLES:
                            owner_el, owner_role = a_el, a_role
                            break
                key = _ref(owner_el)
                if key not in owners:
                    owners[key] = []
                    order.append(key)
                    roles[key] = owner_role
                owners[key].extend(mcs)
            for kid in reversed(tree.children(el)):
                stack.append((kid, anc + [(el, role)]))
        blocks = []
        for n, key in enumerate(order):
            mcs = owners[key]
            bx = [g.mcid_box[m] for m in mcs if m in g.mcid_box]
            bbox = (min(b[0] for b in bx), min(b[1] for b in bx), max(b[2] for b in bx), max(b[3] for b in bx)) if bx else None
            txt = " ".join(g.mcid_text.get(m, "") for m in mcs).strip()
            blocks.append(Block(key, roles[key], pi, bbox, txt, n, [(pi, m) for m in mcs]))
        return blocks

    def page_tree(self, pi: int) -> list[Node]:
        """The slice of the structure tree that has content on this page, containers included."""
        tree = _Tree(self.pdf)
        blocks = {b.ref: b for b in self.page_blocks(pi)}
        memo: dict = {}

        def has(el) -> bool:
            key = _ref(el) if el.is_indirect else id(el)
            if key in memo:
                return memo[key]
            memo[key] = False                                   # cycle guard
            r = bool(self._own_mcids(el, pi)) or any(has(k) for k in tree.children(el))
            memo[key] = r
            return r

        def build(el, depth=0) -> Node | None:
            if depth > 40 or not has(el):
                return None
            role = tree.role(el)
            ref = _ref(el) if el.is_indirect else (-1, 0)
            b = blocks.get(ref)
            node = Node(ref, role, (b.text[:70] if b else ""), b is not None)
            for k in tree.children(el):
                c = build(k, depth + 1)
                if c:
                    node.children.append(c)
            return node

        return [n for n in (build(k) for k in tree.children(tree.st)) if n]

    def role_of(self, ref) -> str:
        return _Tree(self.pdf).role(_get(self.pdf, ref))

    def table_state(self, ref) -> tuple[bool, bool]:
        """(first row is header, first column is header) for a Table element."""
        tree = _Tree(self.pdf)
        el = _get(self.pdf, ref)
        rows = _rows(tree, el)
        cells = [[c for c in tree.children(tr) if tree.role(c) in ("TD", "TH")] for tr, _ in rows]
        row_hdr = bool(cells) and bool(cells[0]) and all(tree.role(c) == "TH" for c in cells[0])
        body = [r[0] for r in cells[1:] if r]
        col_hdr = bool(body) and all(tree.role(c) == "TH" for c in body)
        return row_hdr, col_hdr

    def describe(self, ref) -> dict:
        """What a person can do with this element: its type, cell kind / table header state / figure key,
        and whether it can move up, move down or merge with the next block."""
        pdf = self.pdf
        el = _get(pdf, ref)
        tree = _Tree(pdf)
        role = tree.role(el)
        d: dict = {"role": role}
        if role in ("TD", "TH"):
            kind = "td"
            if role == "TH":
                kind = "th_col"
                a = el.get("/A")
                items = list(a) if isinstance(a, Array) else ([a] if a is not None else [])
                for x in items:
                    if isinstance(x, Dictionary) and "/Scope" in x and str(x.Scope) == "/Row":
                        kind = "th_row"
            d["cell"] = kind
        if role == "Table":
            row_hdr, col_hdr = self.table_state(ref)
            d["table"] = {"header_row": row_hdr, "header_col": col_hdr}
        if role == "Figure":
            d["figure_key"] = self.figure_key(ref)
        parent = el.get("/P")
        k = parent.get("/K") if parent is not None else None
        i = _index_in(parent, el) if parent is not None else -1
        d["can_up"] = i > 0
        d["can_down"] = i >= 0 and isinstance(k, Array) and i < len(k) - 1
        nxt = k[i + 1] if i >= 0 and isinstance(k, Array) and i + 1 < len(k) else None
        textual = TEXT_ROLES | {"LBody"}
        d["can_merge"] = role in textual and isinstance(nxt, Dictionary) and tree.role(nxt) in textual
        d["is_text"] = role in TEXT_ROLES or role == "H"
        return d

    def figure_key(self, ref) -> str:
        return f"f{ref[0]}"

    # ------------------------------------------------------------------ writing
    def save(self, dst: str | Path, decisions: dict | None = None, title: str | None = None,
             lang: str | None = None) -> dict:
        """Write base + edits + alt-text decisions to `dst` (never touches the base file)."""
        dst = Path(dst)
        if dst.resolve() == self.base.resolve():
            raise ValueError("refusing to overwrite the working copy")
        pdf = self._fresh()
        applied = apply_decisions_to_pdf(pdf, decisions or {})
        if title:
            try:
                with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
                    meta["dc:title"] = title
            except Exception:  # noqa: BLE001
                pass
            pdf.docinfo["/Title"] = pikepdf.String(title)
        if lang:
            pdf.Root["/Lang"] = pikepdf.String(lang)
        pdf.save(dst)
        pdf.close()
        applied["edits"] = len(self.ops)
        return applied
