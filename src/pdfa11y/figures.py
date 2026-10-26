"""Figures: find them, locate them on the page, apply alt text / "decorative" decisions.

Everything here works on a tagged PDF and knows nothing about the GUI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name

from .audit import _is_mcr_or_objr, _kids, _name
from .extract import extract_pages


@dataclass
class FigureItem:
    index: int                                   # position in document order (stable for a given file)
    page: int                                    # 0-based
    bbox: tuple[float, float, float, float] | None   # PDF points, origin bottom-left
    alt: str = ""                                # existing alt text, if any
    mcids: list[tuple[int, int]] = field(default_factory=list)   # (page index, MCID)
    near_text: str = ""
    obj: int | None = None                       # PDF object number of the Figure element

    @property
    def key(self) -> str:
        """Key used to store decisions. The object number does not change when blocks are reordered."""
        return f"f{self.obj}" if self.obj is not None else f"p{self.page + 1}#{self.index}"


# --------------------------------------------------------------------------- struct tree walk

def _role_map(st) -> dict[str, str]:
    rm = st.get("/RoleMap")
    return {_name(k): _name(v) for k, v in rm.items()} if isinstance(rm, Dictionary) else {}


def _resolve(role: str, rm: dict[str, str]) -> str:
    seen = set()
    while role in rm and role not in seen and role not in ("Figure",):
        seen.add(role)
        role = rm[role]
    return role


def _figure_elements(pdf: pikepdf.Pdf):
    """(element, parent) for every Figure in document order."""
    st = pdf.Root.get("/StructTreeRoot")
    if st is None:
        return []
    rm = _role_map(st)
    out, seen = [], set()
    stack = [(st, k) for k in reversed([k for k in _kids(st) if isinstance(k, Dictionary)])]
    while stack:
        parent, el = stack.pop()
        if _is_mcr_or_objr(el):
            continue
        if el.is_indirect:
            if el.objgen in seen:
                continue
            seen.add(el.objgen)
        if _resolve(_name(el.get("/S", "?")), rm) == "Figure":
            out.append((el, parent))
        stack.extend((el, k) for k in reversed([k for k in _kids(el) if isinstance(k, Dictionary)]))
    return out


def _page_index(pdf: pikepdf.Pdf, pg) -> int | None:
    if pg is None:
        return None
    for i, p in enumerate(pdf.pages):
        if p.obj.objgen == pg.objgen:
            return i
    return None


def _mcids_of(pdf: pikepdf.Pdf, el) -> list[tuple[int, int]]:
    """(page index, MCID) for the marked content directly inside this element (and its descendants)."""
    out: list[tuple[int, int]] = []
    own_page = _page_index(pdf, el.get("/Pg"))
    for k in _kids(el):
        if isinstance(k, int):
            if own_page is not None:
                out.append((own_page, k))
        elif isinstance(k, Dictionary):
            if k.get("/Type") == Name.MCR:
                pi = _page_index(pdf, k.get("/Pg")) if "/Pg" in k else own_page
                if pi is not None and "/MCID" in k:
                    out.append((pi, int(k.MCID)))
            elif k.get("/Type") == Name.OBJR:
                continue
            else:
                out.extend(_mcids_of(pdf, k))
    return out


def _mcid_op_ranges(page: pikepdf.Page) -> dict[int, tuple[int, int]]:
    """MCID -> (index of its BDC, index of the matching EMC) in the page content stream."""
    ops = pikepdf.parse_content_stream(page.obj)
    res = page.obj.get("/Resources")
    props = res.get("/Properties") if res is not None else None
    stack: list[tuple[int | None, int]] = []
    out: dict[int, tuple[int, int]] = {}
    for i, x in enumerate(ops):
        op = str(x.operator)
        if op in ("BDC", "BMC"):
            mcid = None
            if op == "BDC":
                p = x.operands[1]
                if isinstance(p, Name) and props is not None:
                    p = props.get(p)
                if isinstance(p, Dictionary) and "/MCID" in p:
                    mcid = int(p.MCID)
            stack.append((mcid, i))
        elif op == "EMC" and stack:
            mcid, start = stack.pop()
            if mcid is not None:
                out[mcid] = (start, i)
    return out


# --------------------------------------------------------------------------- listing

def list_figures(path: str | Path) -> list[FigureItem]:
    path = Path(path)
    items: list[FigureItem] = []
    with pikepdf.open(path) as pdf:
        figs = _figure_elements(pdf)
        for n, (el, _) in enumerate(figs):
            mcids = _mcids_of(pdf, el)
            pg = mcids[0][0] if mcids else (_page_index(pdf, el.get("/Pg")) or 0)
            alt = str(el.get("/Alt", "")).strip()
            items.append(FigureItem(n, pg, None, alt, mcids, obj=el.objgen[0] if el.is_indirect else None))

        # locate each figure on its page: the painted image/form inside its marked-content range
        by_page: dict[int, list[FigureItem]] = {}
        for it in items:
            if it.mcids:
                by_page.setdefault(it.mcids[0][0], []).append(it)
        for pi, its in by_page.items():
            ranges = _mcid_op_ranges(pdf.pages[pi])
            for pd_ in extract_pages(path, pdf, only={pi}):
                for it in its:
                    boxes = []
                    for (_, mcid) in it.mcids:
                        rng = ranges.get(mcid)
                        if not rng:
                            continue
                        for f in pd_.figures:
                            if rng[0] <= f.op <= rng[1]:
                                boxes.append((f.x0, f.y0, f.x1, f.y1))
                    if boxes:
                        it.bbox = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                                   max(b[2] for b in boxes), max(b[3] for b in boxes))
                        it.near_text = _near_text(pd_, it.bbox)
    return items


def _near_text(pd_, bbox) -> str:
    """Text just below (captions), above, or beside the figure: a hint for whoever writes the alt text."""
    x0, y0, x1, y1 = bbox
    below, above = [], []
    for l in pd_.lines:
        overlap = min(l.x1, x1 + 30) - max(l.x0, x0 - 30)
        if overlap <= 0:
            continue
        if y0 - 60 <= l.y1 <= y0 + 4 and l.y1 < y0 + 4:
            below.append((-l.y1, l.text))
        elif y1 - 4 <= l.y0 <= y1 + 45:
            above.append((l.y0, l.text))
    txt = " ".join(t for _, t in sorted(below))[:220]
    if not txt:
        txt = " ".join(t for _, t in sorted(above))[:220]
    return re.sub(r"\s+", " ", txt).strip()


# --------------------------------------------------------------------------- applying decisions

def _detach(parent, el) -> None:
    k = parent.get("/K")
    if isinstance(k, Array):
        for i in range(len(k) - 1, -1, -1):
            x = k[i]
            if isinstance(x, Dictionary) and x.is_indirect and el.is_indirect and x.objgen == el.objgen:
                del k[i]
        if len(k) == 0:
            del parent["/K"]
    elif isinstance(k, Dictionary) and k.is_indirect and el.is_indirect and k.objgen == el.objgen:
        del parent["/K"]


def _null_parent_entries(pdf: pikepdf.Pdf, mcids: list[tuple[int, int]]) -> None:
    st = pdf.Root.StructTreeRoot
    pt = st.get("/ParentTree")
    if pt is None:
        return
    nt = pikepdf.NumberTree(pt)
    for pi, mcid in mcids:
        key = pdf.pages[pi].obj.get("/StructParents")
        if key is None:
            continue
        try:
            arr = nt[int(key)]
            if isinstance(arr, Array) and 0 <= mcid < len(arr):
                arr[mcid] = None
        except Exception:  # noqa: BLE001
            continue


def _artifact_ranges(pdf: pikepdf.Pdf, mcids: list[tuple[int, int]]) -> None:
    """Rewrite the marked-content start of each MCID as /Artifact."""
    by_page: dict[int, set[int]] = {}
    for pi, m in mcids:
        by_page.setdefault(pi, set()).add(m)
    mk = pikepdf.ContentStreamInstruction
    for pi, ms in by_page.items():
        page = pdf.pages[pi]
        res = page.obj.get("/Resources")
        props = res.get("/Properties") if res is not None else None
        ops = pikepdf.parse_content_stream(page.obj)
        out = []
        for x in ops:
            if str(x.operator) == "BDC":
                p = x.operands[1]
                if isinstance(p, Name) and props is not None:
                    p = props.get(p)
                if isinstance(p, Dictionary) and "/MCID" in p and int(p.MCID) in ms:
                    out.append(mk([Name.Artifact], pikepdf.Operator("BMC")))
                    continue
            out.append(x)
        page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(out))


def apply_decisions_to_pdf(pdf: pikepdf.Pdf, decisions: dict[str, dict]) -> dict:
    """Apply alt text / decorative choices to an OPEN document (keys from FigureItem.key)."""
    applied = {"alt": 0, "decorative": 0, "missing": 0}
    infos = {}
    for n, (el, parent) in enumerate(_figure_elements(pdf)):
        mcids = _mcids_of(pdf, el)
        pg = mcids[0][0] if mcids else (_page_index(pdf, el.get("/Pg")) or 0)
        infos[FigureItem(n, pg, None, "", mcids, obj=el.objgen[0] if el.is_indirect else None).key] = (el, parent, mcids)
    for key, dec in decisions.items():
        if key not in infos:
            applied["missing"] += 1
            continue
        el, parent, mcids = infos[key]
        if dec.get("decorative"):
            _artifact_ranges(pdf, mcids)
            _null_parent_entries(pdf, mcids)
            _detach(parent, el)
            applied["decorative"] += 1
        elif str(dec.get("alt", "")).strip():
            el["/Alt"] = pikepdf.String(str(dec["alt"]).strip())
            applied["alt"] += 1
    return applied


def apply_decisions(src: str | Path, dst: str | Path, decisions: dict[str, dict],
                    title: str | None = None, lang: str | None = None) -> dict:
    """Write `dst` from `src` with alt text / decorative choices applied."""
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        raise ValueError("refusing to overwrite the source")
    with pikepdf.open(src) as pdf:
        applied = apply_decisions_to_pdf(pdf, decisions)
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
    return applied
