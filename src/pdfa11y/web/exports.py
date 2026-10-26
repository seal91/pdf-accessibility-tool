"""Bulk export: finalize many documents in the background and bundle the results in a zip with a report."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath

from . import report
from .store import DocStore


def signature(session) -> str:
    """Fingerprint of everything that goes into an exported file. Same fingerprint = the old export is still right."""
    blob = json.dumps({"d": session.decisions, "t": session.title, "l": session.lang,
                       "e": session.editor.ops if session.editor else []}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def export_one(store: DocStore, doc_id: str, allow_incomplete: bool = False) -> dict:
    """Produce (or reuse) the exported PDF for one document. Returns the new meta['export'] plus 'remaining'."""
    loaded = store.session(doc_id)
    d = store.dir(doc_id)
    with loaded.lock:
        s = loaded.session
        remaining = s.remaining()
        if remaining and not allow_incomplete:
            return {"skipped": True, "remaining": remaining}
        sig = signature(s)
        meta = store.read_meta(doc_id) or {}
        exp = meta.get("export") or {}
        if exp.get("sig") == sig and exp.get("incomplete") == bool(remaining) and (d / "export.pdf").exists():
            return {**exp, "remaining": remaining, "reused": True}
        r = s.finalize(d / "export.pdf")
        new = {"compliant": r["compliant"], "stamped": r["stamped"], "time": time.time(), "sig": sig,
               "incomplete": bool(remaining)}
        store.update_meta(doc_id, export=new)
        return {**new, "remaining": remaining, "reused": False}


class ExportManager:
    def __init__(self, store: DocStore):
        self.store = store
        self.dir = store.root / "exports"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._tidy()

    def _tidy(self) -> None:
        """Remove export bundles older than a day."""
        cutoff = time.time() - 86400
        for f in self.dir.glob("*.zip"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass

    def start(self, ids: list[str], include_incomplete: bool = False, suffix: bool = False) -> str:
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "state": "running", "total": len(ids), "done": 0, "items": [], "error": None,
               "download": None, "started": time.time()}
        with self._lock:
            self.jobs[job_id] = job
        self._pool.submit(self._run, job, list(ids), include_incomplete, suffix)
        return job_id

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            j = self.jobs.get(job_id)
            return json.loads(json.dumps(j)) if j else None

    def is_busy(self) -> bool:
        with self._lock:
            return any(j["state"] == "running" for j in self.jobs.values())

    def zip_path(self, job_id: str):
        return self.dir / f"{job_id}.zip"

    def _run(self, job: dict, ids: list[str], include_incomplete: bool, suffix: bool) -> None:
        used: set[str] = set()
        extra: dict[str, dict] = {}
        docs: list[dict] = []
        try:
            with zipfile.ZipFile(self.zip_path(job["id"]), "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                for doc_id in ids:
                    meta = self.store.read_meta(doc_id)
                    item = {"id": doc_id, "name": (meta or {}).get("name", "?"), "outcome": "skipped", "passes": None, "message": ""}
                    try:
                        if not meta or meta.get("status") != "ready":
                            item["message"] = "Not ready: " + ((meta or {}).get("status") or "missing")
                        else:
                            res = export_one(self.store, doc_id, include_incomplete)
                            if res.get("skipped"):
                                n = res["remaining"]
                                item["message"] = f"{n} image description{'s' if n != 1 else ''} still needed"
                            else:
                                stem = PurePosixPath(meta["name"]).stem
                                tag = (" (accessible)" if suffix else "") + (" (work in progress)" if res.get("incomplete") else "")
                                arc = "/".join(p for p in (meta.get("folder", ""), f"{stem}{tag}.pdf") if p)
                                base, n = arc, 2
                                while arc.lower() in used:
                                    arc = base[:-4] + f" ({n}).pdf"
                                    n += 1
                                used.add(arc.lower())
                                zf.write(self.store.dir(doc_id) / "export.pdf", arc)
                                item.update(outcome="exported", passes=bool(res.get("compliant")))
                                item["message"] = ("Passes the PDF/UA-1 check" if res.get("compliant") else "Work in progress: does not pass yet") + \
                                                  (" (reused earlier export)" if res.get("reused") else "")
                    except Exception as e:  # noqa: BLE001
                        item.update(outcome="failed", message=f"{type(e).__name__}: {e}")
                    with self._lock:
                        job["items"].append(item)
                        job["done"] += 1
                    extra[doc_id] = item
                    fresh = self.store.read_meta(doc_id)
                    if fresh:
                        docs.append(fresh)
                zf.writestr("report.csv", report.to_csv(docs, extra))
                zf.writestr("report.html", report.to_html(docs, "PDF accessibility export report", extra))
            with self._lock:
                job["state"] = "done"
                job["download"] = f"/api/exports/{job['id']}/download"
        except Exception as e:  # noqa: BLE001
            with self._lock:
                job["state"] = "failed"
                job["error"] = f"{type(e).__name__}: {e}"

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
