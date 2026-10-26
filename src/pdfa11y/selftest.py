"""Installation self-test: `pdfa11y selftest`.

Confirms that every part the tool depends on works on THIS machine (libraries, PDF rendering, the tagging
pipeline, the bundled Java + veraPDF validator, and the desktop-app libraries). Safe to run anywhere: it only
creates and deletes its own temporary files.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path


def _make_test_pdf(path: Path) -> None:
    import pikepdf
    from pikepdf import Dictionary, Name

    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(595, 842))
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))
    page.Resources = Dictionary(Font=Dictionary(F1=font))
    lines = ["Self-test document heading"]
    body = ("This paragraph exists only so the self-test has something to tag. It is ordinary body text in the "
            "size a report would use, repeated across a few lines so that the layout analysis can tell headings "
            "from paragraphs and detect the language of the text on the page.")
    stream = b"BT /F1 26 Tf 72 760 Td (" + lines[0].encode() + b") Tj ET\n"
    y = 710
    for i in range(6):
        stream += b"BT /F1 11 Tf 72 " + str(y).encode() + b" Td (" + body.encode() + b") Tj ET\n"
        y -= 16
    page.Contents = pdf.make_stream(stream)
    pdf.save(path)


def run(deep_pdf: str | None = None) -> int:
    from . import __version__

    results: list[tuple[str, bool, str]] = []

    def check(name: str, fn) -> None:
        t = time.time()
        try:
            detail = fn() or ""
            results.append((name, True, f"{detail}  ({time.time() - t:.1f}s)".strip()))
        except Exception as e:  # noqa: BLE001
            results.append((name, False, f"{type(e).__name__}: {e}"))

    ctx: dict = {}

    def libs() -> str:
        import pdfminer
        import pikepdf
        import pypdfium2
        return f"pikepdf {pikepdf.__version__}, pypdfium2 {pypdfium2.version.PYPDFIUM_INFO}, pdfminer {pdfminer.__version__}"

    def tools() -> str:
        from . import verapdf as vp
        if not vp.available():
            raise RuntimeError(f"Java/veraPDF not found under {vp.tools_dir()}")
        return f"found under {vp.tools_dir()}"

    def pipeline() -> str:
        from .batch import process_one
        td = tempfile.mkdtemp(prefix="pdfa11y_selftest_")
        ctx["td"] = Path(td)
        src, dst = Path(td) / "test.pdf", Path(td) / "out" / "test.pdf"
        _make_test_pdf(src)
        r = process_one(str(src), str(dst), verify=True, validate=True)
        if r["status"] in ("Failed", "Skipped") or not dst.exists():
            raise RuntimeError(r.get("error") or r["status"])
        ctx["out"] = dst
        return f"tagged and fixed a test document ({r['action']})"

    def render() -> str:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(ctx["out"]))
        img = pdf[0].render(scale=0.5).to_pil()
        pdf.close()
        return f"rendered a page at {img.width}x{img.height}"

    def validator() -> str:
        from . import verapdf as vp
        v = vp.validate(ctx["out"])
        return f"veraPDF ran and reported {v['failed_checks']} failed check(s) on the test file"

    def gui() -> str:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            return "not installed (optional: only the desktop window needs it)"

        from .gui.main import MainWindow
        app = QApplication.instance() or QApplication([])
        w = MainWindow()
        w.show()
        app.processEvents()
        w.close()
        return "desktop-app libraries load"

    def deep() -> str:
        """Open a real PDF in the desktop app (offscreen), wait for processing, and load the page view."""
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import QEventLoop, QTimer
        from PySide6.QtWidgets import QApplication

        from .gui.main import MainWindow
        app = QApplication.instance() or QApplication([])
        w = MainWindow()
        w.resize(1300, 800)
        w.show()
        w.open_file(deep_pdf)
        loop, state = QEventLoop(), {"t": 0.0}
        timer = QTimer()
        timer.setInterval(200)

        def tick():
            state["t"] += 0.2
            if w.stack.currentWidget() in (w.work, w.message) or state["t"] > 300:
                loop.quit()

        timer.timeout.connect(tick)
        timer.start()
        loop.exec()
        timer.stop()
        if w.stack.currentWidget() is not w.work:
            raise RuntimeError("the file did not open in the app: " + w.message.body.text()[:200])
        wp = w.work
        wp.tabs.setCurrentIndex(wp.pages_tab_index)
        app.processEvents()
        wp.pageview.goto(min(2, len(wp.pageview.doc) - 1))
        app.processEvents()
        n_fig, n_blocks = len(w.session.figures), len(wp.pageview.blocks)
        status = w.session.result.get("status")
        w.session._state_file().unlink(missing_ok=True)
        wp.pageview.unload()
        w.session.close()
        w.close()
        return f"opened {Path(deep_pdf).name}: {status}; {n_fig} image(s) to review; page view shows {n_blocks} block(s)"

    def web() -> str:
        """Start the browser version on a free port, upload the test PDF over HTTP, wait for it to be processed."""
        import http.client
        import json
        import uuid

        from .web.server import serve_in_thread, stop_thread_server
        h = serve_in_thread(Path(ctx["td"]) / "webdata")
        t0 = time.time()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", h["port"], timeout=60)
            conn.request("GET", f"/?token={h['token']}")
            r = conn.getresponse()
            r.read()
            cookie = (r.getheader("set-cookie") or "").split(";")[0]
            if r.status != 303 or not cookie:
                raise RuntimeError(f"sign-in step returned HTTP {r.status}")
            hdr = {"Cookie": cookie}
            conn.request("GET", "/api/info", headers=hdr)
            info = json.loads(conn.getresponse().read())
            boundary = uuid.uuid4().hex
            pdf = (Path(ctx["td"]) / "test.pdf").read_bytes()
            crlf = bytes([13, 10])                                    # the line ending HTTP multipart bodies use
            body = (b"--" + boundary.encode() + crlf
                    + b'Content-Disposition: form-data; name="files"; filename="test.pdf"' + crlf
                    + b"Content-Type: application/pdf" + crlf + crlf
                    + pdf + crlf + b"--" + boundary.encode() + b"--" + crlf)
            conn.request("POST", "/api/documents", body=body,
                         headers={**hdr, "Content-Type": f"multipart/form-data; boundary={boundary}"})
            doc_id = json.loads(conn.getresponse().read())["results"][0]["id"]
            status = "processing"
            for _ in range(180):
                conn.request("GET", f"/api/documents/{doc_id}", headers=hdr)
                status = json.loads(conn.getresponse().read())["document"]["status"]
                if status != "processing":
                    break
                time.sleep(1)
            if status != "ready":
                raise RuntimeError(f"the test document ended as '{status}'")
            return f"started (version {info['version']}), accepted an upload and processed it in {time.time() - t0:.0f}s"
        finally:
            stop_thread_server(h)

    check("Python libraries", libs)
    check("Bundled tools (Java + veraPDF)", tools)
    check("Tag and fix a test PDF", pipeline)
    if "out" in ctx:
        check("Render a page", render)
        check("Validate with veraPDF", validator)
        check("Browser version", web)
    check("Desktop app", gui)
    if deep_pdf:
        check("Open a real PDF in the app", deep)

    print(f"PDF Accessibility Studio {__version__} · self-test")
    print(f"Python {sys.version.split()[0]} · {'packaged app' if getattr(sys, 'frozen', False) else 'development install'}")
    print("-" * 72)
    for name, ok, detail in results:
        print(f"[{'OK' if ok else 'FAIL'}]  {name:<34} {detail}")
    print("-" * 72)
    if "td" in ctx:
        import shutil
        shutil.rmtree(ctx["td"], ignore_errors=True)
    failed = [n for n, ok, _ in results if not ok]
    print("All checks passed." if not failed else f"{len(failed)} check(s) failed: {', '.join(failed)}")
    return 1 if failed else 0
