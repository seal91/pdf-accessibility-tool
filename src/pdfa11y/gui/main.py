"""PDF Accessibility Studio: the desktop app.

Milestone 1: open a PDF -> automatic remediation -> alt-text queue -> document details -> validate and save.
The app itself is built to be usable with keyboard and screen reader (labels, tab order, no colour-only cues).
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSplitter,
                               QStackedWidget, QTabWidget, QTextBrowser, QVBoxLayout, QWidget)

from ..session import Session
from .pageview import PageView

APP_NAME = "PDF Accessibility Studio"

STYLE = """
* { font-size: 10.5pt; }
QMainWindow, QWidget#page { background: #f6f7f9; }
QPushButton { padding: 6px 14px; border: 1px solid #8a93a3; border-radius: 4px; background: #ffffff; color: #1b1f27; }
QPushButton:hover { background: #eef1f6; }
QPushButton:focus { border: 2px solid #1d4ed8; }
QPushButton#primary { background: #1d4ed8; color: #ffffff; border: 1px solid #1d4ed8; font-weight: 600; }
QPushButton#primary:hover { background: #1e40af; }
QPushButton#primary:disabled { background: #9db3ea; border-color: #9db3ea; }
QPlainTextEdit, QLineEdit, QComboBox { background: #ffffff; border: 1px solid #8a93a3; border-radius: 4px; padding: 4px; color: #1b1f27; }
QPlainTextEdit:focus, QLineEdit:focus, QComboBox:focus { border: 2px solid #1d4ed8; }
QListWidget { background: #ffffff; border: 1px solid #b9c0cc; }
QListWidget::item:selected { background: #dbe6ff; color: #0b1b4d; }
QLabel#h1 { font-size: 20pt; font-weight: 600; color: #1b1f27; }
QLabel#muted { color: #4b5361; }
QLabel#hint { color: #3d4757; background: #eef1f6; border-left: 4px solid #1d4ed8; padding: 6px 8px; }
QLabel#near { color: #2a3140; background: #fff8e1; border: 1px solid #e6d9a8; padding: 6px 8px; }
QGroupBox { border: 1px solid #c3c9d4; border-radius: 6px; margin-top: 10px; padding: 8px; background: #ffffff; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; font-weight: 600; }
QTabBar::tab { padding: 8px 16px; }
QTabBar::tab:selected { font-weight: 600; }
"""


# --------------------------------------------------------------------------- background work

class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class Worker(QRunnable):
    """Runs fn() off the GUI thread; results come back through Qt signals."""

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self) -> None:
        try:
            self.signals.done.emit(self.fn())
        except Exception:  # noqa: BLE001
            self.signals.failed.emit(traceback.format_exc())


def build_visuals(work: Path, figures) -> dict:
    """For each figure: a crop of the image and a small page thumbnail with the figure outlined.
    Runs in ONE worker thread (pdfium is not thread-safe)."""
    import pypdfium2 as pdfium
    from PIL import ImageDraw

    out: dict[str, tuple] = {}
    if not figures:
        return out
    pdf = pdfium.PdfDocument(str(work))
    scale = 1.6
    cache: dict[int, tuple] = {}
    for f in figures:
        if f.page not in cache:
            page = pdf[f.page]
            l, b, r, t = page.get_mediabox()
            img = page.render(scale=scale).to_pil().convert("RGB")
            cache = {f.page: (img, l, t)}           # keep only the current page in memory
        img, left, top = cache[f.page]
        W, H = img.size
        crop = img
        rect = None
        if f.bbox:
            x0, y0, x1, y1 = f.bbox
            pad = 6
            px0 = max(0, int((x0 - left - pad) * scale)); px1 = min(W, int((x1 - left + pad) * scale))
            py0 = max(0, int((top - y1 - pad) * scale)); py1 = min(H, int((top - y0 + pad) * scale))
            if px1 - px0 > 4 and py1 - py0 > 4:
                crop = img.crop((px0, py0, px1, py1))
                rect = (px0, py0, px1, py1)
        if max(crop.size) > 900:
            k = 900 / max(crop.size)
            crop = crop.resize((max(1, int(crop.width * k)), max(1, int(crop.height * k))))
        thumb = img.copy()
        if rect:
            ImageDraw.Draw(thumb).rectangle(rect, outline=(220, 20, 60), width=6)
        k = 460 / thumb.height
        thumb = thumb.resize((max(1, int(thumb.width * k)), 460))
        out[f.key] = ((crop.width, crop.height, crop.tobytes()), (thumb.width, thumb.height, thumb.tobytes()))
    cache.clear()
    pdf.close()
    return out


def qimage(t: tuple) -> QImage:
    w, h, data = t
    return QImage(data, w, h, w * 3, QImage.Format.Format_RGB888).copy()


# --------------------------------------------------------------------------- small widgets

class ScaledImage(QLabel):
    """Shows a pixmap scaled to fit, keeping its aspect ratio."""

    def __init__(self, placeholder: str = "") -> None:
        super().__init__(placeholder)
        self._pm: QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(200, 140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet("background:#ffffff;border:1px solid #c3c9d4;")

    def set_image(self, img: QImage | None) -> None:
        self._pm = QPixmap.fromImage(img) if img else None
        self._refresh()

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self._refresh()

    def _refresh(self) -> None:
        if self._pm:
            self.setPixmap(self._pm.scaled(self.size() - QSize(6, 6), Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))
        else:
            self.setPixmap(QPixmap())


STATUS_MARK = {"todo": ("○", "#9a3412", "needs alt text"), "done": ("✓", "#166534", "done"),
               "decorative": ("⊘", "#475569", "decorative")}


# --------------------------------------------------------------------------- pages

class WelcomePage(QWidget):
    open_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.addStretch(2)
        h = QLabel(APP_NAME)
        h.setObjectName("h1")
        h.setAlignment(Qt.AlignmentFlag.AlignCenter)
        p = QLabel("Make a PDF accessible: the tool does the structural work automatically, "
                   "you supply the parts that need human judgement (image descriptions, a title check).\n\n"
                   "Everything runs on this computer. Your original file is never changed.")
        p.setObjectName("muted")
        p.setAlignment(Qt.AlignmentFlag.AlignCenter)
        p.setWordWrap(True)
        p.setMaximumWidth(620)
        b = QPushButton("Open a PDF…")
        b.setObjectName("primary")
        b.setMinimumHeight(40)
        b.setMaximumWidth(260)
        b.clicked.connect(self.open_requested)
        drop = QLabel("or drag a PDF file into this window")
        drop.setObjectName("muted")
        drop.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row = QHBoxLayout(); row.addStretch(); row.addWidget(p); row.addStretch()
        row2 = QHBoxLayout(); row2.addStretch(); row2.addWidget(b); row2.addStretch()
        lay.addWidget(h); lay.addSpacing(12); lay.addLayout(row); lay.addSpacing(18); lay.addLayout(row2)
        lay.addWidget(drop); lay.addStretch(3)


class BusyPage(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.addStretch()
        self.title = QLabel("Working…")
        self.title.setObjectName("h1")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sub = QLabel("")
        self.sub.setObjectName("muted")
        self.sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sub.setWordWrap(True)
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setMaximumWidth(420)
        bar.setTextVisible(False)
        bar.setAccessibleName("Working")
        row = QHBoxLayout(); row.addStretch(); row.addWidget(bar); row.addStretch()
        lay.addWidget(self.title); lay.addWidget(self.sub); lay.addSpacing(10); lay.addLayout(row); lay.addStretch()

    def show_text(self, title: str, sub: str = "") -> None:
        self.title.setText(title)
        self.sub.setText(sub)


class MessagePage(QWidget):
    open_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("page")
        lay = QVBoxLayout(self)
        lay.addStretch()
        self.title = QLabel("")
        self.title.setObjectName("h1")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body = QLabel("")
        self.body.setWordWrap(True)
        self.body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.setMaximumWidth(720)
        b = QPushButton("Open another PDF…")
        b.clicked.connect(self.open_requested)
        b.setMaximumWidth(220)
        r1 = QHBoxLayout(); r1.addStretch(); r1.addWidget(self.body); r1.addStretch()
        r2 = QHBoxLayout(); r2.addStretch(); r2.addWidget(b); r2.addStretch()
        lay.addWidget(self.title); lay.addLayout(r1); lay.addSpacing(12); lay.addLayout(r2); lay.addStretch()

    def show_text(self, title: str, body: str) -> None:
        self.title.setText(title)
        self.body.setText(body)


class WorkPage(QWidget):
    save_requested = Signal()
    closed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("page")
        self.session: Session | None = None
        self.visuals: dict[str, tuple] = {}
        self.drafts: dict[str, str] = {}
        self.current: int = -1

        outer = QVBoxLayout(self)
        head = QHBoxLayout()
        self.file_label = QLabel("")
        self.file_label.setObjectName("h1")
        self.file_label.setStyleSheet("font-size:14pt;")
        self.summary = QLabel("")
        self.summary.setObjectName("muted")
        self.save_btn = QPushButton("Validate and save accessible PDF…")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self.save_requested)
        close_btn = QPushButton("Close file")
        close_btn.clicked.connect(self.closed)
        head.addWidget(self.file_label, 1); head.addWidget(close_btn); head.addWidget(self.save_btn)
        outer.addLayout(head)
        outer.addWidget(self.summary)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self._build_images_tab()
        self.pageview = PageView()
        self.pageview.edited.connect(self._edited)
        self.tabs.addTab(self.pageview, "Pages")
        self.pages_tab_index = self.tabs.count() - 1
        self._build_document_tab()
        self.doc_tab_index = self.tabs.count() - 1
        self._build_result_tab()
        self.tabs.currentChanged.connect(self._tab_changed)

    # ------------------------------------------------------------------ images tab
    def _build_images_tab(self) -> None:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        self.progress = QLabel("")
        self.progress.setObjectName("muted")
        lay.addWidget(self.progress)
        split = QSplitter(Qt.Orientation.Horizontal)
        lay.addWidget(split, 1)

        self.list = QListWidget()
        self.list.setIconSize(QSize(64, 64))
        self.list.setAccessibleName("Images that need alt text")
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setMinimumWidth(260)
        self.list.currentRowChanged.connect(self._select)
        split.addWidget(self.list)

        right = QWidget()
        r = QVBoxLayout(right)
        self.detail_title = QLabel("")
        self.detail_title.setStyleSheet("font-weight:600;font-size:12pt;")
        r.addWidget(self.detail_title)
        imgs = QHBoxLayout()
        self.crop = ScaledImage("No image selected")
        self.crop.setAccessibleName("The image that needs a description")
        self.page_thumb = ScaledImage("")
        self.page_thumb.setAccessibleName("Where the image sits on the page")
        self.page_thumb.setMaximumWidth(300)
        imgs.addWidget(self.crop, 3); imgs.addWidget(self.page_thumb, 1)
        r.addLayout(imgs, 3)
        self.near = QLabel("")
        self.near.setObjectName("near")
        self.near.setWordWrap(True)
        r.addWidget(self.near)
        hint = QLabel("Describe what the image shows and why it is there. Skip openers like “Image of…”. "
                      "Roughly 125 characters is a good target; for charts, say the main finding, and put the "
                      "detail in the text nearby. If the image is purely decorative, choose Decorative.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        r.addWidget(hint)
        self.alt = QPlainTextEdit()
        self.alt.setPlaceholderText("Alt text for this image")
        self.alt.setAccessibleName("Alt text")
        self.alt.setMaximumHeight(110)
        self.alt.textChanged.connect(self._alt_changed)
        r.addWidget(self.alt)
        self.count = QLabel("")
        self.count.setObjectName("muted")
        r.addWidget(self.count)
        btns = QHBoxLayout()
        self.prev_btn = QPushButton("← Previous")
        self.prev_btn.clicked.connect(lambda: self._go(-1))
        self.deco_btn = QPushButton("Decorative (Ctrl+D)")
        self.deco_btn.clicked.connect(self._decorative)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._clear)
        self.show_btn = QPushButton("Show on page")
        self.show_btn.setToolTip("Open this image in the Pages tab")
        self.show_btn.clicked.connect(self._show_on_page)
        self.skip_btn = QPushButton("Skip →")
        self.skip_btn.clicked.connect(lambda: self._go(1))
        self.next_btn = QPushButton("Save && next (Ctrl+Enter)")
        self.next_btn.setObjectName("primary")
        self.next_btn.clicked.connect(self._save_next)
        for b in (self.prev_btn, self.deco_btn, self.clear_btn, self.show_btn):
            btns.addWidget(b)
        btns.addStretch()
        btns.addWidget(self.skip_btn); btns.addWidget(self.next_btn)
        r.addLayout(btns)
        split.addWidget(right)
        split.setStretchFactor(0, 0); split.setStretchFactor(1, 1)
        self.empty_note = QLabel("No images need a description in this document.\n"
                                 "Check the Document and Result tabs, then save.")
        self.empty_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_note.setObjectName("muted")
        lay.addWidget(self.empty_note)
        self.tabs.addTab(tab, "Images")
        self.images_tab = tab
        self.split = split

        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self._save_next)
        QShortcut(QKeySequence("Ctrl+Enter"), self, activated=self._save_next)
        QShortcut(QKeySequence("Ctrl+D"), self, activated=self._decorative)
        QShortcut(QKeySequence("Alt+Right"), self, activated=lambda: self._go(1))
        QShortcut(QKeySequence("Alt+Left"), self, activated=lambda: self._go(-1))

    # ------------------------------------------------------------------ document tab
    def _build_document_tab(self) -> None:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        box = QGroupBox("Document details")
        form = QFormLayout(box)
        self.title_edit = QLineEdit()
        self.title_edit.setAccessibleName("Document title")
        self.title_edit.textChanged.connect(self._meta_changed)
        self.lang_combo = QComboBox()
        self.lang_combo.setAccessibleName("Document language")
        self.lang_combo.setEditable(True)
        for label, code in (("Danish", "da"), ("English", "en"), ("Spanish", "es"),
                            ("English (US)", "en-US"), ("Danish (Denmark)", "da-DK")):
            self.lang_combo.addItem(f"{label} ({code})", code)
        self.lang_combo.currentTextChanged.connect(self._meta_changed)
        form.addRow("Title:", self.title_edit)
        form.addRow("Language:", self.lang_combo)
        lay.addWidget(box)
        self.confirm_box = QGroupBox("Things the tool guessed or wants you to check")
        self.confirm_lay = QVBoxLayout(self.confirm_box)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.confirm_box)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        lay.addWidget(scroll, 1)
        self.tabs.addTab(tab, "Document")

    # ------------------------------------------------------------------ result tab
    def _build_result_tab(self) -> None:
        tab = QWidget()
        lay = QVBoxLayout(tab)
        self.verdict = QLabel("")
        self.verdict.setWordWrap(True)
        self.verdict.setStyleSheet("font-size:13pt;font-weight:600;")
        self.detail = QTextBrowser()
        self.detail.setAccessibleName("Validation details")
        self.recheck = QPushButton("Check again now")
        self.recheck.setMaximumWidth(200)
        lay.addWidget(self.verdict); lay.addWidget(self.detail, 1); lay.addWidget(self.recheck)
        self.tabs.addTab(tab, "Result")
        self.result_tab_index = self.tabs.count() - 1

    # ------------------------------------------------------------------ populate
    def load(self, session: Session, visuals: dict) -> None:
        self.session, self.visuals, self.drafts = session, visuals, {}
        self.file_label.setText(session.source.name)
        r = session.result
        self.summary.setText(f"{r.get('pages', '?')} pages · {r.get('action', '')} · language {session.lang or '?'}")
        self.list.blockSignals(True)
        self.list.clear()
        for f in session.figures:
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, f.key)
            if f.key in visuals:
                it.setIcon(QIcon(QPixmap.fromImage(qimage(visuals[f.key][0]).scaled(
                    64, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))))
            self.list.addItem(it)
            self._refresh_item(self.list.count() - 1)
        self.list.blockSignals(False)
        has = bool(session.figures)
        self.split.setVisible(has); self.progress.setVisible(has); self.empty_note.setVisible(not has)
        self._refresh_progress()
        self.title_edit.blockSignals(True); self.title_edit.setText(session.title); self.title_edit.blockSignals(False)
        self.lang_combo.blockSignals(True)
        i = self.lang_combo.findData(session.lang)
        if i >= 0:
            self.lang_combo.setCurrentIndex(i)
        else:
            self.lang_combo.setCurrentText(session.lang)
        self.lang_combo.blockSignals(False)
        self._fill_confirm(r)
        self.show_verdict_from_result(r)
        self.pageview.unload()
        self.tabs.setCurrentIndex(0)
        if has:
            first = next((i for i, f in enumerate(session.figures) if session.status_of(f.key) == "todo"), 0)
            self.list.setCurrentRow(first)
        else:
            self.tabs.setCurrentIndex(self.doc_tab_index)

    def _fill_confirm(self, r: dict) -> None:
        while self.confirm_lay.count():
            w = self.confirm_lay.takeAt(0).widget()
            if w:
                w.deleteLater()
        items = [("Confirm: " + a) for a in r.get("assumptions", [])] + [("Check: " + n) for n in r.get("review_notes", [])]
        if not items:
            self.confirm_lay.addWidget(QLabel("Nothing to confirm."))
        for text in items:
            cb = QCheckBox(text)
            cb.setChecked(self.session.checked.get(text, False))
            cb.toggled.connect(lambda on, t=text: self._checked(t, on))
            self.confirm_lay.addWidget(cb)
        self.confirm_lay.addStretch()

    def _checked(self, text: str, on: bool) -> None:
        self.session.checked[text] = on
        self.session.save_state()

    def _meta_changed(self) -> None:
        if not self.session:
            return
        self.session.title = self.title_edit.text().strip()
        text = self.lang_combo.currentText().strip()
        i = self.lang_combo.findText(text)
        self.session.lang = self.lang_combo.itemData(i) if i >= 0 else text     # a picked item, or what was typed
        self.session.save_state()

    # ------------------------------------------------------------------ item handling
    def _refresh_item(self, row: int) -> None:
        s = self.session
        f = s.figures[row]
        st = s.status_of(f.key)
        mark, colour, label = STATUS_MARK[st]
        d = s.decisions.get(f.key, {})
        text = d.get("alt", "") if st == "done" else ("Decorative" if st == "decorative" else "Needs alt text")
        it = self.list.item(row)
        it.setText(f"{mark} Page {f.page + 1} · {text[:38]}")
        it.setForeground(QColor(colour))
        it.setToolTip(f"{label}")

    def _refresh_progress(self) -> None:
        s = self.session
        n = len(s.figures)
        done = n - s.remaining()
        self.progress.setText(f"{done} of {n} images decided" + ("  ·  all done" if n and done == n else ""))
        self.next_btn.setEnabled(bool(n))

    def _select(self, row: int) -> None:
        s = self.session
        if not s or row < 0 or row >= len(s.figures):
            return
        self._stash_draft()
        self.current = row
        f = s.figures[row]
        self.detail_title.setText(f"Page {f.page + 1} · image {row + 1} of {len(s.figures)}")
        v = self.visuals.get(f.key)
        self.crop.set_image(qimage(v[0]) if v else None)
        self.page_thumb.set_image(qimage(v[1]) if v else None)
        self.near.setVisible(bool(f.near_text))
        self.near.setText("Text near this image: " + f.near_text if f.near_text else "")
        d = s.decisions.get(f.key, {})
        self.alt.blockSignals(True)
        self.alt.setPlainText(self.drafts.get(f.key, d.get("alt", "")))
        self.alt.blockSignals(False)
        self._alt_changed()
        self.alt.setFocus()

    def _stash_draft(self) -> None:
        s = self.session
        if s and 0 <= self.current < len(s.figures):
            k = s.figures[self.current].key
            t = self.alt.toPlainText()
            if t.strip() and t.strip() != s.decisions.get(k, {}).get("alt", ""):
                self.drafts[k] = t
            else:
                self.drafts.pop(k, None)

    def _alt_changed(self) -> None:
        n = len(self.alt.toPlainText().strip())
        self.count.setText(f"{n} characters" + ("  ·  quite long: consider shortening" if n > 250 else ""))

    def _go(self, step: int) -> None:
        if not self.session or not self.session.figures:
            return
        self.list.setCurrentRow(max(0, min(len(self.session.figures) - 1, self.current + step)))

    def _next_todo(self, after: int) -> int | None:
        s = self.session
        n = len(s.figures)
        for k in range(1, n + 1):
            i = (after + k) % n
            if s.status_of(s.figures[i].key) == "todo":
                return i
        return None

    def _save_next(self) -> None:
        s = self.session
        if not s or self.current < 0:
            return
        text = self.alt.toPlainText().strip()
        if not text:
            QMessageBox.information(self, APP_NAME, "Type a description first, or choose Decorative if the image "
                                                    "adds no information.")
            self.alt.setFocus()
            return
        key = s.figures[self.current].key
        s.set_alt(key, text)
        self.drafts.pop(key, None)
        self._decided(self.current)

    def _decorative(self) -> None:
        s = self.session
        if not s or self.current < 0:
            return
        key = s.figures[self.current].key
        s.set_decorative(key, True)
        self.drafts.pop(key, None)
        self.alt.blockSignals(True); self.alt.clear(); self.alt.blockSignals(False)
        self._decided(self.current)

    def _clear(self) -> None:
        s = self.session
        if not s or self.current < 0:
            return
        key = s.figures[self.current].key
        s.set_decorative(key, False)
        self.drafts.pop(key, None)
        self.alt.clear()
        self._refresh_item(self.current)
        self._refresh_progress()

    def _decided(self, row: int) -> None:
        self._refresh_item(row)
        self._refresh_progress()
        nxt = self._next_todo(row)
        if nxt is None:
            QMessageBox.information(self, APP_NAME, "Every image now has a description or is marked decorative.\n\n"
                                                    "Check the Document tab, then choose “Validate and save”.")
        else:
            self.list.setCurrentRow(nxt)

    # ------------------------------------------------------------------ pages tab glue
    def _show_on_page(self) -> None:
        s = self.session
        if not s or self.current < 0:
            return
        f = s.figures[self.current]
        self.stash_and_switch = True
        self.tabs.setCurrentIndex(self.pages_tab_index)
        # the figure element's object number is the block reference
        self.pageview.goto(f.page, select=(f.obj, 0), force=True)

    def _tab_changed(self, i: int) -> None:
        if i == self.pages_tab_index and self.session and self.pageview.doc is None:
            self.pageview.load(self.session)

    def _edited(self) -> None:
        """An edit or alt-text change in the page view: keep the Images list in step."""
        if not self.session:
            return
        for row in range(self.list.count()):
            self._refresh_item(row)
        self._refresh_progress()

    # ------------------------------------------------------------------ result
    def show_verdict_from_result(self, r: dict) -> None:
        v = r.get("verapdf")
        if not v:
            self.verdict.setText("Not validated yet.")
            self.detail.setPlainText("")
            return
        self._show_verdict(v["compliant"], v["failed_rules"], r.get("stamped", False))

    def _show_verdict(self, compliant: bool | None, rules: list, stamped: bool = False) -> None:
        if compliant:
            self.verdict.setText("✓ Passes the automated PDF/UA-1 check (veraPDF)" + ("  ·  identifier added" if stamped else ""))
            self.verdict.setStyleSheet("font-size:13pt;font-weight:600;color:#166534;")
            self.detail.setHtml("<p>The independent validator found no rule violations.</p>"
                                "<p>It cannot judge whether descriptions are meaningful, the reading order makes sense, or "
                                "tables are sensible: please look over the pages once before publishing.</p>")
        else:
            self.verdict.setText("✗ Not there yet")
            self.verdict.setStyleSheet("font-size:13pt;font-weight:600;color:#9a3412;")
            rows = "".join(f"<li>{(r.get('friendly') or r['description'])} <i>({r['count']}×, rule {r['clause']}-{r['test']})</i></li>"
                           for r in rules if not (str(r['clause']) == '5' and int(r['test']) == 1 and len(rules) > 1))
            self.detail.setHtml(f"<p>Still to do:</p><ul>{rows}</ul>")


# --------------------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1180, 780)
        self.setAcceptDrops(True)
        self.pool = QThreadPool.globalInstance()
        self.session: Session | None = None
        self._workers: list[Worker] = []

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.welcome, self.busy, self.message, self.work = WelcomePage(), BusyPage(), MessagePage(), WorkPage()
        for w in (self.welcome, self.busy, self.message, self.work):
            self.stack.addWidget(w)
        self.welcome.open_requested.connect(self.open_dialog)
        self.message.open_requested.connect(self.open_dialog)
        self.work.save_requested.connect(self.save)
        self.work.closed.connect(self.close_file)
        self.work.recheck.clicked.connect(self.recheck)

        act = QAction("Open PDF…", self)
        act.setShortcut(QKeySequence.StandardKey.Open)
        act.triggered.connect(self.open_dialog)
        self.menuBar().addMenu("&File").addAction(act)
        self.statusBar().showMessage("Ready")

    # ------------------------------------------------------------------ helpers
    def _run(self, fn, done, failed=None) -> None:
        w = Worker(fn)
        w.signals.done.connect(done)
        w.signals.failed.connect(failed or self._crash)
        self._workers.append(w)
        self.pool.start(w)

    def _crash(self, tb: str) -> None:
        self.message.show_text("Something went wrong", tb.strip().splitlines()[-1] if tb.strip() else "Unknown error")
        self.stack.setCurrentWidget(self.message)
        self.statusBar().showMessage("Error")

    # ------------------------------------------------------------------ opening
    def dragEnterEvent(self, e) -> None:  # noqa: N802
        if e.mimeData().hasUrls() and any(u.toLocalFile().lower().endswith(".pdf") for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:  # noqa: N802
        for u in e.mimeData().urls():
            if u.toLocalFile().lower().endswith(".pdf"):
                self.open_file(u.toLocalFile())
                return

    def open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open a PDF", "", "PDF files (*.pdf)")
        if path:
            self.open_file(path)

    def open_file(self, path: str) -> None:
        self.work.pageview.unload()
        if self.session:
            self.session.close()
        self.session = Session(Path(path))
        self.busy.show_text(f"Making “{Path(path).name}” accessible…",
                            "Tagging structure, fixing document settings and validating. This can take a minute for long documents.")
        self.stack.setCurrentWidget(self.busy)
        self.statusBar().showMessage("Working…")
        s = self.session

        def job():
            r = s.process()
            vis = build_visuals(s.work, s.figures) if s.work and s.work.exists() else {}
            return r, vis

        self._run(job, self._opened)

    def _opened(self, payload) -> None:
        r, vis = payload
        s = self.session
        if r.get("status") in ("Failed", "Skipped") or not (s.work and s.work.exists()):
            self.message.show_text("This file could not be processed", r.get("error") or "Unknown problem")
            self.stack.setCurrentWidget(self.message)
            self.statusBar().showMessage(r.get("status", "Error"))
            return
        self.work.load(s, vis)
        self.stack.setCurrentWidget(self.work)
        self.statusBar().showMessage(f"{len(s.figures)} image(s) to review")

    def close_file(self) -> None:
        self.work.pageview.unload()
        if self.session:
            self.session.close()
            self.session = None
        self.stack.setCurrentWidget(self.welcome)
        self.statusBar().showMessage("Ready")

    # ------------------------------------------------------------------ result / save
    def recheck(self) -> None:
        s = self.session
        if not s:
            return
        self.statusBar().showMessage("Checking…")
        self.work.recheck.setEnabled(False)

        def done(r):
            self.work.recheck.setEnabled(True)
            self.work._show_verdict(r["compliant"], r["failed_rules"], r["stamped"])
            self.work.tabs.setCurrentIndex(self.work.result_tab_index)
            self.statusBar().showMessage("Checked")

        self._run(s.check_now, done)

    def save(self) -> None:
        s = self.session
        if not s:
            return
        remaining = s.remaining()
        if remaining:
            ans = QMessageBox.question(
                self, APP_NAME,
                f"{remaining} image(s) still have no description, so the file cannot pass the accessibility check yet.\n\n"
                "Save it anyway as a work-in-progress copy?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            if ans != QMessageBox.StandardButton.Yes:
                return
        default = str(s.source.with_name(s.source.stem + " (accessible).pdf"))
        path, _ = QFileDialog.getSaveFileName(self, "Save accessible PDF", default, "PDF files (*.pdf)")
        if not path:
            return
        if Path(path).resolve() == s.source.resolve():
            QMessageBox.warning(self, APP_NAME, "Please choose a different name: the original is never overwritten.")
            return
        self.statusBar().showMessage("Validating and saving…")
        self.work.save_btn.setEnabled(False)

        def done(r):
            self.work.save_btn.setEnabled(True)
            self.work._show_verdict(r["compliant"], r["failed_rules"], r["stamped"])
            self.work.tabs.setCurrentIndex(self.work.result_tab_index)
            if r["compliant"]:
                msg = f"Saved. The file passes the automated PDF/UA-1 check.\n\n{r['path']}"
            else:
                msg = f"Saved as a work-in-progress copy. It does not pass yet: see the Result tab.\n\n{r['path']}"
            QMessageBox.information(self, APP_NAME, msg)
            self.statusBar().showMessage("Saved")

        self._run(lambda: s.finalize(path), done)


def main(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    app.setApplicationName(APP_NAME)
    icon = Path(__file__).with_name("icon.png")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    win = MainWindow()
    win.show()
    args = [a for a in (argv if argv is not None else sys.argv)[1:] if a.lower().endswith(".pdf")]
    if args:
        win.open_file(args[0])
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
