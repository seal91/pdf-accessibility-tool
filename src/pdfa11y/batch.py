"""Batch remediation: point it at folders, get accessible copies plus a report.

Per file:
  untagged            -> auto-tag, then automatic fixes
  already tagged      -> automatic fixes only
  scanned (no text)   -> skipped, flagged "needs OCR"
  anything that fails a safety gate is NOT written to the output folder.
"""

from __future__ import annotations

import csv
import html
import json
import os
import re
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from .audit import audit
from .autotag import autotag
from .fix import fix_pdf
from . import verapdf as vp
from .verify import render_diff

# --------------------------------------------------------------------------- statuses
CLEAN = "No issues found by our checks"
PASSES = "Passes PDF/UA-1 check (veraPDF)"
REVIEW = "Needs human review"
FAILED = "Failed"
SKIPPED = "Skipped"


def collect(paths: list[str], out_dir: Path) -> list[tuple[Path, Path]]:
    """[(source, relative output path)] for every PDF, skipping anything inside the output folder."""
    out_dir = out_dir.resolve()
    items: list[tuple[Path, Path]] = []
    for p in map(Path, paths):
        p = p.resolve()
        if p.is_dir():
            for f in sorted(p.rglob("*.pdf")):
                if out_dir in f.resolve().parents:
                    continue
                items.append((f, f.relative_to(p)))
        elif p.suffix.lower() == ".pdf":
            items.append((p, Path(p.name)))
    seen: dict[str, int] = {}
    unique = []
    for src, rel in items:                      # never let two inputs overwrite one output
        key = str(rel).lower()
        if key in seen:
            seen[key] += 1
            rel = rel.with_name(f"{rel.stem}_{seen[key]}{rel.suffix}")
        else:
            seen[key] = 1
        unique.append((src, rel))
    return unique


# --------------------------------------------------------------------------- issue derivation

def issues_from_audit(a: dict) -> list[dict]:
    """Remaining problems, from an audit of the OUTPUT file. severity: high | medium | low."""
    out: list[dict] = []

    def add(sev: str, kind: str, msg: str, count: int = 1) -> None:
        out.append({"severity": sev, "kind": kind, "message": msg, "count": count})

    d, s, t, an = a["document"], a["structure"], a["tagging"], a["annotations"]
    if s is None:
        add("high", "untagged", "Document has no structure tags")
        return out
    ops = t.get("ops", {})
    untagged = sum(v for k, v in ops.items() if k.endswith("_untagged"))
    if untagged:
        add("high", "untagged-content", f"{untagged} content item(s) are neither tagged nor marked as decoration", untagged)
    if not (d["title_docinfo"] or d["title_xmp"]):
        add("high", "title", "No document title")
    if not d["lang"]:
        add("high", "language", "No document language")
    figs = s["figures"] - s["figures_with_alt"] - s["figures_with_actualtext_only"]
    if figs > 0:
        add("high", "alt-text", f"{figs} image(s) need alt text or must be marked decorative", figs)
    if s["tables_without_th"]:
        add("high", "table-headers", f"{s['tables_without_th']} table(s) have no header cells", s["tables_without_th"])
    if s["th_without_scope"]:
        add("medium", "table-scope", f"{s['th_without_scope']} table header cell(s) lack a scope", s["th_without_scope"])
    for h in s["heading_issues"]:
        if re.search(r"\d+ H1s$", h):
            continue                                   # several H1s are allowed; informational only
        add("medium", "headings", h[:1].upper() + h[1:])
    unlinked = an["links"] - an["links_in_struct_tree"]
    if unlinked > 0:
        add("medium", "links", f"{unlinked} link(s) are not in the tag tree", unlinked)
    if t.get("pages_without_tabs_S"):
        add("low", "tab-order", f"{t['pages_without_tabs_S']} page(s) without structure tab order", t["pages_without_tabs_S"])
    if s["custom_unmapped_roles"]:
        add("medium", "roles", f"Custom tag type(s) without a standard mapping: {', '.join(sorted(s['custom_unmapped_roles']))}")
    if s["rolemap_remaps_standard"]:
        add("medium", "roles", f"Standard tag type(s) remapped: {', '.join(s['rolemap_remaps_standard'])}")
    if s["empty_elements"]:
        add("low", "empty-tags", f"{s['empty_elements']} empty tag(s)", s["empty_elements"])
    if a["fonts"]["not_embedded"]:
        add("medium", "fonts", f"{a['fonts']['not_embedded']} font(s) not embedded")
    return out


# --------------------------------------------------------------------------- one file

def _group_notes(notes: list[str]) -> list[str]:
    """'page 3: X', 'page 9: X'  ->  'X (pages 3, 9)'."""
    grouped: dict[str, list[int]] = {}
    for n in notes:
        m = re.match(r"page (\d+): (.*)", n)
        if m:
            grouped.setdefault(m.group(2), []).append(int(m.group(1)))
        else:
            grouped.setdefault(n, [])
    out = []
    for msg, pages in grouped.items():
        pages = sorted(set(pages))
        out.append(f"{msg} (page{'s' if len(pages) > 1 else ''} {', '.join(map(str, pages))})" if pages else msg)
    return out


def process_one(src: str, dst: str, verify: bool = True, validate: bool = True) -> dict:
    """Runs in a worker process. Never raises: failures come back as a result dict."""
    src_p, dst_p = Path(src), Path(dst)
    t0 = time.time()
    res: dict = {"file": src_p.name, "source": str(src_p), "output": None, "action": None, "status": None,
                 "issues": [], "assumptions": [], "review_notes": [], "figures": [], "changes": [], "error": None}
    try:
        before = audit(src_p)
        if "error" in before:
            raise RuntimeError(before["error"])
        res["pages"] = before["pages"]
        if before["document"]["encrypted"]:
            res.update(action="none", status=SKIPPED, error="Encrypted file: remove the password first")
            return _done(res, t0)

        tagged = before["structure"] is not None
        pages_img_only = len(before["tagging"].get("image_only_pages", []))
        if not tagged and before["pages"] and pages_img_only >= 0.5 * before["pages"]:
            res.update(action="none", status=SKIPPED,
                       error=f"Looks scanned ({pages_img_only} of {before['pages']} pages are images only): needs OCR first")
            return _done(res, t0)

        dst_p.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "work.pdf"
            if tagged:
                res["action"] = "Fixed"
                fixrep = fix_pdf(src_p, work)
            else:
                res["action"] = "Tagged + fixed"
                mid = Path(td) / "mid.pdf"
                tagrep = autotag(src_p, mid)
                res["figures"] = tagrep["figures_needing_alt"]
                res["review_notes"] = _group_notes(tagrep["review"])
                res["headings_found"] = len(tagrep["headings"])
                fixrep = fix_pdf(mid, work)

            res["changes"] = fixrep["changes"]
            res["assumptions"] = [c["msg"] for c in fixrep["changes"] if c["kind"] == "assumed"]
            if fixrep["qpdf_problems"]:
                raise RuntimeError(f"structural check failed: {fixrep['qpdf_problems'][:2]}")

            after = audit(work)
            if "error" in after:
                raise RuntimeError(after["error"])
            res["issues"] = issues_from_audit(after)
            res["language"] = after["document"]["lang"]
            res["title"] = after["document"]["title_docinfo"] or after["document"]["title_xmp"]

            if not tagged:
                left = [i for i in res["issues"] if i["kind"] == "untagged-content"]
                if left:
                    raise RuntimeError("safety gate: content left untagged after tagging")
            if verify and not tagged:
                bad = render_diff(src_p, work)
                if bad:
                    raise RuntimeError(f"safety gate: rendering changed on page(s) {bad[:8]}")
            if verify and tagged:
                bad = render_diff(src_p, work)
                if bad:
                    raise RuntimeError(f"safety gate: rendering changed on page(s) {bad[:8]}")

            if validate and vp.available():
                v = vp.validate(work)
                res["stamped"] = False
                if not v["compliant"] and vp.is_identifier_only(v):
                    # everything else passes: the PDF/UA identifier is now a true statement, so add it
                    stamped = Path(td) / "stamped.pdf"
                    vp.stamp_pdfua(work, stamped)
                    v2 = vp.validate(stamped)
                    if v2["compliant"]:
                        work, v, res["stamped"] = stamped, v2, True
                res["verapdf"] = {"compliant": v["compliant"], "failed_checks": v["failed_checks"],
                                  "failed_rules": v["failed_rules"]}
                # the validator's verdict replaces our own approximation of these issues
                rules = v["failed_rules"]
                if len(rules) > 1:      # the identifier is added automatically once everything else passes
                    rules = [r for r in rules if not (str(r["clause"]) == "5" and int(r["test"]) == 1)]
                res["issues"] = [_issue_from_rule(r) for r in rules]

            work.replace(dst_p) if work.drive == dst_p.drive else dst_p.write_bytes(work.read_bytes())
        res["output"] = str(dst_p)
        high = [i for i in res["issues"] if i["severity"] in ("high", "medium")]
        needs_look = bool(high or res["assumptions"] or res["review_notes"])
        if "verapdf" in res and res["verapdf"]["compliant"]:
            res["status"] = REVIEW if needs_look else PASSES
        else:
            res["status"] = REVIEW if needs_look or "verapdf" in res else CLEAN
    except Exception as e:  # noqa: BLE001
        res.update(status=FAILED, error=f"{type(e).__name__}: {e}", output=None)
        if dst_p.exists() and res["output"] is None:
            try:
                dst_p.unlink()
            except OSError:
                pass
    return _done(res, t0)


def _issue_from_rule(r: dict) -> dict:
    kind = "alt-text" if str(r["clause"]) == "7.3" else f"ua-{r['clause']}-{r['test']}"
    msg = r["friendly"] or r["description"]
    if kind == "alt-text":
        msg = f"{r['count']} image(s) need alt text or must be marked decorative"
    elif r["count"] > 1:
        msg = f"{msg} ({r['count']}x)"
    return {"severity": "high", "kind": kind, "message": msg, "count": r["count"]}


def _done(res: dict, t0: float) -> dict:
    res["seconds"] = round(time.time() - t0, 1)
    return res


# --------------------------------------------------------------------------- driver

def run_batch(paths: list[str], out_dir: str | Path, jobs: int = 2, suffix: str = "",
              skip_existing: bool = False, verify: bool = True, validate: bool = True, on_result=None) -> list[dict]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    items = collect(paths, out_dir)
    tasks = []
    for src, rel in items:
        dst = out_dir / rel.with_name(rel.stem + suffix + rel.suffix)
        if dst.resolve() == src.resolve():
            raise ValueError(f"output would overwrite the original: {src}")
        if skip_existing and dst.exists():
            continue
        tasks.append((src, dst))
    results: list[dict] = []
    if jobs <= 1 or len(tasks) <= 1:
        for s, d in tasks:
            r = process_one(str(s), str(d), verify, validate)
            results.append(r)
            if on_result:
                on_result(r, len(results), len(tasks))
    else:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            futs = {ex.submit(process_one, str(s), str(d), verify, validate): (s, d) for s, d in tasks}
            for fut in as_completed(futs):
                try:
                    r = fut.result()
                except Exception as e:  # noqa: BLE001  (worker crashed)
                    s, _ = futs[fut]
                    r = {"file": s.name, "source": str(s), "status": FAILED, "action": None, "issues": [],
                         "assumptions": [], "review_notes": [], "figures": [], "changes": [],
                         "error": f"worker crashed: {e}", "output": None, "seconds": 0}
                results.append(r)
                if on_result:
                    on_result(r, len(results), len(tasks))
    results.sort(key=lambda r: r["source"].lower())
    write_reports(results, out_dir)
    return results


# --------------------------------------------------------------------------- reports

def _needs(r: dict) -> list[str]:
    """Short human list of what still needs a person."""
    out = []
    for i in r["issues"]:
        if i["severity"] in ("high", "medium"):
            out.append(i["message"])
    for a in r["assumptions"]:
        out.append("Confirm: " + a)
    for n in r["review_notes"]:
        out.append("Check: " + n)
    if r.get("error"):
        out.append(r["error"])
    return out


def write_reports(results: list[dict], out_dir: Path) -> None:
    rep_dir = out_dir / "_report"
    rep_dir.mkdir(exist_ok=True)
    (rep_dir / "batch.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    with open(rep_dir / "batch.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["File", "Pages", "Action", "Status", "Language", "Images needing alt text",
                    "Open items", "Details", "Output"])
        for r in results:
            alt = sum(i["count"] for i in r["issues"] if i["kind"] == "alt-text")
            items = _needs(r)
            w.writerow([r["file"], r.get("pages", ""), r.get("action") or "", r["status"], r.get("language") or "",
                        alt, len(items), " | ".join(items), r.get("output") or ""])

    (rep_dir / "batch.html").write_text(_html(results), encoding="utf-8")


def _html(results: list[dict]) -> str:
    n = len(results)
    counts = {s: sum(1 for r in results if r["status"] == s) for s in (PASSES, CLEAN, REVIEW, SKIPPED, FAILED)}
    alt_total = sum(i["count"] for r in results for i in r["issues"] if i["kind"] == "alt-text")
    rows = []
    for r in results:
        items = _needs(r)
        alt = sum(i["count"] for i in r["issues"] if i["kind"] == "alt-text")
        detail = ""
        if items:
            detail = "<ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in items) + "</ul>"
        ch = [c for c in r["changes"] if c["kind"] == "auto"]
        done = ""
        if ch:
            done = "<details><summary>Fixed automatically ({})</summary><ul>{}</ul></details>".format(
                len(ch), "".join(f"<li>{html.escape(c['msg'])}</li>" for c in ch))
        cls = {PASSES: "ok", CLEAN: "ok", REVIEW: "warn", FAILED: "bad", SKIPPED: "skip"}[r["status"]]
        rows.append(
            f"<tr><th scope='row'>{html.escape(r['file'])}</th><td>{r.get('pages', '')}</td>"
            f"<td>{html.escape(r.get('action') or '–')}</td>"
            f"<td class='{cls}'><strong>{html.escape(r['status'])}</strong></td>"
            f"<td>{html.escape(r.get('language') or '–')}</td><td>{alt}</td>"
            f"<td>{detail or '–'}{done}</td></tr>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PDF accessibility batch report</title>
<style>
body{{font:16px/1.5 system-ui,Segoe UI,sans-serif;margin:2rem auto;max-width:78rem;padding:0 1rem;color:#1a1a1a;background:#fff}}
h1{{font-size:1.6rem}} table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #bbb;padding:.5rem .6rem;text-align:left;vertical-align:top}}
thead th{{background:#eee}} caption{{text-align:left;font-weight:600;padding:.5rem 0}}
.ok{{background:#e3f4e6}} .warn{{background:#fff3d6}} .bad{{background:#fbe0e0}} .skip{{background:#e8e8ee}}
ul{{margin:.2rem 0 .2rem 1.1rem;padding:0}} summary{{cursor:pointer}}
.note{{background:#f3f3f7;border-left:4px solid #557;padding:.6rem .9rem;margin:1rem 0}}
</style></head><body>
<h1>PDF accessibility batch report</h1>
<p>{n} file(s) processed. Passes the automated PDF/UA-1 check (veraPDF): <strong>{counts[PASSES]}</strong>.
No issues found by our own checks: <strong>{counts[CLEAN]}</strong>. Needs human review: <strong>{counts[REVIEW]}</strong>.
Skipped: <strong>{counts[SKIPPED]}</strong>. Failed: <strong>{counts[FAILED]}</strong>.
Images needing alt text: <strong>{alt_total}</strong>.</p>
<p class="note"><strong>Read this first.</strong> "Passes PDF/UA-1 check" means the independent open-source validator veraPDF found no
rule violations. That is strong evidence, but it can only check what a machine can check: it cannot judge whether alt text is
meaningful, whether the reading order makes sense, or whether a table is marked up sensibly. Those still need a person.
"Needs human review" lists exactly what to look at. Original files are never modified.</p>
<table><caption>Results per file</caption>
<thead><tr><th scope="col">File</th><th scope="col">Pages</th><th scope="col">Action</th><th scope="col">Status</th>
<th scope="col">Language</th><th scope="col">Images needing alt text</th><th scope="col">Open items</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
</body></html>"""
