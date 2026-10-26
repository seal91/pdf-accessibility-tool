r"""Prepare the demo data folders.

    .venv\Scripts\python demo\prepare_demo.py ready   -> demo\data-ready : all sample publications already processed
    .venv\Scripts\python demo\prepare_demo.py live    -> demo\data-live  : an empty library (for the live drag-in)
    .venv\Scripts\python demo\prepare_demo.py reset   -> rebuild both from scratch

'ready' is your safety net and lets you show the editing parts without waiting. It leaves every image undescribed,
so the image queue is intact for the live part. The sample PDFs are only READ; nothing in samples\ is changed.
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"
NICE = {"indesign": "InDesign", "word": "Word", "international": "International", "national": "National"}


def wipe(target: Path) -> None:
    if target.parent != DEMO or not target.name.startswith("data-"):      # never delete anything else
        raise SystemExit(f"refusing to delete {target}")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)


def make_ready() -> None:
    from pdfa11y.web.store import DocStore

    target = DEMO / "data-ready"
    wipe(target)
    store = DocStore(target, workers=3)
    files = sorted((ROOT / "samples").rglob("*.pdf"))
    t0 = time.time()
    for f in files:
        rel = f.relative_to(ROOT / "samples").parent.parts
        folder = "/".join(NICE.get(p, p) for p in rel)
        with open(f, "rb") as fh:
            store.create(f.name, fh, folder)
    print(f"Processing {len(files)} publications ...")
    while any(m["status"] == "processing" for m in store.list()):
        time.sleep(1)
    time.sleep(2)                                                        # let the last summaries settle
    docs = store.list()
    for m in sorted(docs, key=lambda m: m["name"]):
        print(f"  {m['status']:<9} {m.get('pages') or '?':>3} pages  {m['figures_remaining']:>3} images to describe  {m['name'][:60]}")
    print(f"Done in {time.time() - t0:.0f} s -> {target}")
    store.shutdown()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")     # Danish file names on any console
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    if what in ("live", "reset"):
        wipe(DEMO / "data-live")
        print("Created an empty demo\data-live")
    if what in ("ready", "reset"):
        make_ready()
    if what not in ("live", "ready", "reset"):
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))
    raise SystemExit(main())
