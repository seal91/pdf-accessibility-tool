"""Auto-tagger: turns an untagged PDF into a tagged one.

For every page we rewrite the content stream, wrapping each show/paint operator
in marked content (an MCID owned by a structure element, or /Artifact), and we
build the structure tree (Document > H1..H6 / P / L / LI / TOC / Figure / Link),
the ParentTree, and the link-annotation wiring PDF/UA requires.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name, Operator, ContentStreamInstruction as CSI

from .extract import PageData, extract_pages
from .layout import Block, DocStats, PageLayout, analyse_document

SHOW = {"Tj", "TJ", "'", '"'}
CONSTRUCT = {"m", "l", "c", "v", "y", "re", "h", "W", "W*"}
PAINT = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"}
BREAK = {"q", "Q", "BT", "ET", "BX", "EX", "BI", "ID", "EI", "INLINE IMAGE", "Do", "sh",
         "BMC", "BDC", "EMC", "MP", "DP"}


# --------------------------------------------------------------------------- python-side tree

@dataclass
class SE:
    role: str
    kids: list = field(default_factory=list)      # SE | ('mc', mcid) | ('objr', annot, page_index)
    page: int | None = None
    alt: str | None = None
    attrs: dict | None = None                     # table attributes: Scope, ColSpan
    obj: pikepdf.Object | None = None


def _emc() -> CSI:
    return CSI([], Operator("EMC"))


def _bdc(tag: str, mcid: int) -> CSI:
    return CSI([Name("/" + tag), Dictionary(MCID=mcid)], Operator("BDC"))


def _artifact(subtype: str) -> CSI:
    if subtype in ("Header", "Footer"):
        return CSI([Name.Artifact, Dictionary(Type=Name.Pagination, Subtype=Name("/" + subtype))], Operator("BDC"))
    return CSI([Name.Artifact], Operator("BMC"))


# --------------------------------------------------------------------------- links

def _rect(annot) -> tuple[float, float, float, float] | None:
    try:
        r = [float(x) for x in annot.Rect]
        return min(r[0], r[2]), min(r[1], r[3]), max(r[0], r[2]), max(r[1], r[3])
    except Exception:  # noqa: BLE001
        return None


def _link_ops(page: PageData, layout: PageLayout, rect) -> dict[int, int]:
    """{op: chars inside rect} for ops that are (mostly) covered by the link rectangle."""
    x0, y0, x1, y1 = rect
    inside: dict[int, int] = {}
    total: dict[int, int] = {}
    chars = [c for l in page.lines for c in l.chars] + [c for f in page.figures for c in f.chars]
    for c in chars:
        total[c.op] = total.get(c.op, 0) + 1
        cx, cy = (c.x0 + c.x1) / 2, (c.y0 + c.y1) / 2
        if x0 - 1 <= cx <= x1 + 1 and y0 - 1 <= cy <= y1 + 1:
            inside[c.op] = inside.get(c.op, 0) + 1
    return {op: n for op, n in inside.items() if n >= 0.5 * total[op] and layout.owners.get(op, ("",))[0] == "blk"}


# --------------------------------------------------------------------------- per-page rewrite

def _rewrite_page(page: PageData, layout: PageLayout, link_of_op: dict[int, int], new_elem, bid_elem):
    """Return (new_ops, mcid_parents) for one page.

    link_of_op: op -> link index (into per-page link list) for ops that belong to a link.
    new_elem(kind, bid, link_idx) -> SE that should receive MCIDs for that run.
    """
    out: list = []
    mcid_parent: list[SE] = []
    open_run = False
    in_path = False

    def close():
        nonlocal open_run
        if open_run:
            out.append(_emc())
            open_run = False

    run_key = None
    for i, ins in enumerate(page.ops):
        op = str(ins.operator)

        # ---- vector paths -> artifact ----------------------------------------
        if op in CONSTRUCT or op in PAINT:
            if not in_path:
                close()
                run_key = None
                out.append(_artifact(""))
                in_path = True
            out.append(ins)
            if op in PAINT:
                out.append(_emc())
                in_path = False
            continue

        owner = layout.owners.get(i)
        if op in SHOW:
            if owner is None or owner[0] == "art":
                key = ("art", owner[1] if owner else "")
            else:
                key = ("blk", owner[1], link_of_op.get(i))
            if not (open_run and run_key == key):
                close()
                if key[0] == "art":
                    out.append(_artifact(key[1]))
                else:
                    se = new_elem(key[1], key[2])
                    mcid = len(mcid_parent)
                    mcid_parent.append(se)
                    se.kids.append(("mc", mcid))
                    out.append(_bdc(se.role if se.role != "LBody" else "P", mcid))
                open_run = True
                run_key = key
            out.append(ins)
            continue

        if op in ("Do", "INLINE IMAGE", "sh"):
            close()
            run_key = None
            if op == "sh" or owner is None or owner[0] == "art":
                out.append(_artifact(owner[1] if owner and owner[0] == "art" else ""))
                out.append(ins)
                out.append(_emc())
            else:
                se = new_elem(owner[1], None)
                mcid = len(mcid_parent)
                mcid_parent.append(se)
                se.kids.append(("mc", mcid))
                out.append(_bdc(se.role if se.role != "LBody" else "P", mcid))
                out.append(ins)
                out.append(_emc())
            continue

        if op in BREAK:
            close()
            run_key = None
        out.append(ins)

    close()
    if in_path:                                    # malformed stream: never leave a sequence open
        out.append(_emc())
    return out, mcid_parent


# --------------------------------------------------------------------------- orchestration

def _normalise_levels(layouts: list[PageLayout]) -> None:
    """Consistent, gap-free heading levels.

    A heading's level is 1 + the number of higher-ranking heading styles currently open, so the
    same style under the same parent always gets the same level and levels never skip.
    """
    stack: list[int] = []
    for pl in layouts:
        for b in pl.blocks:
            if b.kind == "H":
                raw = b.level or 1
                while stack and stack[-1] >= raw:
                    stack.pop()
                stack.append(raw)
                b.level = len(stack)


def autotag(src: str | Path, dst: str | Path) -> dict:
    src, dst = Path(src), Path(dst)
    with pikepdf.open(src) as pdf:
        if pdf.is_encrypted:
            raise RuntimeError("file is encrypted; remove the password first")
        if "/StructTreeRoot" in pdf.Root:
            raise RuntimeError("file is already tagged; use `fix` instead")

        pages = list(extract_pages(src, pdf))
        bad = [p for p in pages if not p.aligned]
        if bad:
            raise RuntimeError(f"cannot tag safely: {len(bad)} page(s) failed operator alignment "
                               f"(first: page {bad[0].index + 1}: {bad[0].align_note})")
        layouts, stats = analyse_document(pages)
        _normalise_levels(layouts)

        report: dict = {"file": src.name, "pages": len(pages), "body_size": stats.body_size,
                        "heading_sizes": stats.heading_sizes, "counts": {}, "figures_needing_alt": [],
                        "review": [], "links": {"total": 0, "with_text": 0, "unattached": 0}}
        counts = report["counts"]

        document = SE("Document")
        parent_tree: dict[int, list[SE]] = {}
        annot_parents: list[tuple[object, SE]] = []     # (annot object, link SE)
        headings: list[tuple[int, str, int, float]] = []

        for pd_, pl in zip(pages, layouts):
            pk_page = pdf.pages[pd_.index]
            blocks = [b for b in pl.blocks if b.kind != "Artifact"]

            # --- one SE per block; group consecutive list items / TOC rows ------------
            sink: dict[int, SE] = {}
            top: list[SE] = []
            cur_group: SE | None = None
            for b in blocks:
                counts[b.kind] = counts.get(b.kind, 0) + 1
                if b.kind == "H":
                    se = SE(f"H{min(b.level, 6)}", page=pd_.index)
                    sink[id(b)] = se
                    headings.append((min(b.level, 6), b.text, pd_.index, b.bbox[3]))
                    outer = se
                elif b.kind == "LI":
                    li = SE("LI")
                    body = SE("LBody", page=pd_.index)
                    li.kids.append(body)
                    sink[id(b)] = body
                    outer = li
                elif b.kind == "TOCI":
                    se = SE("TOCI", page=pd_.index)
                    sink[id(b)] = se
                    outer = se
                elif b.kind == "Table":
                    t = b.table
                    tse = SE("Table")
                    for r in t.cells:
                        tr = SE("TR")
                        for c in r:
                            if c.covered:
                                continue
                            cse = SE("TH" if c.header else "TD", page=pd_.index)
                            attrs = {}
                            if c.header and c.scope:
                                attrs["Scope"] = c.scope
                            if c.colspan > 1:
                                attrs["ColSpan"] = c.colspan
                            cse.attrs = attrs or None
                            if c.block is not None:
                                sink[id(c.block)] = cse
                            tr.kids.append(cse)
                        tse.kids.append(tr)
                    report.setdefault("tables", []).append(
                        {"page": pd_.index + 1, "rows": t.rows, "cols": t.cols,
                         "header_rows": t.header_rows, "header_cols": t.header_cols})
                    report["review"].append(
                        f"page {pd_.index + 1}: table detected ({t.rows} rows x {t.cols} columns, "
                        f"{t.header_rows} header row(s)): check it is a real table and its headers are right")
                    outer = tse
                elif b.kind == "Figure":
                    se = SE("Figure", page=pd_.index)
                    sink[id(b)] = se
                    outer = se
                    report["figures_needing_alt"].append(
                        {"page": pd_.index + 1, "bbox": [round(v, 1) for v in b.bbox]})
                else:
                    se = SE("P", page=pd_.index)
                    sink[id(b)] = se
                    outer = se
                group_role = {"LI": "L", "TOCI": "TOC"}.get(b.kind)
                if group_role:
                    if cur_group is None or cur_group.role != group_role:
                        cur_group = SE(group_role)
                        top.append(cur_group)
                    cur_group.kids.append(outer)
                else:
                    cur_group = None
                    top.append(outer)
                if b.note:
                    report["review"].append(f"page {pd_.index + 1}: {b.note}")
                if b.kind == "Artifact" and b.note:
                    report["review"].append(f"page {pd_.index + 1}: {b.note}")
            for b in pl.blocks:
                if b.kind == "Artifact" and b.note:
                    report["review"].append(f"page {pd_.index + 1}: {b.note}")
            document.kids.extend(top)

            # --- links ---------------------------------------------------------------
            link_of_op: dict[int, int] = {}
            link_defs: list[dict] = []
            annots = pk_page.obj.get("/Annots")
            for a in (annots or []):
                if a.get("/Subtype") != Name.Link:
                    continue
                rect = _rect(a)
                if rect is None:
                    continue
                report["links"]["total"] += 1
                ops = _link_ops(pd_, pl, rect)
                idx = len(link_defs)
                owner_blk = None
                if ops:
                    votes: dict[int, int] = {}
                    for op, n in ops.items():
                        bid = pl.owners[op][1]
                        votes[bid] = votes.get(bid, 0) + n
                    owner_blk = max(votes, key=votes.get)
                    for op in ops:
                        if pl.owners[op][1] == owner_blk:
                            link_of_op[op] = idx
                    report["links"]["with_text"] += 1
                link_defs.append({"annot": a, "blk": owner_blk, "rect": rect, "se": None})

            link_bid_used: dict[int, SE] = {}

            def new_elem(bid: int, link_idx: int | None, _sink=sink, _defs=link_defs, _page=pd_.index):
                parent = _sink.get(bid)
                if parent is None:
                    raise KeyError(bid)
                if link_idx is None:
                    return parent
                d = _defs[link_idx]
                if d["se"] is None:
                    d["se"] = SE("Link", page=_page)
                    parent.kids.append(d["se"])
                    d["se"].kids.append(("objr", d["annot"], _page))
                return d["se"]

            new_ops, mcid_parent = _rewrite_page(pd_, pl, link_of_op, new_elem, sink)

            # links that matched no text: attach an (empty) Link element to the nearest block
            for d in link_defs:
                if d["se"] is not None:
                    continue
                x0, y0, x1, y1 = d["rect"]
                best, best_d = None, 1e18
                for b in blocks:
                    bx0, by0, bx1, by1 = b.bbox
                    dist = ((bx0 + bx1) / 2 - (x0 + x1) / 2) ** 2 + ((by0 + by1) / 2 - (y0 + y1) / 2) ** 2
                    if dist < best_d:
                        best, best_d = b, dist
                se = SE("Link", page=pd_.index)
                se.kids.append(("objr", d["annot"], pd_.index))
                uri = None
                try:
                    uri = str(d["annot"].A.URI)
                except Exception:  # noqa: BLE001
                    pass
                se.alt = uri or "Link"
                if best is not None:
                    sink[id(best)].kids.append(se)
                else:
                    document.kids.append(se)
                    report["links"]["unattached"] += 1
                d["se"] = se

            for d in link_defs:
                annot_parents.append((d["annot"], d["se"]))
                if d["se"].alt is None and "/Contents" not in d["annot"]:
                    # give the annotation a description from its visible text
                    txt = " ".join(
                        c.text for l in pd_.lines for c in l.chars
                        if d["rect"][0] - 1 <= (c.x0 + c.x1) / 2 <= d["rect"][2] + 1
                        and d["rect"][1] - 1 <= (c.y0 + c.y1) / 2 <= d["rect"][3] + 1)
                    txt = re.sub(r"\s+", " ", txt).strip()
                    if txt:
                        d["annot"]["/Contents"] = pikepdf.String(txt[:200])

            pk_page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(new_ops))
            pk_page.obj["/StructParents"] = pd_.index
            parent_tree[pd_.index] = mcid_parent

        # ---------------------------------------------------------------- serialise
        root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))

        def build(se: SE, parent_obj) -> pikepdf.Object:
            d = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/" + se.role), P=parent_obj))
            se.obj = d
            if se.page is not None:
                d["/Pg"] = pdf.pages[se.page].obj
            if se.alt:
                d["/Alt"] = pikepdf.String(se.alt)
            if se.attrs:
                a = Dictionary(O=Name.Table)
                if "Scope" in se.attrs:
                    a["/Scope"] = Name("/" + se.attrs["Scope"])
                if "ColSpan" in se.attrs:
                    a["/ColSpan"] = se.attrs["ColSpan"]
                d["/A"] = a
            k = Array()
            for kid in se.kids:
                if isinstance(kid, SE):
                    k.append(build(kid, d))
                elif kid[0] == "mc":
                    k.append(kid[1])
                else:
                    k.append(Dictionary(Type=Name.OBJR, Obj=kid[1], Pg=pdf.pages[kid[2]].obj))
            if len(k):
                d["/K"] = k
            return d

        doc_obj = build(document, root)
        root["/K"] = doc_obj

        nt = pikepdf.NumberTree.new(pdf)
        for pg_index, arr in parent_tree.items():
            nt[pg_index] = Array([s.obj for s in arr])
        key = len(pages)
        for annot, se in annot_parents:
            annot["/StructParent"] = key
            nt[key] = se.obj
            key += 1
        root["/ParentTree"] = nt.obj
        root["/ParentTreeNextKey"] = key
        pdf.Root["/StructTreeRoot"] = root
        pdf.Root["/MarkInfo"] = Dictionary(Marked=True)

        # ---------------------------------------------------------------- bookmarks
        outlines = pdf.Root.get("/Outlines")
        if outlines is not None and outlines.get("/First") is None:
            del pdf.Root["/Outlines"]                      # empty placeholder
        if "/Outlines" not in pdf.Root and len(pages) >= 4 and len(headings) >= 2:
            with pdf.open_outline() as outline:
                stack: list[tuple[int, list]] = [(0, outline.root)]
                for level, title, pg, top_y in headings:
                    item = pikepdf.OutlineItem(title[:200], pg, pikepdf.PageLocation.XYZ, top=top_y + 10)
                    while stack[-1][0] >= level:
                        stack.pop()
                    stack[-1][1].append(item)
                    stack.append((level, item.children))
            report["bookmarks"] = len(headings)

        pdf.save(dst)

    report["headings"] = [{"level": l, "text": t[:80], "page": p + 1} for l, t, p, _ in headings]
    report["output"] = str(dst)
    return report
