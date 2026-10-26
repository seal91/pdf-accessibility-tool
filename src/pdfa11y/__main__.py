"""Command line entry point: python -m pdfa11y audit <files or folders> [--out DIR]"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .audit import audit


def _collect(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            files.extend(sorted(p.rglob("*.pdf")))
        elif p.suffix.lower() == ".pdf":
            files.append(p)
    return files


def _summary(r: dict) -> str:
    if "error" in r:
        return f"{r['file'][:48]:<48}  ERROR {r['error']}"
    d, s, t = r["document"], r["structure"], r["tagging"]
    tag = "tagged" if s else "NO TAGS"
    untagged = t["text_untagged_pct"]
    return (
        f"{r['file'][:48]:<48} {r['pages']:>4}p  {tag:<7} "
        f"lang={d['lang'] or '-':<6} title={'y' if (d['title_docinfo'] or d['title_xmp']) else 'n'} "
        f"untagged-text={untagged if untagged is not None else '-'}%  "
        f"prod={(d['producer'] or '?')[:28]}"
    )


def _run_fix(args) -> int:
    from .fix import fix_pdf

    files = _collect(args.paths)
    if not files:
        print("no PDFs found", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for f in files:
        dst = out / (f.stem + ".fixed.pdf")
        try:
            rep = fix_pdf(f, dst)
        except Exception as e:  # noqa: BLE001
            print(f"{f.name[:60]}\n   FAILED: {type(e).__name__}: {e}", flush=True)
            continue
        print(f.name[:80])
        for c in rep["changes"]:
            print(f"   [{c['kind']:<7}] {c['msg']}")
        if rep["qpdf_problems"]:
            print(f"   !! qpdf check: {rep['qpdf_problems'][:3]}")
        (out / (f.stem + ".fix.json")).write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


def _run_tag(args) -> int:
    import tempfile

    from .autotag import autotag
    from .fix import fix_pdf

    files = _collect(args.paths)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for f in files:
        dst = out / (f.stem + ".tagged.pdf")
        try:
            with tempfile.TemporaryDirectory() as td:
                mid = Path(td) / "mid.pdf"
                rep = autotag(f, mid)
                fixrep = fix_pdf(mid, dst)
        except Exception as e:  # noqa: BLE001
            print(f"{f.name[:70]}\n   FAILED: {type(e).__name__}: {e}", flush=True)
            continue
        rep["fix"] = fixrep["changes"]
        rep["qpdf_problems"] = fixrep["qpdf_problems"]
        print(f.name[:80])
        print(f"   blocks: {rep['counts']}  links: {rep['links']}  bookmarks: {rep.get('bookmarks', 0)}")
        print(f"   figures needing alt text: {len(rep['figures_needing_alt'])}   review notes: {len(rep['review'])}")
        for c in fixrep["changes"]:
            print(f"   [{c['kind']:<7}] {c['msg']}")
        if fixrep["qpdf_problems"]:
            print(f"   !! qpdf check: {fixrep['qpdf_problems'][:3]}")
        (out / (f.stem + ".tag.json")).write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


def _run_batch(args) -> int:
    from .batch import CLEAN, FAILED, PASSES, REVIEW, SKIPPED, run_batch

    def show(r, i, n):
        extra = r.get("error") or ""
        print(f"[{i}/{n}] {r['status']:<32} {r.get('action') or '-':<15} {r['file'][:60]}  ({r['seconds']}s) {extra[:90]}", flush=True)

    try:
        results = run_batch(args.paths, args.out, jobs=args.jobs, suffix=args.suffix,
                            skip_existing=args.skip_existing, verify=not args.no_verify,
                            validate=not args.no_validate, on_result=show)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    tally = {s: sum(1 for r in results if r["status"] == s) for s in (PASSES, CLEAN, REVIEW, SKIPPED, FAILED)}
    print()
    print("  ".join(f"{k}: {v}" for k, v in tally.items()))
    print(f"Report: {Path(args.out) / '_report' / 'batch.html'}")
    return 1 if tally[FAILED] else 0


def _run_validate(args) -> int:
    from . import verapdf as vp

    if not vp.available():
        print("veraPDF is not installed (expected under the tools folder).", file=sys.stderr)
        return 2
    if args.stamp and not args.out:
        print("--stamp needs --out", file=sys.stderr)
        return 2
    files = _collect(args.paths)
    bad = 0
    for f in files:
        try:
            v = vp.validate(f)
        except Exception as e:  # noqa: BLE001
            print(f"{f.name[:70]}\n   ERROR: {e}")
            bad += 1
            continue
        verdict = "PASS" if v["compliant"] else "FAIL"
        note = ""
        if args.stamp and not v["compliant"] and vp.is_identifier_only(v):
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            dst = out / f.name
            vp.stamp_pdfua(f, dst)
            v2 = vp.validate(dst)
            note = f"  -> stamped copy written, now {'PASS' if v2['compliant'] else 'FAIL'}: {dst}"
        print(f"{verdict}  {f.name[:70]}  ({v['failed_checks']} failed checks){note}")
        for r in v["failed_rules"]:
            print(f"     {r['clause']}-{r['test']} x{r['count']}  {r['friendly'] or r['description'][:100]}")
        bad += 0 if v["compliant"] else 1
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pdfa11y")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit", help="audit PDFs (read-only)")
    a.add_argument("paths", nargs="+")
    a.add_argument("--out", default=None, help="write one JSON report per file here")
    a.add_argument("--max-pages", type=int, default=None, help="limit content-stream analysis")
    f = sub.add_parser("fix", help="apply automatic fixes; writes NEW files, originals are untouched")
    f.add_argument("paths", nargs="+")
    f.add_argument("--out", required=True, help="output folder")
    t = sub.add_parser("tag", help="auto-tag untagged PDFs, then apply the automatic fixes; writes NEW files")
    t.add_argument("paths", nargs="+")
    t.add_argument("--out", required=True, help="output folder")
    b = sub.add_parser("batch", help="remediate whole folders: tag or fix each PDF, write copies + a report")
    b.add_argument("paths", nargs="+", help="PDF files and/or folders (searched recursively)")
    b.add_argument("--out", required=True, help="output folder (must not be one of the input folders)")
    b.add_argument("--jobs", type=int, default=2, help="files processed in parallel (default 2)")
    b.add_argument("--suffix", default="", help="text added to output file names, e.g. _accessible")
    b.add_argument("--skip-existing", action="store_true", help="do not redo files already in the output folder")
    b.add_argument("--no-verify", action="store_true", help="skip the render-comparison safety check (faster)")
    b.add_argument("--no-validate", action="store_true", help="skip the veraPDF PDF/UA validation")
    va = sub.add_parser("validate", help="check PDFs against PDF/UA-1 with veraPDF (read-only unless --stamp)")
    va.add_argument("paths", nargs="+")
    va.add_argument("--stamp", action="store_true",
                    help="if the ONLY failure is the missing PDF/UA identifier, write a stamped copy to --out")
    va.add_argument("--out", default=None, help="folder for stamped copies (required with --stamp)")
    st = sub.add_parser("selftest", help="check that everything works on this machine")
    st.add_argument("--deep", metavar="PDF", default=None, help="also open this real PDF in the desktop app (offscreen)")
    w = sub.add_parser("web", help="start the browser version (local by default)")
    w.add_argument("--host", default="127.0.0.1", help="127.0.0.1 = this computer only (default); 0.0.0.0 = whole network (needs --password)")
    w.add_argument("--port", type=int, default=0, help="0 = pick a free port")
    w.add_argument("--data-dir", default=None, help="where documents are kept")
    w.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    w.add_argument("--token", default=None, help="fixed access key (default: random each start)")
    w.add_argument("--password", default=None, help="require this password instead of a key (for shared use)")
    w.add_argument("--max-upload-mb", type=int, default=150)
    w.add_argument("--retention-days", type=float, default=None, help="delete documents untouched for this many days")
    w.add_argument("--workers", type=int, default=2, help="PDFs processed in parallel")
    w.add_argument("--no-auth", action="store_true", help="local only: skip the access key (testing)")
    w.add_argument("--idle-minutes", type=float, default=None, help="stop by itself this many minutes after the last browser tab closes")
    w.add_argument("--single-instance", action="store_true", help="if already running, just re-open it in the browser")
    g = sub.add_parser("gui", help="open the desktop app")
    g.add_argument("file", nargs="?", help="a PDF to open straight away")
    args = ap.parse_args(argv)
    if args.cmd == "selftest":
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        from .selftest import run
        return run(args.deep)
    if args.cmd == "web":
        from .web.server import run as web_run
        return web_run(host=args.host, port=args.port, data_dir=args.data_dir, token=args.token,
                       password=args.password or os.environ.get("PDFA11Y_PASSWORD") or None,
                       open_browser=not args.no_browser, max_upload_mb=args.max_upload_mb,
                       retention_days=args.retention_days, workers=args.workers, no_auth=args.no_auth,
                       idle_minutes=args.idle_minutes, single_instance=args.single_instance)
    if args.cmd == "gui":
        from .gui.main import main as gui_main
        return gui_main([sys.argv[0]] + ([args.file] if args.file else []))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.cmd == "fix":
        return _run_fix(args)
    if args.cmd == "tag":
        return _run_tag(args)
    if args.cmd == "batch":
        return _run_batch(args)
    if args.cmd == "validate":
        return _run_validate(args)

    files = _collect(args.paths)
    if not files:
        print("no PDFs found", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    for f in files:
        r = audit(f, args.max_pages)
        print(_summary(r), flush=True)
        if out:
            (out / (f.stem + ".audit.json")).write_text(
                json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
