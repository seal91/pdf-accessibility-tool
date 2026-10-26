"""A remediation session for one PDF: the working copy, the person's decisions, saving and finishing.

No GUI code here, so it can be tested and reused (e.g. by a future command-line review mode).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import verapdf as vp
from .batch import process_one
from .editor import Editor
from .figures import FigureItem, list_figures


def state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    d = Path(base) / "pdfa11y" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fingerprint(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Session:
    source: Path
    tmp: Path | None = None
    work: Path | None = None
    result: dict = field(default_factory=dict)
    figures: list[FigureItem] = field(default_factory=list)
    decisions: dict[str, dict] = field(default_factory=dict)     # figure key -> {"alt": str} | {"decorative": True}
    title: str = ""
    lang: str = ""
    checked: dict[str, bool] = field(default_factory=dict)       # "things to confirm" the person has ticked off
    fingerprint: str = ""
    editor: Editor | None = None
    workdir: Path | None = None          # permanent folder (web mode); None = temporary folder (desktop app)

    # ------------------------------------------------------------------ lifecycle
    def process(self) -> dict:
        """Tag/fix the source into a private working copy. Slow: run off the GUI thread."""
        self.fingerprint = _fingerprint(self.source)
        self.tmp = Path(tempfile.mkdtemp(prefix="pdfa11y_"))
        self.work = self.tmp / "work.pdf"
        self.result = process_one(str(self.source), str(self.work), verify=True, validate=True)
        if self.result.get("status") in ("Failed", "Skipped") or not self.work.exists():
            return self.result
        self._attach()
        return self.result

    def open_existing(self, result: dict) -> dict:
        """Attach to a working copy that was already produced in `self.workdir` (web mode)."""
        if self.workdir is None:
            raise ValueError("open_existing needs a workdir")
        self.work = self.workdir / "work.pdf"
        self.result = result
        self._attach()
        return self.result

    def _attach(self) -> None:
        self.figures = list_figures(self.work)
        self.editor = Editor(self.work)
        self.title = self.result.get("title") or ""
        self.lang = self.result.get("language") or ""
        self.load_state()
        # an alt text that already exists in the file counts as a decision
        for it in self.figures:
            if it.alt and it.key not in self.decisions:
                self.decisions[it.key] = {"alt": it.alt, "existing": True}

    def close(self) -> None:
        if self.editor:
            self.editor.close()
            self.editor = None
        if self.tmp and self.tmp.exists():
            shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ decisions
    def set_alt(self, key: str, text: str) -> None:
        text = text.strip()
        if text:
            self.decisions[key] = {"alt": text}
        else:
            self.decisions.pop(key, None)
        self.save_state()

    def set_decorative(self, key: str, on: bool = True) -> None:
        if on:
            self.decisions[key] = {"decorative": True}
        else:
            self.decisions.pop(key, None)
        self.save_state()

    def status_of(self, key: str) -> str:
        d = self.decisions.get(key)
        if not d:
            return "todo"
        return "decorative" if d.get("decorative") else "done"

    def remaining(self) -> int:
        return sum(1 for f in self.figures if self.status_of(f.key) == "todo")

    # ------------------------------------------------------------------ persistence (resume later)
    def _state_file(self) -> Path:
        if self.workdir is not None:
            return self.workdir / "state.json"
        return state_dir() / f"{self.fingerprint}.json"

    def save_state(self) -> None:
        try:
            self._state_file().write_text(json.dumps({
                "source": str(self.source), "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
                "decisions": {k: v for k, v in self.decisions.items() if not v.get("existing")},
                "title": self.title, "lang": self.lang, "checked": self.checked,
                "edits": self.editor.ops if self.editor else [],
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass                                   # never lose work because a state file could not be written

    def load_state(self) -> None:
        f = self._state_file()
        if not f.exists():
            return
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.decisions.update(d.get("decisions", {}))
        self.title = d.get("title") or self.title
        self.lang = d.get("lang") or self.lang
        self.checked = d.get("checked", {})
        if self.editor and d.get("edits"):
            self.editor.load_ops(d["edits"])

    # ------------------------------------------------------------------ finishing
    def finalize(self, dst: str | Path, stamp: bool = True) -> dict:
        """Apply decisions, validate with veraPDF, add the PDF/UA identifier if (and only if) nothing else fails,
        and write `dst`. Returns {'compliant', 'failed_rules', 'stamped', 'applied', 'path'}."""
        dst = Path(dst)
        with tempfile.TemporaryDirectory(prefix="pdfa11y_fin_") as td:
            edited = Path(td) / "edited.pdf"
            applied = self.editor.save(edited, self.decisions, title=self.title or None, lang=self.lang or None)
            final, stamped = edited, False
            verdict = {"compliant": None, "failed_rules": [], "failed_checks": 0}
            if vp.available():
                verdict = vp.validate(edited)
                if stamp and not verdict["compliant"] and vp.is_identifier_only(verdict):
                    st = Path(td) / "stamped.pdf"
                    vp.stamp_pdfua(edited, st)
                    v2 = vp.validate(st)
                    if v2["compliant"]:
                        final, verdict, stamped = st, v2, True
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(final, dst)
        return {"compliant": verdict["compliant"], "failed_rules": verdict["failed_rules"], "stamped": stamped,
                "applied": applied, "path": str(dst)}

    def check_now(self) -> dict:
        """Validate the current state without keeping a file."""
        with tempfile.TemporaryDirectory(prefix="pdfa11y_chk_") as td:
            return self.finalize(Path(td) / "check.pdf")
