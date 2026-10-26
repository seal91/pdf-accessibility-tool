"""Document store: one permanent folder per PDF.

    <root>/docs/<id>/source.pdf   the uploaded original (never modified)
                     work.pdf     the tagged + fixed working copy
                     result.json  full processing result
                     state.json   the person's decisions, edits, title/language
                     meta.json    small summary used by the library list
                     export.pdf   the last finalized (downloadable) file
                     cache/       rendered image crops
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from ..batch import process_one
from ..session import Session

ID_RE = re.compile(r"^[0-9a-f]{12}$")


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "pdfa11y" / "web"


def confirm_items(result: dict) -> list[str]:
    """The 'things to confirm' list: what the tool guessed or wants a person to look at."""
    return [("Confirm: " + a) for a in result.get("assumptions", [])] + [("Check: " + n) for n in result.get("review_notes", [])]


@dataclass
class Loaded:
    session: Session
    lock: threading.RLock = field(default_factory=threading.RLock)


class NotPDF(ValueError):
    pass


class DocStore:
    def __init__(self, root: Path, max_loaded: int = 4, workers: int = 2, max_upload_bytes: int = 150 * 1024 * 1024):
        self.root = Path(root)
        (self.root / "docs").mkdir(parents=True, exist_ok=True)
        self.max_upload_bytes = max_upload_bytes
        self.workers = workers
        self.max_loaded = max_loaded
        self._lock = threading.RLock()
        self._loaded: OrderedDict[str, Loaded] = OrderedDict()
        self._pool: ProcessPoolExecutor | None = None

    # ------------------------------------------------------------------ paths and metadata
    def valid(self, doc_id: str) -> bool:
        return bool(ID_RE.match(doc_id or "")) and self.dir(doc_id).is_dir()

    def dir(self, doc_id: str) -> Path:
        return self.root / "docs" / doc_id

    def read_meta(self, doc_id: str) -> dict | None:
        f = self.dir(doc_id) / "meta.json"
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def write_meta(self, doc_id: str, meta: dict) -> None:
        meta["updated"] = time.time()
        f = self.dir(doc_id) / "meta.json"
        tmp = f.with_suffix(".tmp")
        with self._lock:
            tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(f)

    def update_meta(self, doc_id: str, **changes) -> dict:
        with self._lock:
            meta = self.read_meta(doc_id) or {}
            meta.update(changes)
            self.write_meta(doc_id, meta)
            return meta

    def list(self) -> list[dict]:
        out = []
        for d in (self.root / "docs").iterdir():
            if d.is_dir() and ID_RE.match(d.name):
                m = self.read_meta(d.name)
                if m:
                    out.append(m)
        return sorted(out, key=lambda m: m.get("created", 0), reverse=True)

    # ------------------------------------------------------------------ creating and processing
    @staticmethod
    def clean_folder(folder: str | None) -> str:
        """A safe, relative folder label from what the browser reports (e.g. 'Reports/2025'). Never contains '..'."""
        parts = [p for p in re.split(r"[\\/]+", folder or "") if p and p not in (".", "..")]
        return "/".join(parts)[:300]

    def create(self, filename: str, stream: BinaryIO, folder: str = "") -> dict:
        """Save an uploaded PDF and queue it for processing. Raises NotPDF / ValueError(too large)."""
        doc_id = uuid.uuid4().hex[:12]
        d = self.dir(doc_id)
        d.mkdir(parents=True)
        src = d / "source.pdf"
        try:
            head = stream.read(1024)
            if b"%PDF-" not in head:
                raise NotPDF("This does not look like a PDF file.")
            size = len(head)
            with open(src, "wb") as out:
                out.write(head)
                while True:
                    chunk = stream.read(1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_upload_bytes:
                        raise ValueError(f"The file is larger than the {self.max_upload_bytes // (1024 * 1024)} MB limit.")
                    out.write(chunk)
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise
        name = Path(filename or "document.pdf").name[:200] or "document.pdf"
        meta = {"id": doc_id, "name": name, "folder": self.clean_folder(folder), "created": time.time(), "status": "processing", "error": None,
                "size": size, "pages": None, "summary": {}, "figures_total": 0, "figures_remaining": 0,
                "confirm_total": 0, "confirm_done": 0, "export": None}
        self.write_meta(doc_id, meta)
        self.submit(doc_id)
        return meta

    def _executor(self) -> ProcessPoolExecutor:
        with self._lock:
            if self._pool is None:
                self._pool = ProcessPoolExecutor(max_workers=self.workers)
            return self._pool

    def submit(self, doc_id: str) -> None:
        d = self.dir(doc_id)
        self.update_meta(doc_id, status="processing", error=None)
        fut = self._executor().submit(process_one, str(d / "source.pdf"), str(d / "work.pdf"), True, True)
        fut.add_done_callback(lambda f, i=doc_id: self._finished(i, f))

    def _finished(self, doc_id: str, fut: Future) -> None:
        try:
            res = fut.result()
        except Exception as e:  # noqa: BLE001  (worker crashed)
            res = {"status": "Failed", "error": f"{type(e).__name__}: {e}", "issues": [], "assumptions": [],
                   "review_notes": [], "figures": [], "changes": []}
        d = self.dir(doc_id)
        if not d.is_dir():                                 # deleted while processing
            return
        (d / "result.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
        ok = res.get("status") not in ("Failed", "Skipped") and (d / "work.pdf").exists()
        status = "ready" if ok else ("skipped" if res.get("status") == "Skipped" else "failed")
        summary = {k: res.get(k) for k in ("action", "language", "title", "issues", "assumptions", "review_notes",
                                           "verapdf", "stamped", "seconds") if k in res}
        self.update_meta(doc_id, status=status, error=None if ok else res.get("error"), pages=res.get("pages"),
                         summary=summary, confirm_total=len(confirm_items(res)), confirm_done=0)
        if ok:
            try:
                s = self.session(doc_id)
                with s.lock:
                    self.refresh_counts(doc_id, s.session)
            except Exception:  # noqa: BLE001
                pass

    def resume_pending(self) -> int:
        """After a restart, queue anything that was still processing."""
        n = 0
        for m in self.list():
            if m.get("status") == "processing":
                self.submit(m["id"])
                n += 1
        return n

    def retry(self, doc_id: str) -> None:
        self.drop_loaded(doc_id)
        d = self.dir(doc_id)
        for f in ("work.pdf", "result.json", "export.pdf"):
            (d / f).unlink(missing_ok=True)
        self.submit(doc_id)

    # ------------------------------------------------------------------ sessions
    def session(self, doc_id: str) -> Loaded:
        with self._lock:
            if doc_id in self._loaded:
                self._loaded.move_to_end(doc_id)
                return self._loaded[doc_id]
            d = self.dir(doc_id)
            result = json.loads((d / "result.json").read_text(encoding="utf-8"))
            s = Session(d / "source.pdf", workdir=d)
            s.open_existing(result)
            loaded = Loaded(s)
            self._loaded[doc_id] = loaded
            while len(self._loaded) > self.max_loaded:
                _, old = self._loaded.popitem(last=False)
                with old.lock:
                    old.session.close()
            return loaded

    def drop_loaded(self, doc_id: str) -> None:
        with self._lock:
            old = self._loaded.pop(doc_id, None)
        if old:
            with old.lock:
                old.session.close()

    def refresh_counts(self, doc_id: str, s: Session) -> dict:
        items = confirm_items(s.result)
        return self.update_meta(doc_id, figures_total=len(s.figures), figures_remaining=s.remaining(),
                                confirm_total=len(items), confirm_done=sum(1 for t in items if s.checked.get(t)))

    # ------------------------------------------------------------------ housekeeping
    def delete(self, doc_id: str) -> None:
        self.drop_loaded(doc_id)
        shutil.rmtree(self.dir(doc_id), ignore_errors=True)

    def purge_older_than(self, days: float) -> int:
        """Delete documents not touched for `days` days (privacy: unpublished files do not linger)."""
        cutoff = time.time() - days * 86400
        n = 0
        for m in self.list():
            if m.get("updated", m.get("created", 0)) < cutoff and m.get("status") != "processing":
                self.delete(m["id"])
                n += 1
        return n

    def shutdown(self) -> None:
        with self._lock:
            for k in list(self._loaded):
                self.drop_loaded(k)
            if self._pool:
                self._pool.shutdown(wait=False, cancel_futures=True)
