"""Read-only accessibility audit of a PDF.

Produces a plain dict (JSON-serialisable) describing document-level settings,
the structure tree, how much page content is actually tagged, fonts and
annotations. Nothing here modifies the file.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name, Stream

# ISO 32000-1 standard structure types (plus PDF 2.0 additions are treated as custom).
STANDARD_TYPES = {
    "Document", "Part", "Art", "Sect", "Div", "BlockQuote", "Caption", "TOC", "TOCI",
    "Index", "NonStruct", "Private", "P", "H", "H1", "H2", "H3", "H4", "H5", "H6",
    "L", "LI", "Lbl", "LBody", "Table", "TR", "TH", "TD", "THead", "TBody", "TFoot",
    "Span", "Quote", "Note", "Reference", "BibEntry", "Code", "Link", "Annot",
    "Ruby", "RB", "RT", "RP", "Warichu", "WT", "WP", "Figure", "Formula", "Form",
}
HEADINGS = {"H", "H1", "H2", "H3", "H4", "H5", "H6"}
TEXT_OPS = {"Tj", "TJ", "'", '"'}
PATH_OPS = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}


def _name(obj) -> str:
    s = str(obj)
    return s[1:] if s.startswith("/") else s


def _kids(node) -> list:
    k = node.get("/K")
    if k is None:
        return []
    if isinstance(k, Array):
        return list(k)
    return [k]


def _is_mcr_or_objr(obj) -> bool:
    return isinstance(obj, Dictionary) and obj.get("/Type") in (Name.MCR, Name.OBJR)


def _attr_scope(elem) -> str | None:
    a = elem.get("/A")
    if a is None:
        return None
    items = list(a) if isinstance(a, Array) else [a]
    for it in items:
        if isinstance(it, Dictionary) and "/Scope" in it:
            return _name(it.Scope)
    return None


# --------------------------------------------------------------------------- doc-level

def _doc_level(pdf: pikepdf.Pdf) -> dict:
    root = pdf.Root
    info = pdf.docinfo
    xmp = b""
    try:
        if "/Metadata" in root:
            xmp = root.Metadata.read_bytes()
    except Exception:
        pass
    xmp_s = xmp.decode("utf-8", "ignore")
    ua = re.search(r"pdfuaid:part[>=\"']+\s*(\d)", xmp_s)
    xmp_title = re.search(r"<dc:title>.*?<rdf:li[^>]*>(.*?)</rdf:li>", xmp_s, re.S)
    vp = root.get("/ViewerPreferences")
    mark = root.get("/MarkInfo")
    lang = root.get("/Lang")
    return {
        "pdf_version": pdf.pdf_version,
        "encrypted": pdf.is_encrypted,
        "producer": str(info.get("/Producer", "")) or None,
        "creator": str(info.get("/Creator", "")) or None,
        "title_docinfo": (str(info.get("/Title", "")).strip() or None),
        "title_xmp": (xmp_title.group(1).strip() if xmp_title else None),
        "lang": str(lang) if lang is not None else None,
        "display_doc_title": bool(vp.get("/DisplayDocTitle", False)) if vp is not None else False,
        "marked": bool(mark.get("/Marked", False)) if mark is not None else False,
        "has_struct_tree": "/StructTreeRoot" in root,
        "pdfua_identifier": int(ua.group(1)) if ua else None,
        "has_bookmarks": "/Outlines" in root and root.Outlines.get("/First") is not None,
        "has_page_labels": "/PageLabels" in root,
        "is_acroform": "/AcroForm" in root,
    }


# --------------------------------------------------------------------------- struct tree

def _struct_tree(pdf: pikepdf.Pdf) -> dict | None:
    st = pdf.Root.get("/StructTreeRoot")
    if st is None:
        return None
    rolemap = {}
    rm = st.get("/RoleMap")
    if isinstance(rm, Dictionary):
        rolemap = {_name(k): _name(v) for k, v in rm.items()}

    def resolve(role: str) -> str:
        seen = set()
        while role in rolemap and role not in seen and role not in STANDARD_TYPES:
            seen.add(role)
            role = rolemap[role]
        return role

    roles: Counter = Counter()
    custom_unmapped: Counter = Counter()
    heading_seq: list[int] = []
    figures = figures_alt = figures_actual = 0
    tables = tables_no_th = 0
    th_total = th_no_scope = 0
    link_elems = link_elems_with_objr = 0
    list_bad_children = 0
    empty_elems = 0
    mcrs = 0
    max_depth = 0
    seen: set = set()
    table_stack_info: dict = {}

    # iterative DFS keeping document order
    stack = [(k, 1, None) for k in reversed(_kids(st)) if isinstance(k, Dictionary)]
    # For tables: track per-table TH/TD counts. Map table objgen -> [th, td]
    while stack:
        node, depth, parent = stack.pop()
        if _is_mcr_or_objr(node):
            continue
        og = node.objgen if node.is_indirect else None
        if og is not None:
            if og in seen:
                continue
            seen.add(og)
        raw = _name(node.get("/S", "?"))
        role = resolve(raw)
        roles[role] += 1
        if role not in STANDARD_TYPES:
            custom_unmapped[raw] += 1
        max_depth = max(max_depth, depth)

        kids = _kids(node)
        has_content = False
        has_objr = False
        for kd in kids:
            if isinstance(kd, int):
                mcrs += 1
                has_content = True
            elif _is_mcr_or_objr(kd):
                has_content = True
                if kd.Type == Name.OBJR:
                    has_objr = True
                else:
                    mcrs += 1
            elif isinstance(kd, Dictionary):
                has_content = True

        if role in HEADINGS:
            heading_seq.append(0 if role == "H" else int(role[1]))
        elif role == "Figure":
            figures += 1
            alt = node.get("/Alt")
            if alt is not None and str(alt).strip():
                figures_alt += 1
            elif node.get("/ActualText") is not None and str(node.ActualText).strip():
                figures_actual += 1
        elif role == "Table":
            tables += 1
            has_th = False
            # scan descendants for TH
            sub = [k for k in kids if isinstance(k, Dictionary)]
            local_seen = set()
            while sub:
                n = sub.pop()
                if _is_mcr_or_objr(n):
                    continue
                if n.is_indirect:
                    if n.objgen in local_seen:
                        continue
                    local_seen.add(n.objgen)
                r = resolve(_name(n.get("/S", "?")))
                if r == "TH":
                    has_th = True
                    break
                if r == "Table":
                    continue  # nested table handled on its own
                sub.extend(k for k in _kids(n) if isinstance(k, Dictionary))
            if not has_th:
                tables_no_th += 1
        elif role == "TH":
            th_total += 1
            if _attr_scope(node) is None:
                th_no_scope += 1
        elif role == "Link":
            link_elems += 1
            if has_objr:
                link_elems_with_objr += 1
        elif role == "L":
            for kd in kids:
                if isinstance(kd, Dictionary) and not _is_mcr_or_objr(kd):
                    if resolve(_name(kd.get("/S", "?"))) not in ("LI", "L"):
                        list_bad_children += 1
        if not has_content:
            empty_elems += 1

        for kd in reversed(kids):
            if isinstance(kd, Dictionary) and not _is_mcr_or_objr(kd):
                stack.append((kd, depth + 1, role))

    heading_issues = []
    if heading_seq:
        if heading_seq[0] not in (0, 1):
            heading_issues.append(f"first heading is H{heading_seq[0]}, not H1")
        prev = None
        skips = 0
        for h in heading_seq:
            if h and prev and h > prev + 1:
                skips += 1
            if h:
                prev = h
        if skips:
            heading_issues.append(f"{skips} skipped heading level(s)")
        h1s = sum(1 for h in heading_seq if h == 1)
        if h1s == 0 and any(heading_seq):
            heading_issues.append("no H1")
        if h1s > 1:
            heading_issues.append(f"{h1s} H1s")

    return {
        "elements": sum(roles.values()),
        "roles": dict(roles.most_common()),
        "rolemap_size": len(rolemap),
        "rolemap_remaps_standard": sorted(k for k in rolemap if k in STANDARD_TYPES and rolemap[k] != k),
        "custom_unmapped_roles": dict(custom_unmapped),
        "max_depth": max_depth,
        "mcr_count": mcrs,
        "empty_elements": empty_elems,
        "headings": len(heading_seq),
        "heading_issues": heading_issues,
        "figures": figures,
        "figures_with_alt": figures_alt,
        "figures_with_actualtext_only": figures_actual,
        "tables": tables,
        "tables_without_th": tables_no_th,
        "th_total": th_total,
        "th_without_scope": th_no_scope,
        "link_elements": link_elems,
        "link_elements_with_objr": link_elems_with_objr,
        "list_bad_children": list_bad_children,
    }


# --------------------------------------------------------------------------- content tagging

def _page_tagging(page: pikepdf.Page) -> dict:
    """Classify painted content as tagged (MCID), artifact, or untagged."""
    res = page.obj.get("/Resources")
    props = res.get("/Properties") if res is not None else None
    xobjs = res.get("/XObject") if res is not None else None

    counts = Counter()
    stack: list[str] = []  # 'A' artifact, 'T' tagged (has MCID), 'O' other marked content

    try:
        instructions = pikepdf.parse_content_stream(page.obj)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}

    for ins in instructions:
        op = str(ins.operator)
        if op == "BDC":
            kind = "O"
            try:
                tag = _name(ins.operands[0])
                p = ins.operands[1]
                if isinstance(p, Name) and props is not None:
                    p = props.get(p)
                if tag == "Artifact":
                    kind = "A"
                elif isinstance(p, Dictionary) and "/MCID" in p:
                    kind = "T"
            except Exception:  # noqa: BLE001
                pass
            stack.append(kind)
            continue
        if op == "BMC":
            stack.append("A" if _name(ins.operands[0]) == "Artifact" else "O")
            continue
        if op == "EMC":
            if stack:
                stack.pop()
            continue

        if op in TEXT_OPS:
            cat = "text"
        elif op == "Do":
            cat = "form"
            try:
                xo = xobjs.get(ins.operands[0]) if xobjs is not None else None
                if xo is not None and xo.get("/Subtype") == Name.Image:
                    cat = "image"
            except Exception:  # noqa: BLE001
                pass
        elif op == "INLINE IMAGE":
            cat = "image"
        elif op in PATH_OPS:
            cat = "path"
        else:
            continue

        if "A" in stack:
            state = "artifact"
        elif "T" in stack:
            state = "tagged"
        else:
            state = "untagged"
        counts[f"{cat}_{state}"] += 1
    return dict(counts)


def _tagging(pdf: pikepdf.Pdf, max_pages: int | None = None) -> dict:
    total: Counter = Counter()
    untagged_text_pages = []
    image_only_pages = []
    no_tabs = 0
    errors = 0
    for i, page in enumerate(pdf.pages):
        if max_pages is not None and i >= max_pages:
            break
        if "/Tabs" not in page.obj or _name(page.obj.Tabs) != "S":
            no_tabs += 1
        t = _page_tagging(page)
        if "error" in t:
            errors += 1
            continue
        total.update(t)
        text_all = sum(v for k, v in t.items() if k.startswith("text_"))
        img_all = sum(v for k, v in t.items() if k.startswith("image_"))
        if t.get("text_untagged", 0) > 0:
            untagged_text_pages.append(i + 1)
        if text_all == 0 and img_all > 0:
            image_only_pages.append(i + 1)
    text_total = sum(v for k, v in total.items() if k.startswith("text_"))
    return {
        "ops": dict(total),
        "text_ops_total": text_total,
        "text_untagged_pct": round(100 * total.get("text_untagged", 0) / text_total, 1) if text_total else None,
        "pages_with_untagged_text": len(untagged_text_pages),
        "image_only_pages": image_only_pages,
        "pages_without_tabs_S": no_tabs,
        "parse_errors": errors,
    }


# --------------------------------------------------------------------------- fonts / annots

def _fonts(pdf: pikepdf.Pdf) -> dict:
    seen = set()
    total = not_embedded = no_tounicode = 0
    bad_names = []
    for page in pdf.pages:
        res = page.obj.get("/Resources")
        fonts = res.get("/Font") if res is not None else None
        if not isinstance(fonts, Dictionary):
            continue
        for _, f in fonts.items():
            try:
                if f.is_indirect:
                    if f.objgen in seen:
                        continue
                    seen.add(f.objgen)
                sub = _name(f.get("/Subtype", ""))
                if sub == "Type3":
                    total += 1
                    if "/ToUnicode" not in f:
                        no_tounicode += 1
                    continue
                base = f
                if sub == "Type0":
                    d = f.get("/DescendantFonts")
                    if d:
                        base = d[0]
                fd = base.get("/FontDescriptor")
                total += 1
                embedded = fd is not None and any(k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3"))
                if not embedded:
                    not_embedded += 1
                    bad_names.append(_name(f.get("/BaseFont", "?")))
                if "/ToUnicode" not in f:
                    no_tounicode += 1
            except Exception:  # noqa: BLE001
                continue
    return {"fonts": total, "not_embedded": not_embedded, "without_tounicode": no_tounicode,
            "not_embedded_names": sorted(set(bad_names))[:10]}


def _annots(pdf: pikepdf.Pdf) -> dict:
    subtypes: Counter = Counter()
    links = links_struct = links_contents = 0
    widgets = widgets_tu = 0
    for page in pdf.pages:
        annots = page.obj.get("/Annots")
        if not annots:
            continue
        for a in annots:
            try:
                st = _name(a.get("/Subtype", "?"))
            except Exception:  # noqa: BLE001
                continue
            subtypes[st] += 1
            if st == "Link":
                links += 1
                if "/StructParent" in a:
                    links_struct += 1
                if "/Contents" in a:
                    links_contents += 1
            elif st == "Widget":
                widgets += 1
                if "/TU" in a:
                    widgets_tu += 1
    return {
        "subtypes": dict(subtypes),
        "links": links,
        "links_in_struct_tree": links_struct,
        "links_with_contents": links_contents,
        "widgets": widgets,
        "widgets_with_tooltip": widgets_tu,
    }


# --------------------------------------------------------------------------- public

def audit(path: str | Path, max_pages: int | None = None) -> dict:
    path = Path(path)
    t0 = time.time()
    out: dict = {"file": path.name, "size_mb": round(path.stat().st_size / 1e6, 2)}
    try:
        with pikepdf.open(path) as pdf:
            out["pages"] = len(pdf.pages)
            out["document"] = _doc_level(pdf)
            out["structure"] = _struct_tree(pdf)
            out["tagging"] = _tagging(pdf, max_pages)
            out["fonts"] = _fonts(pdf)
            out["annotations"] = _annots(pdf)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    out["seconds"] = round(time.time() - t0, 1)
    return out
