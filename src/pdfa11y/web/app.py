"""The web application (Starlette). JSON API under /api, static single-page app everywhere else."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__
from .. import verapdf as vp
from . import render, report
from .exports import ExportManager, signature
from .store import DocStore, NotPDF, confirm_items

STATIC = Path(__file__).parent / "static"
COOKIE = "pdfa11y_auth"
LANG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


# --------------------------------------------------------------------------- security

class Guard(BaseHTTPMiddleware):
    """Host/Origin checks, authentication (token or password), and security headers."""

    async def dispatch(self, request: Request, call_next):
        st = request.app.state
        if request.url.path.startswith("/api/"):
            st.last_seen = time.time()                           # the page's heartbeat keeps the tool alive
        host = request.headers.get("host", "")
        if st.allowed_hosts is not None and host not in st.allowed_hosts:
            return PlainText("Unexpected host name.", 400)

        path = request.url.path
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != host:
                return PlainText("Cross-site request refused.", 403)

        if st.auth_mode != "none" and not path.startswith("/static/") and path != "/login":
            if not self._authenticated(request):
                tok = request.query_params.get("token")
                if st.auth_mode == "token" and tok and hmac.compare_digest(tok, st.token):
                    clean = request.url.path
                    resp = RedirectResponse(clean or "/", status_code=303)
                    resp.set_cookie(COOKIE, st.cookie_value, httponly=True, samesite="strict", path="/")
                    return self._headers(resp)
                if path.startswith("/api/"):
                    return self._headers(JSONResponse({"error": "Not signed in."}, status_code=401))
                if st.auth_mode == "password":
                    return self._headers(RedirectResponse("/login", status_code=303))
                return self._headers(HTMLResponse(
                    "<!doctype html><meta charset=utf-8><title>PDF Accessibility Studio</title>"
                    "<h1>Please use the link you were given</h1>"
                    "<p>Open PDF Accessibility Studio with the address it printed when it started "
                    "(it contains a one-time key).</p>", status_code=401))
        return self._headers(await call_next(request))

    @staticmethod
    def _authenticated(request: Request) -> bool:
        st = request.app.state
        got = request.cookies.get(COOKIE, "")
        return bool(got) and hmac.compare_digest(got, st.cookie_value)

    @staticmethod
    def _headers(resp: Response) -> Response:
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp


def PlainText(text: str, status: int) -> Response:
    return Response(text, status_code=status, media_type="text/plain")


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · PDF Accessibility Studio</title><link rel="stylesheet" href="/static/style.css"></head>
<body class="login"><main><h1>PDF Accessibility Studio</h1>
<form method="post" action="/login"><label for="pw">Password</label>
<input id="pw" name="password" type="password" autocomplete="current-password" required autofocus>
{error}<button class="primary" type="submit">Sign in</button></form></main></body></html>"""


async def login(request: Request) -> Response:
    st = request.app.state
    if st.auth_mode != "password":
        return RedirectResponse("/", status_code=303)
    ip = request.client.host if request.client else "?"
    if request.method == "POST":
        now = time.time()
        fails = [t for t in st.failures.get(ip, []) if now - t < 300]
        if len(fails) >= 10:
            return HTMLResponse(LOGIN_HTML.format(error='<p role="alert">Too many attempts. Wait a few minutes.</p>'), status_code=429)
        form = await request.form()
        if hmac.compare_digest(str(form.get("password", "")), st.password):
            st.failures.pop(ip, None)
            resp = RedirectResponse("/", status_code=303)
            https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"
            resp.set_cookie(COOKIE, st.cookie_value, httponly=True, samesite="strict", path="/", secure=https)
            return resp
        fails.append(now)
        st.failures[ip] = fails
        return HTMLResponse(LOGIN_HTML.format(error='<p role="alert">That password is not right.</p>'), status_code=401)
    return HTMLResponse(LOGIN_HTML.format(error=""))


# --------------------------------------------------------------------------- helpers

def J(data, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def err(msg: str, status: int = 400) -> JSONResponse:
    return J({"error": msg}, status)


def store_of(request: Request) -> DocStore:
    return request.app.state.store


def public_meta(m: dict) -> dict:
    return {k: m.get(k) for k in ("id", "name", "folder", "created", "updated", "status", "error", "size", "pages",
                                  "figures_total", "figures_remaining", "confirm_total", "confirm_done", "export")}


def verdict_json(v: dict) -> dict:
    return {"compliant": v.get("compliant"), "failed_checks": v.get("failed_checks", 0),
            "failed_rules": [{"clause": r["clause"], "test": r["test"], "count": r["count"],
                              "message": r.get("friendly") or r["description"]} for r in v.get("failed_rules", [])]}


async def doc_or_404(request: Request):
    store = store_of(request)
    doc_id = request.path_params["doc_id"]
    if not store.valid(doc_id):
        return None, None, err("That document does not exist.", 404)
    return store, doc_id, None


# --------------------------------------------------------------------------- API: info and library

async def api_info(request: Request) -> Response:
    st = request.app.state
    return J({"version": __version__, "mode": st.mode, "can_quit": st.allow_quit, "validator": vp.available(),
              "max_upload_mb": st.store.max_upload_bytes // (1024 * 1024)})


async def api_list(request: Request) -> Response:
    return J({"documents": [public_meta(m) for m in store_of(request).list()]})


async def api_upload(request: Request) -> Response:
    store = store_of(request)
    form = await request.form(max_files=100, max_fields=20)
    files = [f for f in form.getlist("files") if hasattr(f, "filename") and f.filename]
    if not files:
        return err("Choose at least one PDF file.")
    paths = [str(x) for x in form.getlist("relpaths")]
    results = []
    for i, f in enumerate(files):
        folder = ""
        if len(paths) == len(files) and paths[i]:
            folder = str(paths[i]).rsplit("/", 1)[0] if "/" in paths[i] else ""
        try:
            meta = await run_in_threadpool(store.create, f.filename, f.file, folder)
            results.append({"name": f.filename, "id": meta["id"]})
        except (NotPDF, ValueError) as e:
            results.append({"name": f.filename, "error": str(e)})
    return J({"results": results}, 201 if any("id" in r for r in results) else 400)


# --------------------------------------------------------------------------- API: one document

async def api_doc(request: Request) -> Response:
    store, doc_id, bad = await doc_or_404(request)
    if bad:
        return bad
    meta = store.read_meta(doc_id)
    out = {"document": public_meta(meta), "summary": meta.get("summary", {})}
    if meta.get("status") != "ready":
        return J(out)
    loaded = await run_in_threadpool(store.session, doc_id)
    with loaded.lock:
        s = loaded.session
        items = confirm_items(s.result)
        out.update({
            "title": s.title, "lang": s.lang, "edits": len(s.editor.ops) if s.editor else 0,
            "figures": [{"key": f.key, "page": f.page + 1, "near_text": f.near_text,
                         "status": s.status_of(f.key),
                         "alt": s.decisions.get(f.key, {}).get("alt", ""),
                         "existing": bool(s.decisions.get(f.key, {}).get("existing"))} for f in s.figures],
            "confirm": [{"text": t, "checked": bool(s.checked.get(t))} for t in items],
        })
    v = meta.get("summary", {}).get("verapdf")
    if v:
        out["verdict"] = verdict_json(v)
    return J(out)


async def api_delete(request: Request) -> Response:
    store, doc_id, bad = await doc_or_404(request)
    if bad:
        return bad
    await run_in_threadpool(store.delete, doc_id)
    return J({"ok": True})


async def api_retry(request: Request) -> Response:
    store, doc_id, bad = await doc_or_404(request)
    if bad:
        return bad
    await run_in_threadpool(store.retry, doc_id)
    return J({"ok": True})


async def api_figure_put(request: Request) -> Response:
    store, doc_id, bad = await doc_or_404(request)
    if bad:
        return bad
    key = request.path_params["key"]
    try:
        body = await request.json()
    except ValueError:
        return err("Invalid request.")
    loaded = await run_in_threadpool(store.session, doc_id)
    with loaded.lock:
        s = loaded.session
        if key not in {f.key for f in s.figures}:
            return err("That image does not exist.", 404)
        if body.get("decorative") is True:
            s.set_decorative(key, True)
        else:
            alt = str(body.get("alt", "")).strip()[:2000]
            s.set_alt(key, alt)                               # empty text clears the decision
        meta = store.refresh_counts(doc_id, s)
        return J({"status": s.status_of(key), "figures_remaining": meta["figures_remaining"]})


async def api_meta_put(request: Request) -> Response:
    store, doc_id, bad = await doc_or_404(request)
    if bad:
        return bad
    try:
        body = await request.json()
    except ValueError:
        return err("Invalid request.")
    loaded = await run_in_threadpool(store.session, doc_id)
    with loaded.lock:
        s = loaded.session
        if "title" in body:
            s.title = str(body["title"]).strip()[:300]
        if "lang" in body:
            lang = str(body["lang"]).strip()
            if lang and not LANG_RE.match(lang):
                return err("That is not a valid language code (examples: da, en, es, en-GB).")
            s.lang = lang
        if isinstance(body.get("checked"), dict):
            valid = set(confirm_items(s.result))
            for text, on in body["checked"].items():
                if text in valid:
                    s.checked[text] = bool(on)
        s.save_state()
        store.refresh_counts(doc_id, s)
    return J({"ok": True})


async def api_check(request: Request) -> Response:
    store, doc_id, bad = await doc_or_404(request)
    if bad:
        return bad
    loaded = await run_in_threadpool(store.session, doc_id)

    def work():
        with loaded.lock:
            return loaded.session.check_now()

    try:
        r = await run_in_threadpool(work)
    except Exception as e:  # noqa: BLE001
        return err(f"The check could not be completed: {e}", 500)
    return J({**verdict_json(r), "stamped": r["stamped"]})


async def api_finalize(request: Request) -> Response:
    store, doc_id, bad = await doc_or_404(request)
    if bad:
        return bad
    try:
        body = await request.json()
    except ValueError:
        body = {}
    loaded = await run_in_threadpool(store.session, doc_id)
    d = store.dir(doc_id)

    def work():
        with loaded.lock:
            s = loaded.session
            if s.remaining() and not body.get("allow_incomplete"):
                return None, s.remaining()
            sig, incomplete = signature(s), bool(s.remaining())
            r = s.finalize(d / "export.pdf")
            r["sig"], r["incomplete"] = sig, incomplete
            return r, 0

    try:
        r, remaining = await run_in_threadpool(work)
    except Exception as e:  # noqa: BLE001
        return err(f"The file could not be produced: {e}", 500)
    if r is None:
        return J({"error": f"{remaining} image(s) still have no description.", "remaining": remaining}, 409)
    store.update_meta(doc_id, export={"compliant": r["compliant"], "stamped": r["stamped"], "time": time.time(),
                                      "sig": r["sig"], "incomplete": r["incomplete"]})
    return J({**verdict_json(r), "stamped": r["stamped"], "download": f"/api/documents/{doc_id}/download"})


async def api_download(request: Request) -> Response:
    store, doc_id, bad = await doc_or_404(request)
    if bad:
        return bad
    f = store.dir(doc_id) / "export.pdf"
    if not f.exists():
        return err("Nothing to download yet: validate and save first.", 404)
    stem = Path(store.read_meta(doc_id)["name"]).stem
    return FileResponse(f, media_type="application/pdf", filename=f"{stem} (accessible).pdf",
                        headers={"Cache-Control": "no-store"})


async def api_source(request: Request) -> Response:
    store, doc_id, bad = await doc_or_404(request)
    if bad:
        return bad
    return FileResponse(store.dir(doc_id) / "source.pdf", media_type="application/pdf",
                        filename=store.read_meta(doc_id)["name"], content_disposition_type="inline")


async def api_figure_image(request: Request) -> Response:
    store, doc_id, bad = await doc_or_404(request)
    if bad:
        return bad
    kind = request.query_params.get("kind", "crop")
    if kind not in ("crop", "thumb", "page"):
        return err("Unknown image kind.")
    loaded = await run_in_threadpool(store.session, doc_id)
    key = request.path_params["key"]
    with loaded.lock:
        fig = next((f for f in loaded.session.figures if f.key == key), None)
        work = loaded.session.work
    if fig is None:
        return err("That image does not exist.", 404)
    paths = await run_in_threadpool(render.figure_images, work, fig, store.dir(doc_id) / "cache")
    return FileResponse(paths[kind], media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


# --------------------------------------------------------------------------- API: pages and the tag editor

def _node_json(n) -> dict:
    return {"ref": list(n.ref), "role": n.role, "text": n.text, "is_block": n.is_block,
            "children": [_node_json(c) for c in n.children]}


def _edit_state(ed) -> dict:
    return {"can_undo": ed.can_undo, "can_redo": ed.can_redo, "edits": len(ed.ops)}


def _parse_ref(v):
    if (isinstance(v, list) and len(v) == 2 and all(isinstance(x, int) and not isinstance(x, bool) for x in v)
            and 0 < v[0] < 10_000_000 and 0 <= v[1] < 65536):
        return v
    raise ValueError("bad element reference")


def _parse_edit(body: dict) -> tuple[str, list]:
    """Validate an edit request and turn it into (operation name, arguments). Raises ValueError."""
    from ..editor import TEXT_ROLES
    op = body.get("op")
    a = body.get("args", [])
    if not isinstance(a, list):
        raise ValueError("bad arguments")
    if op == "renumber_headings" and not a:
        return op, []
    if op in ("artifact", "merge_next") and len(a) == 1:
        return op, [_parse_ref(a[0])]
    if op == "set_role" and len(a) == 2 and a[1] in TEXT_ROLES:
        return op, [_parse_ref(a[0]), a[1]]
    if op == "move" and len(a) == 2 and a[1] in (-1, 1) and not isinstance(a[1], bool):
        return op, [_parse_ref(a[0]), a[1]]
    if op == "cell" and len(a) == 2 and a[1] in ("td", "th_col", "th_row"):
        return op, [_parse_ref(a[0]), a[1]]
    if op == "table_headers" and len(a) == 3 and isinstance(a[1], bool) and isinstance(a[2], bool):
        return op, [_parse_ref(a[0]), a[1], a[2]]
    raise ValueError("That edit is not recognised.")


async def api_page(request: Request) -> Response:
    store, doc_id, bad = await doc_or_404(request)
    if bad:
        return bad
    meta = store.read_meta(doc_id)
    try:
        n = int(request.path_params["n"])
    except ValueError:
        return err("Bad page number.")
    if meta.get("status") != "ready" or not (1 <= n <= (meta.get("pages") or 0)):
        return err("That page does not exist.", 404)
    loaded = await run_in_threadpool(store.session, doc_id)

    def work():
        with loaded.lock:
            ed = loaded.session.editor
            g = ed.geometry(n - 1)
            blocks = ed.page_blocks(n - 1)
            return {
                "page": n, "pages": meta["pages"], "width": g.width, "height": g.height, "left": g.left, "top": g.top,
                "blocks": [{"ref": list(b.ref), "role": b.role, "order": b.order, "text": b.text[:300],
                            "bbox": list(b.bbox) if b.bbox else None} for b in blocks],
                "tree": [_node_json(x) for x in ed.page_tree(n - 1)],
                **_edit_state(ed),
            }

    return J(await run_in_threadpool(work))


async def api_page_image(request: Request) -> Response:
    store, doc_id, bad = await doc_or_404(request)
    if bad:
        return bad
    meta = store.read_meta(doc_id)
    try:
        n = int(request.path_params["n"])
        scale = float(request.query_params.get("scale", "2"))
    except ValueError:
        return err("Bad request.")
    if meta.get("status") != "ready" or not (1 <= n <= (meta.get("pages") or 0)):
        return err("That page does not exist.", 404)
    path = await run_in_threadpool(render.page_image, store.dir(doc_id) / "work.pdf", n - 1, scale, store.dir(doc_id) / "cache")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


async def api_element(request: Request) -> Response:
    from ..editor import EditError
    store, doc_id, bad = await doc_or_404(request)
    if bad:
        return bad
    try:
        ref = _parse_ref([int(request.query_params["num"]), int(request.query_params.get("gen", "0"))])
    except (KeyError, ValueError):
        return err("Bad element reference.")
    loaded = await run_in_threadpool(store.session, doc_id)

    def work():
        with loaded.lock:
            return loaded.session.editor.describe(ref)

    try:
        return J(await run_in_threadpool(work))
    except EditError as e:
        return err(str(e), 404)


async def api_edit(request: Request) -> Response:
    from ..editor import EditError
    store, doc_id, bad = await doc_or_404(request)
    if bad:
        return bad
    try:
        op, args = _parse_edit(await request.json())
    except (ValueError, TypeError) as e:
        return err(str(e) or "Invalid request.")
    loaded = await run_in_threadpool(store.session, doc_id)

    def work():
        with loaded.lock:
            s = loaded.session
            try:
                s.editor.do(op, *args)
            except EditError:
                raise
            except Exception:
                s.editor._replay()                       # never leave the live copy half-edited
                raise
            s.save_state()
            return _edit_state(s.editor)

    try:
        return J({"ok": True, **await run_in_threadpool(work)})
    except EditError as e:
        return err(str(e), 400)
    except Exception as e:  # noqa: BLE001
        return err(f"That edit could not be applied ({type(e).__name__}). Nothing was changed.", 500)


async def api_history(request: Request) -> Response:
    store, doc_id, bad = await doc_or_404(request)
    if bad:
        return bad
    which = request.path_params["which"]
    if which not in ("undo", "redo"):
        return err("Unknown action.", 404)
    loaded = await run_in_threadpool(store.session, doc_id)

    def work():
        with loaded.lock:
            s = loaded.session
            changed = s.editor.undo() if which == "undo" else s.editor.redo()
            if changed:
                s.save_state()
            return {"changed": changed, **_edit_state(s.editor)}

    return J({"ok": True, **await run_in_threadpool(work)})


# --------------------------------------------------------------------------- API: bulk actions, exports, reports

def _ids_from(body, store: DocStore) -> list[str] | None:
    ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(ids, list) or not ids or len(ids) > 1000 or not all(isinstance(i, str) for i in ids):
        return None
    seen, out = set(), []
    for i in ids:
        if i not in seen and store.valid(i):
            seen.add(i)
            out.append(i)
    return out


async def api_bulk_delete(request: Request) -> Response:
    store = store_of(request)
    try:
        ids = _ids_from(await request.json(), store)
    except ValueError:
        ids = None
    if not ids:
        return err("Select at least one document.")
    for i in ids:
        await run_in_threadpool(store.delete, i)
    return J({"deleted": len(ids)})


async def api_bulk_retry(request: Request) -> Response:
    store = store_of(request)
    try:
        ids = _ids_from(await request.json(), store)
    except ValueError:
        ids = None
    if not ids:
        return err("Select at least one document.")
    n = 0
    for i in ids:
        m = store.read_meta(i)
        if m and m.get("status") == "failed":
            await run_in_threadpool(store.retry, i)
            n += 1
    return J({"retried": n})


async def api_export_start(request: Request) -> Response:
    store = store_of(request)
    try:
        body = await request.json()
        ids = _ids_from(body, store)
    except ValueError:
        return err("Invalid request.")
    if not ids:
        return err("Select at least one document.")
    job = request.app.state.exports.start(ids, bool(body.get("include_incomplete")), bool(body.get("suffix")))
    return J({"job": job}, 202)


async def api_export_get(request: Request) -> Response:
    job = request.app.state.exports.get(request.path_params["job"])
    if job is None:
        return err("That export does not exist.", 404)
    return J(job)


async def api_export_download(request: Request) -> Response:
    ex = request.app.state.exports
    job = ex.get(request.path_params["job"])
    if job is None or job["state"] != "done" or not ex.zip_path(job["id"]).exists():
        return err("That export is not ready.", 404)
    return FileResponse(ex.zip_path(job["id"]), media_type="application/zip", filename="accessible-pdfs.zip",
                        headers={"Cache-Control": "no-store"})


async def api_report(request: Request) -> Response:
    store = store_of(request)
    fmt = request.query_params.get("format", "html")
    if fmt not in ("html", "csv"):
        return err("Unknown format.")
    wanted = {i for i in request.query_params.get("ids", "").split(",") if i}
    docs = [m for m in store.list() if not wanted or m["id"] in wanted]
    if fmt == "csv":
        return Response(report.to_csv(docs), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=accessibility-report.csv"})
    return Response(report.to_html(docs), media_type="text/html; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=accessibility-report.html"})


async def api_quit(request: Request) -> Response:
    st = request.app.state
    if not st.allow_quit:
        return err("Not available in this mode.", 403)
    st.exports.shutdown()
    st.store.shutdown()
    if getattr(st, "server", None) is not None:
        st.server.should_exit = True
    return J({"ok": True})


async def index(request: Request) -> Response:
    return FileResponse(STATIC / "index.html", media_type="text/html")


# --------------------------------------------------------------------------- factory

def create_app(store: DocStore, mode: str = "local", auth_mode: str = "token", token: str | None = None,
               password: str | None = None, allowed_hosts: set[str] | None = None, allow_quit: bool = False) -> Starlette:
    routes = [
        Route("/", index),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/api/info", api_info),
        Route("/api/documents", api_list, methods=["GET"]),
        Route("/api/documents", api_upload, methods=["POST"]),
        Route("/api/documents/{doc_id}", api_doc, methods=["GET"]),
        Route("/api/documents/{doc_id}", api_delete, methods=["DELETE"]),
        Route("/api/documents/{doc_id}/retry", api_retry, methods=["POST"]),
        Route("/api/documents/{doc_id}/meta", api_meta_put, methods=["PUT"]),
        Route("/api/documents/{doc_id}/figures/{key}", api_figure_put, methods=["PUT"]),
        Route("/api/documents/{doc_id}/figures/{key}/image", api_figure_image, methods=["GET"]),
        Route("/api/documents/{doc_id}/pages/{n}", api_page, methods=["GET"]),
        Route("/api/documents/{doc_id}/pages/{n}/image", api_page_image, methods=["GET"]),
        Route("/api/documents/{doc_id}/element", api_element, methods=["GET"]),
        Route("/api/documents/{doc_id}/edits", api_edit, methods=["POST"]),
        Route("/api/documents/{doc_id}/history/{which}", api_history, methods=["POST"]),
        Route("/api/documents/{doc_id}/check", api_check, methods=["POST"]),
        Route("/api/documents/{doc_id}/finalize", api_finalize, methods=["POST"]),
        Route("/api/documents/{doc_id}/download", api_download, methods=["GET"]),
        Route("/api/documents/{doc_id}/source", api_source, methods=["GET"]),
        Route("/api/bulk/delete", api_bulk_delete, methods=["POST"]),
        Route("/api/bulk/retry", api_bulk_retry, methods=["POST"]),
        Route("/api/exports", api_export_start, methods=["POST"]),
        Route("/api/exports/{job}", api_export_get, methods=["GET"]),
        Route("/api/exports/{job}/download", api_export_download, methods=["GET"]),
        Route("/api/report", api_report, methods=["GET"]),
        Route("/api/quit", api_quit, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    app = Starlette(routes=routes, middleware=[Middleware(Guard)])
    st = app.state
    st.store, st.mode, st.auth_mode, st.allow_quit = store, mode, auth_mode, allow_quit
    st.exports = ExportManager(store)
    st.allowed_hosts, st.token, st.password = allowed_hosts, token or "", password or ""
    secret = secrets.token_bytes(32)                          # per-start secret: a restart signs everyone out
    st.cookie_value = hmac.new(secret, b"pdfa11y-auth", hashlib.sha256).hexdigest()
    st.failures = {}
    st.last_seen = time.time()
    st.server = None
    return app
