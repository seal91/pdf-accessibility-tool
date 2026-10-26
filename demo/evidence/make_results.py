"""Build demo/evidence/results-at-a-glance.html from demo/evidence/results.json (real measurements).

Re-create the measurements with:  python demo/evidence/collect.py
"""

from __future__ import annotations

import html
import json
import time
from pathlib import Path

HERE = Path(__file__).parent
rows = json.loads((HERE / "results.json").read_text(encoding="utf-8"))

pages = sum(r["pages"] for r in rows)
before = sum(r["before_checks"] for r in rows)
after = sum(r["after_checks"] for r in rows)
passing_after = sum(1 for r in rows if r["after_pass"])
passing_before = sum(1 for r in rows if r["before_pass"])
alt = sum(r["images_needing_alt"] for r in rows)
secs = sum(r["seconds"] for r in rows)


def left(r) -> str:
    if r["after_pass"]:
        return "Nothing: passes the check"
    parts = []
    if r["images_needing_alt"]:
        parts.append(f"{r['images_needing_alt']} image description{'s' if r['images_needing_alt'] != 1 else ''} (a person writes these)")
    return "; ".join(parts) + ("; the PDF/UA stamp is added automatically afterwards" if parts else "")


def bar(n: int, top: int) -> str:
    w = max(1.5, 100 * n / top) if n else 0
    return f'<span class="bar" aria-hidden="true"><span style="width:{w:.1f}%"></span></span>'


top = max(r["before_checks"] for r in rows) or 1
body = []
for r in sorted(rows, key=lambda r: -r["before_checks"]):
    before_txt = "passes already" if r["before_pass"] else f"{r['before_checks']:,}".replace(",", " ")
    after_txt = "passes" if r["after_pass"] else f"{r['after_checks']:,}".replace(",", " ")
    body.append(
        "<tr>"
        f"<th scope='row'>{html.escape(r['file'])}<div class='sub'>{html.escape(r['area'])} · made in {r['source']} · {r['pages']} pages</div></th>"
        f"<td class='num'>{before_txt}{bar(r['before_checks'], top)}</td>"
        f"<td class='num {'ok' if r['after_pass'] else ''}'>{after_txt}{bar(r['after_checks'], top)}</td>"
        f"<td>{html.escape(left(r))}</td>"
        f"<td class='num'>{r['seconds']:.0f} s</td></tr>")

doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Results at a glance · PDF accessibility tool</title>
<style>
:root{{--text:#1b1f27;--muted:#4b5361;--line:#b9c0cc;--ok:#166534;--okbg:#e4f4e8;--blue:#1d4ed8}}
body{{font:17px/1.55 "Segoe UI",system-ui,Arial,sans-serif;color:var(--text);margin:0;background:#f6f7f9}}
main{{max-width:1100px;margin:0 auto;padding:1.5rem}}
h1{{font-size:1.9rem;margin:.2rem 0}} h2{{font-size:1.25rem;margin:1.8rem 0 .5rem}}
.lead{{color:var(--muted);margin:0 0 1rem}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:.8rem;margin:1rem 0}}
.card{{background:#fff;border:1px solid #dde1e8;border-radius:8px;padding:.9rem 1rem}}
.card b{{display:block;font-size:2rem;line-height:1.1;color:var(--blue)}} .card span{{color:var(--muted);font-size:.95rem}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line)}}
caption{{text-align:left;font-weight:600;padding:.6rem 0}}
th,td{{padding:.55rem .7rem;border-bottom:1px solid #dde1e8;text-align:left;vertical-align:top}}
thead th{{background:#eef1f6}} .sub{{font-weight:400;color:var(--muted);font-size:.88rem}}
td.num{{white-space:nowrap}} td.ok{{color:var(--ok);font-weight:700}}
.bar{{display:block;height:7px;background:#e6e9ef;border-radius:4px;margin-top:4px;min-width:90px}}
.bar span{{display:block;height:100%;background:#1d4ed8;border-radius:4px}}
td.ok .bar span{{background:var(--ok)}}
.note{{background:#fff;border-left:4px solid #557;padding:.7rem 1rem;margin:1rem 0}}
ul{{margin:.3rem 0 .3rem 1.2rem}}
@media print{{body{{background:#fff}} .card,.note{{break-inside:avoid}}}}
</style></head><body><main>
<h1>Results at a glance</h1>
<p class="lead">Ten published publications, run through the tool automatically, then checked with the independent PDF/UA validator veraPDF.
Measured {time.strftime('%d %B %Y')} on the development PC. No human edits.</p>

<div class="cards" role="list">
  <div class="card" role="listitem"><b>{len(rows)}</b><span>publications ({pages} pages) in Danish, English and Spanish</span></div>
  <div class="card" role="listitem"><b>{before:,}</b><span>failed accessibility checks before</span></div>
  <div class="card" role="listitem"><b>{after:,}</b><span>failed checks after automatic processing</span></div>
  <div class="card" role="listitem"><b>{passing_after} of {len(rows)}</b><span>pass completely ({passing_before} did before)</span></div>
  <div class="card" role="listitem"><b>{secs / pages:.1f} s</b><span>of processing per page ({secs:.0f} s in total; several files ran in parallel)</span></div>
</div>

<p>Almost everything that remains is <strong>{alt} images that need a description</strong>. That is deliberately left to a person:
only someone who knows the publication can say what an image is for.</p>

<h2>Publication by publication</h2>
<table>
<caption>Failed PDF/UA-1 checks before and after automatic processing (fewer is better)</caption>
<thead><tr><th scope="col">Publication</th><th scope="col">Before</th><th scope="col">After</th><th scope="col">What is left</th><th scope="col">Processing</th></tr></thead>
<tbody>{''.join(body)}</tbody></table>

<h2>How to read this honestly</h2>
<div class="note">
<ul>
<li><strong>“Passes” means the validator found no rule violations.</strong> It cannot judge whether image descriptions are meaningful,
whether the reading order makes sense on a complicated page, or whether a table is sensible. A person still looks over each file.</li>
<li><strong>“After” is before any human work.</strong> Once the image descriptions are written, the files that only lacked those pass too
(shown live in the demo on a short document).</li>
<li><strong>Two of the ten were already fine</strong> (one Word report, one InDesign guide). The tool leaves good files alone.</li>
<li><strong>Ten publications is a small sample.</strong> A pilot on 30 to 50 documents is the next honest step.</li>
<li>Everything ran offline on one computer; nothing was uploaded anywhere.</li>
</ul></div>
</main></body></html>
"""
out = HERE / "results-at-a-glance.html"
out.write_text(doc, encoding="utf-8")
print("wrote", out, f"({len(doc) // 1024} KB)")
