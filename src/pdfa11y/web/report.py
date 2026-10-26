"""Library report (CSV for spreadsheets, accessible HTML for reading) built from the documents' summaries."""

from __future__ import annotations

import csv
import html
import io
import time


def status_label(m: dict) -> str:
    """Words only (the web UI adds symbols). Mirrors the library's status badge."""
    s = m.get("status")
    if s == "processing":
        return "Processing"
    if s == "failed":
        return "Failed"
    if s == "skipped":
        return "Skipped (scanned)"
    n = m.get("figures_remaining") or 0
    if n > 0:
        return f"Needs {n} image description{'s' if n != 1 else ''}"
    to_confirm = (m.get("confirm_total") or 0) - (m.get("confirm_done") or 0)
    if to_confirm > 0:
        return f"Needs review ({to_confirm} to confirm)"
    exp = m.get("export")
    if exp and exp.get("compliant"):
        return "Exported · passes check"
    return "Ready to export"


def verdict_label(m: dict) -> str:
    exp = m.get("export")
    if not exp:
        return "not exported yet"
    if exp.get("compliant"):
        return "passes PDF/UA-1 check (veraPDF)"
    return "work in progress (does not pass yet)"


def rows(docs: list[dict]) -> list[dict]:
    out = []
    for m in docs:
        out.append({
            "Document": m.get("name", ""),
            "Folder": m.get("folder", ""),
            "Pages": m.get("pages") or "",
            "Status": status_label(m),
            "Images described": f"{(m.get('figures_total') or 0) - (m.get('figures_remaining') or 0)} of {m.get('figures_total') or 0}"
                                 if m.get("status") == "ready" else "",
            "Items confirmed": f"{m.get('confirm_done') or 0} of {m.get('confirm_total') or 0}" if m.get("status") == "ready" else "",
            "Last export": verdict_label(m) if m.get("status") == "ready" else "",
            "Problem": (m.get("error") or "") if m.get("status") in ("failed", "skipped") else "",
        })
    return out


def to_csv(docs: list[dict], extra: dict[str, dict] | None = None) -> bytes:
    """Semicolon-separated with a BOM: opens correctly in Excel with Danish regional settings."""
    r = rows(docs)
    if extra:
        for row, m in zip(r, docs):
            row["Result of this export"] = (extra.get(m["id"]) or {}).get("message", "")
    buf = io.StringIO()
    if r:
        w = csv.DictWriter(buf, fieldnames=list(r[0].keys()), delimiter=";", lineterminator="\r\n")
        w.writeheader()
        w.writerows(r)
    return ("﻿" + buf.getvalue()).encode("utf-8")


def to_html(docs: list[dict], title: str = "PDF accessibility report", extra: dict[str, dict] | None = None) -> bytes:
    r = rows(docs)
    if extra:
        for row, m in zip(r, docs):
            row["Result of this export"] = (extra.get(m["id"]) or {}).get("message", "")
    cols = list(r[0].keys()) if r else []
    ready = sum(1 for m in docs if status_label(m).startswith(("Ready", "Exported")))
    head = "".join(f"<th scope='col'>{html.escape(c)}</th>" for c in cols)
    body = "".join(
        "<tr>" + f"<th scope='row'>{html.escape(str(row['Document']))}</th>" +
        "".join(f"<td>{html.escape(str(row[c]))}</td>" for c in cols[1:]) + "</tr>" for row in r)
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{font:16px/1.5 system-ui,Segoe UI,sans-serif;margin:2rem auto;max-width:80rem;padding:0 1rem;color:#1b1f27;background:#fff}}
h1{{font-size:1.5rem}} table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #b9c0cc;padding:.45rem .6rem;text-align:left;vertical-align:top}}
thead th{{background:#eef1f6}} caption{{text-align:left;font-weight:600;padding:.5rem 0}}
.note{{background:#f3f3f7;border-left:4px solid #557;padding:.6rem .9rem;margin:1rem 0}}
</style></head><body>
<h1>{html.escape(title)}</h1>
<p>Created {time.strftime('%d %B %Y, %H:%M')}. {len(docs)} document(s); {ready} ready or exported.</p>
<p class="note"><strong>Read this first.</strong> "Passes PDF/UA-1 check" means the independent validator veraPDF found no rule violations.
It cannot judge whether image descriptions are meaningful, whether the reading order makes sense, or whether tables are sensible:
a person should confirm those.</p>
<table><caption>Documents</caption><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>
</body></html>"""
    return doc.encode("utf-8")
