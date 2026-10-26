"""Automatic, non-destructive fixes for the problems that need no human judgement.

Every change is logged with a kind:
  auto     - unambiguous (tab order, display-title flag, dropping empty tags...)
  assumed  - a heuristic guess a person should glance at (title, table headers...)
The original file is never touched; output goes to a new path.
"""

from __future__ import annotations

import re
from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name

from .audit import STANDARD_TYPES, _attr_scope, _is_mcr_or_objr, _kids, _name
from .lang import detect_language

BAD_TITLE = re.compile(r"(\.(docx?|indd|pdf|pptx?|xlsx?)$)|^(microsoft (word|powerpoint)\b|untitled|document\d*$)", re.I)


# --------------------------------------------------------------------------- text helpers

def _sample_text(path: Path, pages: int = 12) -> str:
    from pdfminer.high_level import extract_text
    try:
        return extract_text(str(path), maxpages=pages)
    except Exception:  # noqa: BLE001
        return ""


def guess_title(path: Path) -> str | None:
    """Largest-type text at the top of page 1, or None."""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTChar, LTTextContainer, LTTextLine

    try:
        page = next(iter(extract_pages(str(path), maxpages=1)))
    except Exception:  # noqa: BLE001
        return None
    lines = []
    for box in page:
        if not isinstance(box, LTTextContainer):
            continue
        for line in box:
            if not isinstance(line, LTTextLine):
                continue
            sizes = [round(c.size, 1) for c in line if isinstance(c, LTChar)]
            text = re.sub(r"\s+", " ", line.get_text()).strip()
            if sizes and len(text) > 1:
                lines.append((max(sizes), -line.y1, text))
    if not lines:
        return None
    biggest = max(s for s, _, _ in lines)
    chosen = [t for s, _, t in sorted(lines, key=lambda x: x[1]) if s >= biggest * 0.92]
    title = re.sub(r"\s+", " ", " ".join(chosen)).strip(" -–—")
    return title[:200] if len(title) >= 4 else None


# --------------------------------------------------------------------------- struct helpers

class _Tree:
    def __init__(self, pdf: pikepdf.Pdf):
        self.st = pdf.Root.StructTreeRoot
        rm = self.st.get("/RoleMap")
        self.rolemap = {_name(k): _name(v) for k, v in rm.items()} if isinstance(rm, Dictionary) else {}

    def role(self, el) -> str:
        r, seen = _name(el.get("/S", "?")), set()
        while r in self.rolemap and r not in STANDARD_TYPES and r not in seen:
            seen.add(r)
            r = self.rolemap[r]
        return r

    @staticmethod
    def children(node) -> list:
        return [k for k in _kids(node) if isinstance(k, Dictionary) and not _is_mcr_or_objr(k)]

    def pairs(self):
        """(parent, element) in document order; each indirect element once."""
        out, seen = [], set()
        stack = [(self.st, k) for k in reversed(self.children(self.st))]
        while stack:
            parent, el = stack.pop()
            if el.is_indirect:
                if el.objgen in seen:
                    continue
                seen.add(el.objgen)
            out.append((parent, el))
            stack.extend((el, k) for k in reversed(self.children(el)))
        return out


def _same(a, b) -> bool:
    return (isinstance(a, Dictionary) and a.is_indirect and b.is_indirect and a.objgen == b.objgen)


def _detach(parent, el) -> None:
    k = parent.get("/K")
    if isinstance(k, Array):
        for i in range(len(k) - 1, -1, -1):
            if _same(k[i], el):
                del k[i]
        if len(k) == 0:
            del parent["/K"]
    elif _same(k, el):
        del parent["/K"]


def _has_content(el, removed: set) -> bool:
    for k in _kids(el):
        if isinstance(k, int) or _is_mcr_or_objr(k):
            return True
        if isinstance(k, Dictionary) and not (k.is_indirect and k.objgen in removed):
            return True
    return False


def _set_scope(el, scope: str) -> None:
    a = el.get("/A")
    items = list(a) if isinstance(a, Array) else ([a] if a is not None else [])
    for it in items:
        if isinstance(it, Dictionary) and it.get("/O") == Name.Table:
            it["/Scope"] = Name("/" + scope)
            return
    attr = Dictionary(O=Name.Table, Scope=Name("/" + scope))
    if a is None:
        el["/A"] = attr
    elif isinstance(a, Array):
        a.append(attr)
    else:
        el["/A"] = Array([a, attr])


def _rows(tree: _Tree, table):
    rows = []
    for k in tree.children(table):
        r = tree.role(k)
        if r == "TR":
            rows.append((k, False))
        elif r in ("THead", "TBody", "TFoot"):
            rows.extend((kk, r == "THead") for kk in tree.children(k) if tree.role(kk) == "TR")
    return rows


HEAD_RE = re.compile(r"^H([1-6])$")
_CONSTRUCT = {"m", "l", "c", "v", "y", "re", "h", "W", "W*"}
_PAINT = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "n"}
_TEXT_OPS = {"Tj", "TJ", "'", '"'}


def _renumber_headings(tree: _Tree, force: bool = False) -> int:
    """Stack-based renumbering: level = 1 + number of higher-ranking headings still open.
    Applied only if the document skips a level or does not start at H1."""
    heads = []
    for _, el in tree.pairs():
        m = HEAD_RE.match(tree.role(el))
        if m:
            heads.append((el, int(m.group(1))))
        elif tree.role(el) == "H":
            return 0                                   # unnumbered headings: leave alone
    if not heads:
        return 0
    broken = heads[0][1] != 1 or any(b[1] > a[1] + 1 for a, b in zip(heads, heads[1:]))
    if not broken and not force:
        return 0
    stack: list[int] = []
    changed = 0
    for el, raw in heads:
        while stack and stack[-1] >= raw:
            stack.pop()
        stack.append(raw)
        new = min(len(stack), 6)
        if new != raw:
            el["/S"] = Name(f"/H{new}")
            changed += 1
    return changed


def _form_has_text(xo, memo: dict, depth: int = 0) -> bool:
    key = xo.objgen if xo.is_indirect else None
    if key is not None and key in memo:
        return memo[key]
    found = False
    try:
        ins = pikepdf.parse_content_stream(xo)
        res = xo.get("/Resources")
        for x in ins:
            op = str(x.operator)
            if op in _TEXT_OPS:
                found = True
                break
            if op == "Do" and depth < 4 and res is not None:
                sub = res.get("/XObject", {}).get(x.operands[0])
                if sub is not None and sub.get("/Subtype") == Name.Form and _form_has_text(sub, memo, depth + 1):
                    found = True
                    break
    except Exception:  # noqa: BLE001
        found = True                                   # unreadable: assume it matters, do not hide it
    if key is not None:
        memo[key] = found
    return found


def _untagged_show_ops(pdf: pikepdf.Pdf, page) -> tuple[list, list[int]]:
    """(parsed ops, indexes of text-showing operators outside any tagged/artifact sequence)."""
    res = page.obj.get("/Resources")
    props = res.get("/Properties") if res is not None else None
    ops = pikepdf.parse_content_stream(page.obj)
    stack: list[str] = []
    idxs: list[int] = []
    for i, x in enumerate(ops):
        op = str(x.operator)
        if op in ("BDC", "BMC"):
            kind = "O"
            try:
                if _name(x.operands[0]) == "Artifact":
                    kind = "A"
                elif op == "BDC":
                    p = x.operands[1]
                    if isinstance(p, Name) and props is not None:
                        p = props.get(p)
                    if isinstance(p, Dictionary) and "/MCID" in p:
                        kind = "T"
            except Exception:  # noqa: BLE001
                pass
            stack.append(kind)
        elif op == "EMC":
            if stack:
                stack.pop()
        elif op in _TEXT_OPS and "A" not in stack and "T" not in stack:
            idxs.append(i)
    return ops, idxs


def _artifact_margin_text(src: Path, pdf: pikepdf.Pdf) -> int:
    """Untagged text that sits entirely in the top/bottom page margin (running header/footer, URL, page
    number) becomes a Pagination artifact. Untagged text anywhere else is left alone and reported."""
    from collections import defaultdict

    from .extract import extract_pages

    need: dict[int, tuple[list, list[int]]] = {}
    for i, page in enumerate(pdf.pages):
        try:
            ops, idxs = _untagged_show_ops(pdf, page)
        except Exception:  # noqa: BLE001
            continue
        if idxs:
            need[i] = (ops, idxs)
    if not need:
        return 0
    total = 0
    for pd_ in extract_pages(src, pdf, only=set(need)):
        ops, idxs = need[pd_.index]
        if not pd_.aligned:
            continue
        by_op = defaultdict(list)
        for l in pd_.lines:
            for c in l.chars:
                by_op[c.op].append(c)
        for f in pd_.figures:
            for c in f.chars:
                by_op[c.op].append(c)
        choose: dict[int, str] = {}
        for idx in idxs:
            cs = by_op.get(idx)
            if not cs:
                continue
            yc = [(c.y0 + c.y1) / 2 for c in cs]
            if all(y > pd_.height * 0.92 for y in yc):
                choose[idx] = "Header"
            elif all(y < pd_.height * 0.08 for y in yc):
                choose[idx] = "Footer"
        if not choose:
            continue
        mk = pikepdf.ContentStreamInstruction
        out: list = []
        for i, x in enumerate(ops):
            if i in choose:
                out.append(mk([Name.Artifact, Dictionary(Type=Name.Pagination, Subtype=Name("/" + choose[i]))],
                              pikepdf.Operator("BDC")))
                out.append(x)
                out.append(mk([], pikepdf.Operator("EMC")))
                total += 1
            else:
                out.append(x)
        pdf.pages[pd_.index].obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(out))
    return total


def _artifact_untagged_graphics(pdf: pikepdf.Pdf) -> int:
    """Wrap untagged vector paths, shadings and text-free forms in /Artifact. Text and images are never touched."""
    memo: dict = {}
    total = 0
    for page in pdf.pages:
        res = page.obj.get("/Resources")
        props = res.get("/Properties") if res is not None else None
        xobjs = res.get("/XObject") if res is not None else None
        try:
            ins = pikepdf.parse_content_stream(page.obj)
        except Exception:  # noqa: BLE001
            continue
        out: list = []
        stack: list[str] = []
        in_path = wrapped = False
        n = 0
        mk = pikepdf.ContentStreamInstruction
        for x in ins:
            op = str(x.operator)
            if op in ("BDC", "BMC"):
                kind = "O"
                try:
                    tag = _name(x.operands[0])
                    if tag == "Artifact":
                        kind = "A"
                    elif op == "BDC":
                        p = x.operands[1]
                        if isinstance(p, Name) and props is not None:
                            p = props.get(p)
                        if isinstance(p, Dictionary) and "/MCID" in p:
                            kind = "T"
                except Exception:  # noqa: BLE001
                    pass
                stack.append(kind)
                out.append(x)
                continue
            if op == "EMC":
                if stack:
                    stack.pop()
                out.append(x)
                continue
            covered = "A" in stack or "T" in stack
            if op in _CONSTRUCT or op in _PAINT:
                if not in_path:
                    in_path = True
                    wrapped = not covered
                    if wrapped:
                        out.append(mk([Name.Artifact], pikepdf.Operator("BMC")))
                out.append(x)
                if op in _PAINT:
                    if wrapped:
                        out.append(mk([], pikepdf.Operator("EMC")))
                        n += 1
                    in_path = wrapped = False
                continue
            if not covered and op == "sh":
                out += [mk([Name.Artifact], pikepdf.Operator("BMC")), x, mk([], pikepdf.Operator("EMC"))]
                n += 1
                continue
            if not covered and op == "Do" and xobjs is not None:
                xo = xobjs.get(x.operands[0])
                if xo is not None and xo.get("/Subtype") == Name.Form and not _form_has_text(xo, memo):
                    out += [mk([Name.Artifact], pikepdf.Operator("BMC")), x, mk([], pikepdf.Operator("EMC"))]
                    n += 1
                    continue
            out.append(x)
        if n:
            page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(out))
            total += n
    return total


def _describe_links(src: Path, pdf: pikepdf.Pdf) -> int:
    """Give every Link annotation a /Contents description (PDF/UA 7.18.5): the text under it, else its address."""
    from .extract import extract_pages

    need: dict[int, list] = {}
    for i, page in enumerate(pdf.pages):
        for a in (page.obj.get("/Annots") or []):
            try:
                if a.get("/Subtype") == Name.Link and not str(a.get("/Contents", "")).strip():
                    need.setdefault(i, []).append(a)
            except Exception:  # noqa: BLE001
                continue
    if not need:
        return 0
    n = 0
    for pd_ in extract_pages(src, pdf, only=set(need)):
        for a in need[pd_.index]:
            try:
                r = [float(x) for x in a.Rect]
            except Exception:  # noqa: BLE001
                continue
            x0, x1, y0, y1 = min(r[0], r[2]), max(r[0], r[2]), min(r[1], r[3]), max(r[1], r[3])
            txt = ""
            for l in pd_.lines:
                got = "".join(c.text for c in l.chars if x0 - 1 <= (c.x0 + c.x1) / 2 <= x1 + 1 and y0 - 1 <= (c.y0 + c.y1) / 2 <= y1 + 1)
                if got.strip():
                    txt += " " + got
            for f in pd_.figures:
                got = "".join(c.text for c in f.chars if x0 - 1 <= (c.x0 + c.x1) / 2 <= x1 + 1 and y0 - 1 <= (c.y0 + c.y1) / 2 <= y1 + 1)
                if got.strip():
                    txt += " " + got
            txt = re.sub(r"\s+", " ", txt).strip()
            if not txt:
                act = a.get("/A")
                if act is not None and "/URI" in act:
                    txt = str(act.URI)
                elif a.get("/Dest") is not None or (act is not None and "/D" in act):
                    txt = "Link to another place in this document"
                else:
                    txt = "Link"
            a["/Contents"] = pikepdf.String(txt[:250])
            n += 1
    return n


def _drop_bad_cidsets(pdf: pikepdf.Pdf) -> int:
    """CIDSet is optional; when present and wrong (PDF/UA 7.21.4.2) the safe repair is to remove it."""
    n = 0
    for obj in pdf.objects:
        try:
            if isinstance(obj, Dictionary) and obj.get("/Type") == Name.FontDescriptor and "/CIDSet" in obj:
                del obj["/CIDSet"]
                n += 1
        except Exception:  # noqa: BLE001
            continue
    return n


def _fix_thead_without_tbody(tree: _Tree, pdf: pikepdf.Pdf) -> int:
    n = 0
    for _, el in tree.pairs():
        if tree.role(el) != "Table":
            continue
        kids = tree.children(el)
        roles = [tree.role(k) for k in kids]
        if "THead" not in roles or "TBody" in roles:
            continue
        rows = [k for k, r in zip(kids, roles) if r == "TR"]
        if rows:
            body = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.TBody, P=el, K=Array(rows)))
            if "/Pg" in rows[0]:
                body["/Pg"] = rows[0].Pg
            for r in rows:
                r["/P"] = body
            new_k = [k for k, ro in zip(kids, roles) if ro != "TR"]
            i = max((j for j, ro in enumerate(roles) if ro == "THead"), default=-1)
            insert_at = sum(1 for ro in roles[:i + 1] if ro != "TR") if i >= 0 else len(new_k)
            new_k.insert(insert_at, body)
            el["/K"] = Array(new_k)
        else:                                                   # only a THead: flatten it into the table
            flat = []
            for k, ro in zip(kids, roles):
                if ro == "THead":
                    for tr in tree.children(k):
                        tr["/P"] = el
                        flat.append(tr)
                else:
                    flat.append(k)
            el["/K"] = Array(flat)
        n += 1
    return n


def _note_ids(tree: _Tree, pdf: pikepdf.Pdf) -> int:
    """Note elements need a unique /ID (PDF/UA 7.9); register them in an IDTree as ISO 32000 expects."""
    notes = [el for _, el in tree.pairs() if tree.role(el) == "Note"]
    if not notes:
        return 0
    used = {str(el.ID) for _, el in tree.pairs() if "/ID" in el}
    n = 0
    for el in notes:
        if "/ID" not in el or not str(el.ID):
            i = n
            while f"note{i}" in used:
                i += 1
            el["/ID"] = pikepdf.String(f"note{i}")
            used.add(f"note{i}")
            n += 1
    if n:
        nt = pikepdf.NameTree.new(pdf)
        for _, el in tree.pairs():
            if "/ID" in el:
                nt[str(el.ID)] = el
        tree.st["/IDTree"] = nt.obj
    return n


# --------------------------------------------------------------------------- the fixer

def fix_pdf(src: str | Path, dst: str | Path, *, lang: str | None = None, title: str | None = None) -> dict:
    src, dst = Path(src), Path(dst)
    if src.resolve() == dst.resolve():
        raise ValueError("refusing to overwrite the original file")
    changes: list[dict] = []

    def log(kind: str, msg: str) -> None:
        changes.append({"kind": kind, "msg": msg})

    with pikepdf.open(src) as pdf:
        if pdf.is_encrypted:
            raise RuntimeError("file is encrypted; remove the password first")
        root = pdf.Root
        tagged = "/StructTreeRoot" in root

        # ---- language -------------------------------------------------------
        declared = str(root.get("/Lang")) if "/Lang" in root else None
        detected, conf, scores = detect_language(_sample_text(src))
        if lang:
            root["/Lang"] = pikepdf.String(lang)
            log("auto", f"language set to {lang!r} (given)")
        elif detected is None:
            if declared is None:
                log("assumed", "no document language and detection was inconclusive: set it manually")
        elif declared is None:
            root["/Lang"] = pikepdf.String(detected)
            log("auto", f"language set to {detected!r} (detected, confidence {conf})")
        elif declared.split("-")[0].lower() != detected:
            root["/Lang"] = pikepdf.String(detected)
            log("assumed", f"language changed {declared!r} -> {detected!r} (detected, confidence {conf})")

        # ---- title ----------------------------------------------------------
        info_title = str(pdf.docinfo.get("/Title", "")).strip()
        cur = info_title
        if not cur:
            try:
                m = re.search(r"<dc:title>.*?<rdf:li[^>]*>(.*?)</rdf:li>", root.Metadata.read_bytes().decode("utf-8", "ignore"), re.S) if "/Metadata" in root else None
                cur = (m.group(1).strip() if m else "")
            except Exception:  # noqa: BLE001
                cur = ""
        new_title = title
        if new_title is None and (not cur or BAD_TITLE.search(cur)):
            guess = guess_title(src)
            # a one- or two-word "title" is usually a cover label; the filename reads better
            new_title = guess if guess and len(guess.split()) >= 3 else src.stem.replace("_", " ").strip()
            log("assumed", f"title set to {new_title!r} (guessed from page 1; verify)")
        elif new_title:
            log("auto", f"title set to {new_title!r} (given)")
        if new_title:
            try:
                with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
                    meta["dc:title"] = new_title
            except Exception:  # noqa: BLE001
                pass
            pdf.docinfo["/Title"] = pikepdf.String(new_title)

        # ---- viewer preferences, tab order, MarkInfo --------------------------
        vp = root.get("/ViewerPreferences")
        if vp is None:
            root["/ViewerPreferences"] = vp = Dictionary()
        if not bool(vp.get("/DisplayDocTitle", False)):
            vp["/DisplayDocTitle"] = True
            log("auto", "set DisplayDocTitle so the title (not the filename) is shown")

        n_tabs = 0
        for page in pdf.pages:
            if page.obj.get("/Tabs") != Name.S:
                page.obj["/Tabs"] = Name.S
                n_tabs += 1
        if n_tabs:
            log("auto", f"tab order set to structure order on {n_tabs} page(s)")

        if not tagged:
            log("assumed", "file has NO structure tree: needs the auto-tagger (not marked as tagged)")
        else:
            mark = root.get("/MarkInfo")
            if mark is None or not bool(mark.get("/Marked", False)):
                root["/MarkInfo"] = Dictionary(Marked=True)
                log("auto", "MarkInfo /Marked set to true")

            tree = _Tree(pdf)

            # ---- un-remap standard types (e.g. Quote -> P) --------------------
            bad = {k: v for k, v in tree.rolemap.items() if k in STANDARD_TYPES and k != v}
            if bad:
                n = 0
                for _, el in tree.pairs():
                    s = _name(el.get("/S", "?"))
                    if s in bad and bad[s] in STANDARD_TYPES:
                        el["/S"] = Name("/" + bad[s])
                        n += 1
                for k in bad:
                    del tree.st.RoleMap["/" + k]
                tree.rolemap = {k: v for k, v in tree.rolemap.items() if k not in bad}
                log("auto", f"RoleMap remapped standard type(s) {sorted(bad)}: retyped {n} element(s) and removed the mapping")

            # ---- empty elements -------------------------------------------------
            removed: set = set()
            for parent, el in reversed(tree.pairs()):
                if el.is_indirect and tree.role(el) not in ("TD", "TH", "TR") and not _has_content(el, removed):
                    _detach(parent, el)
                    removed.add(el.objgen)
            if removed:
                log("auto", f"removed {len(removed)} empty structure element(s)")

            # ---- heading levels: only touched when they are actually broken ---------
            n_re = _renumber_headings(tree)
            if n_re:
                log("assumed", f"heading levels renumbered on {n_re} heading(s) to remove skipped levels / start at H1 (verify)")

            # ---- untagged text in the page margin (footer URL, page number) ---------
            n_txt = _artifact_margin_text(src, pdf)
            if n_txt:
                log("assumed", f"marked {n_txt} untagged text item(s) in the page margin as header/footer artifacts (verify)")

            # ---- decorative graphics that are neither tagged nor artifacts ----------
            n_art = _artifact_untagged_graphics(pdf)
            if n_art:
                log("auto", f"marked {n_art} untagged decorative graphic(s) as artifacts")

            # ---- tables ---------------------------------------------------------
            promoted = scoped = 0
            for _, el in tree.pairs():
                if tree.role(el) != "Table":
                    continue
                rows = _rows(tree, el)
                cells = [(tr, hd, [c for c in tree.children(tr) if tree.role(c) in ("TH", "TD")]) for tr, hd in rows]
                has_th = any(tree.role(c) == "TH" for _, _, cs in cells for c in cs)
                if not has_th and len(cells) >= 2 and cells[0][2]:
                    for c in cells[0][2]:
                        c["/S"] = Name.TH
                        _set_scope(c, "Column")
                    promoted += 1
                for tr, in_head, cs in cells:
                    roles = [tree.role(c) for c in cs]
                    for i, c in enumerate(cs):
                        if roles[i] == "TH" and _attr_scope(c) is None:
                            row_hdr = i == 0 and "TD" in roles and not in_head
                            _set_scope(c, "Row" if row_hdr else "Column")
                            scoped += 1
            if promoted:
                log("assumed", f"{promoted} table(s) had no header cells: first row promoted to column headers (verify)")
            if scoped:
                log("auto", f"added Scope to {scoped} table header cell(s)")

            n_tb = _fix_thead_without_tbody(tree, pdf)
            if n_tb:
                log("auto", f"{n_tb} table(s) had a header group but no body group: structure repaired")
            n_ids = _note_ids(tree, pdf)
            if n_ids:
                log("auto", f"gave {n_ids} footnote/endnote element(s) the ID they require (and built the ID tree)")

        # ---- link descriptions (all documents) --------------------------------------------
        n_links = _describe_links(src, pdf)
        if n_links:
            log("auto", f"added a description to {n_links} link(s) (their visible text, or the address)")
        n_cid = _drop_bad_cidsets(pdf)
        if n_cid:
            log("auto", f"removed {n_cid} optional font glyph-list entr{'y' if n_cid == 1 else 'ies'} (CIDSet) that did not match the embedded font")

        pdf.save(dst)

    # sanity: does the result open and pass qpdf's structural check?
    with pikepdf.open(dst) as out:
        problems = [str(p) for p in out.check_pdf_syntax()]
    return {"file": src.name, "output": str(dst), "language_detected": detected,
            "language_confidence": conf, "changes": changes, "qpdf_problems": problems}
