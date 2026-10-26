"""Page view: the page with its tag blocks overlaid, the structure tree, and editing actions."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QGraphicsItem, QGraphicsPixmapItem,
                               QGraphicsRectItem, QGraphicsScene, QGraphicsView, QGroupBox, QHBoxLayout, QLabel,
                               QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from ..editor import BLOCK_ROLES, TEXT_ROLES, EditError

COLOURS = {"H": "#dc2626", "P": "#2563eb", "LBody": "#16a34a", "Lbl": "#16a34a", "TD": "#7c3aed", "TH": "#7c3aed",
           "Figure": "#ea580c", "Caption": "#0891b2", "Note": "#0891b2", "BlockQuote": "#0891b2", "TOCI": "#9333ea"}
TYPE_CHOICES = [("Paragraph", "P"), ("Heading 1", "H1"), ("Heading 2", "H2"), ("Heading 3", "H3"), ("Heading 4", "H4"),
                ("Heading 5", "H5"), ("Heading 6", "H6"), ("Caption", "Caption"), ("Quote", "BlockQuote")]
CELL_CHOICES = [("Data cell", "td"), ("Header for its column", "th_col"), ("Header for its row", "th_row")]


def colour_for(role: str) -> QColor:
    key = "H" if role in ("H", "H1", "H2", "H3", "H4", "H5", "H6") else role
    return QColor(COLOURS.get(key, "#64748b"))


def label_for(role: str, order: int) -> str:
    return f"{order + 1}" + (f" {role}" if role.startswith("H") and role != "H" else "")


class BlockItem(QGraphicsRectItem):
    """One block overlay: outlined rectangle with its reading-order number."""

    def __init__(self, rect: QRectF, block, on_click) -> None:
        super().__init__(rect)
        self.block = block
        self.on_click = on_click
        self.colour = colour_for(block.role)
        self.selected_flag = False
        self.setAcceptHoverEvents(True)
        self.setToolTip(f"{block.role}: {block.text[:120]}")
        self._style()

    def _style(self) -> None:
        c = QColor(self.colour)
        pen = QPen(c, 3.5 if self.selected_flag else 1.5)
        self.setPen(pen)
        fill = QColor(c)
        fill.setAlpha(95 if self.selected_flag else 30)
        self.setBrush(QBrush(fill))
        self.setZValue(2 if self.selected_flag else 1)

    def set_selected_flag(self, on: bool) -> None:
        self.selected_flag = on
        self._style()
        self.update()

    def hoverEnterEvent(self, e) -> None:  # noqa: N802
        if not self.selected_flag:
            c = QColor(self.colour); c.setAlpha(65)
            self.setBrush(QBrush(c))

    def hoverLeaveEvent(self, e) -> None:  # noqa: N802
        self._style()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        self.on_click(self.block.ref)
        e.accept()

    BADGE_H = 14

    def boundingRect(self) -> QRectF:  # noqa: N802
        r = super().boundingRect()
        return QRectF(r.left(), r.top() - self.BADGE_H, max(r.width(), 60), r.height() + self.BADGE_H)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        super().paint(painter, option, widget)
        text = label_for(self.block.role, self.block.order)
        f = QFont(); f.setPointSize(8); f.setBold(True)
        painter.setFont(f)
        fm = painter.fontMetrics()
        w = fm.horizontalAdvance(text) + 8
        r = self.rect()
        box = QRectF(r.left(), r.top() - self.BADGE_H, w, self.BADGE_H)     # sits above the block: never hides text
        painter.fillRect(box, self.colour)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)


class PageView(QWidget):
    edited = Signal()                       # an edit was made (the window saves state)
    figure_selected = Signal(str)          # key of a selected figure (so the Images tab can follow)

    def __init__(self) -> None:
        super().__init__()
        self.session = None
        self.doc = None
        self.page = 0
        self.zoom = 1.25
        self.selected = None
        self.items: dict[tuple, BlockItem] = {}
        self.blocks: dict[tuple, object] = {}
        self._loading = False

        outer = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.prev_btn = QPushButton("◀ Page")
        self.prev_btn.setAccessibleName("Previous page")
        self.prev_btn.clicked.connect(lambda: self.goto(self.page - 1))
        self.spin = QSpinBox()
        self.spin.setAccessibleName("Page number")
        self.spin.setMinimum(1)
        self.spin.valueChanged.connect(lambda v: self.goto(v - 1))
        self.of_label = QLabel("")
        self.next_btn = QPushButton("Page ▶")
        self.next_btn.setAccessibleName("Next page")
        self.next_btn.clicked.connect(lambda: self.goto(self.page + 1))
        self.zoom_out = QPushButton("−")
        self.zoom_out.setAccessibleName("Zoom out")
        self.zoom_in = QPushButton("+")
        self.zoom_in.setAccessibleName("Zoom in")
        self.zoom_out.setMaximumWidth(34); self.zoom_in.setMaximumWidth(34)
        self.zoom_out.clicked.connect(lambda: self.set_zoom(self.zoom / 1.2))
        self.zoom_in.clicked.connect(lambda: self.set_zoom(self.zoom * 1.2))
        self.fit_btn = QPushButton("Fit width")
        self.fit_btn.clicked.connect(self.fit_width)
        self.undo_btn = QPushButton("Undo")
        self.undo_btn.clicked.connect(self.undo)
        self.redo_btn = QPushButton("Redo")
        self.redo_btn.clicked.connect(self.redo)
        self.heading_btn = QPushButton("Fix heading levels")
        self.heading_btn.setToolTip("Renumber headings so they nest properly and start at Heading 1")
        self.heading_btn.clicked.connect(lambda: self._do("renumber_headings", note="Heading levels renumbered."))
        for w in (self.prev_btn, self.spin, self.of_label, self.next_btn):
            bar.addWidget(w)
        bar.addSpacing(16)
        for w in (self.zoom_out, self.zoom_in, self.fit_btn):
            bar.addWidget(w)
        bar.addSpacing(16)
        for w in (self.undo_btn, self.redo_btn, self.heading_btn):
            bar.addWidget(w)
        bar.addStretch()
        legend = QLabel("Blue text · Red headings · Green list · Purple table · Orange image")
        legend.setObjectName("muted")
        bar.addWidget(legend)
        outer.addLayout(bar)

        split = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(split, 1)

        # ---- left: tree + properties
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Structure on this page", ""])
        self.tree.setAccessibleName("Structure on this page, in reading order")
        self.tree.setColumnWidth(0, 250)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.itemSelectionChanged.connect(self._tree_selected)
        lv.addWidget(self.tree, 3)
        self.props = QGroupBox("Selected block")
        pv = QVBoxLayout(self.props)
        self.sel_label = QLabel("Click a block on the page, or pick one in the list.")
        self.sel_label.setWordWrap(True)
        pv.addWidget(self.sel_label)
        self.type_combo = QComboBox()
        self.type_combo.setAccessibleName("Block type")
        for label, code in TYPE_CHOICES:
            self.type_combo.addItem(label, code)
        self.type_combo.activated.connect(self._type_chosen)
        self.type_row = self._row("Type:", self.type_combo)
        pv.addWidget(self.type_row)
        self.cell_combo = QComboBox()
        self.cell_combo.setAccessibleName("Table cell type")
        for label, code in CELL_CHOICES:
            self.cell_combo.addItem(label, code)
        self.cell_combo.activated.connect(self._cell_chosen)
        self.cell_row = self._row("Cell:", self.cell_combo)
        pv.addWidget(self.cell_row)
        self.hdr_row_cb = QCheckBox("First row is a header")
        self.hdr_col_cb = QCheckBox("First column is a header")
        self.hdr_row_cb.clicked.connect(self._table_hdr)
        self.hdr_col_cb.clicked.connect(self._table_hdr)
        self.table_box = QWidget()
        tb = QVBoxLayout(self.table_box); tb.setContentsMargins(0, 0, 0, 0)
        tb.addWidget(self.hdr_row_cb); tb.addWidget(self.hdr_col_cb)
        pv.addWidget(self.table_box)
        self.alt_box = QWidget()
        ab = QVBoxLayout(self.alt_box); ab.setContentsMargins(0, 0, 0, 0)
        self.alt_edit = QPlainTextEdit()
        self.alt_edit.setAccessibleName("Alt text")
        self.alt_edit.setPlaceholderText("Alt text for this image")
        self.alt_edit.setMaximumHeight(80)
        row = QHBoxLayout()
        save_alt = QPushButton("Save alt text")
        save_alt.clicked.connect(self._save_alt)
        deco = QPushButton("Decorative")
        deco.clicked.connect(self._set_decorative)
        row.addWidget(save_alt); row.addWidget(deco)
        ab.addWidget(self.alt_edit); ab.addLayout(row)
        pv.addWidget(self.alt_box)
        acts = QHBoxLayout()
        self.up_btn = QPushButton("Move up")
        self.up_btn.setToolTip("Read this block earlier (Alt+Up)")
        self.up_btn.clicked.connect(lambda: self._do("move", -1, note="Moved up."))
        self.down_btn = QPushButton("Move down")
        self.down_btn.setToolTip("Read this block later (Alt+Down)")
        self.down_btn.clicked.connect(lambda: self._do("move", 1, note="Moved down."))
        acts.addWidget(self.up_btn); acts.addWidget(self.down_btn)
        pv.addLayout(acts)
        acts2 = QHBoxLayout()
        self.merge_btn = QPushButton("Merge with next")
        self.merge_btn.setToolTip("Join this block and the next into one paragraph")
        self.merge_btn.clicked.connect(lambda: self._do("merge_next", note="Merged."))
        self.hide_btn = QPushButton("Mark as decoration")
        self.hide_btn.setToolTip("Screen readers will skip this block (Delete). Undo with Ctrl+Z")
        self.hide_btn.clicked.connect(self._artifact)
        acts2.addWidget(self.merge_btn); acts2.addWidget(self.hide_btn)
        pv.addLayout(acts2)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setObjectName("muted")
        pv.addWidget(self.message)
        lv.addWidget(self.props, 2)
        split.addWidget(left)

        # ---- right: page
        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene)
        self.view.setAccessibleName("Page with tag overlay")
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.view.setBackgroundBrush(QBrush(QColor("#d7dbe2")))
        self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        split.addWidget(self.view)
        split.setStretchFactor(0, 0); split.setStretchFactor(1, 1)
        split.setSizes([380, 800])

        QShortcut(QKeySequence("Ctrl+Z"), self, activated=self.undo)
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=self.redo)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self, activated=self.redo)
        QShortcut(QKeySequence("Alt+Up"), self, activated=lambda: self._do("move", -1, note="Moved up."))
        QShortcut(QKeySequence("Alt+Down"), self, activated=lambda: self._do("move", 1, note="Moved down."))
        QShortcut(QKeySequence("PgDown"), self, activated=lambda: self.goto(self.page + 1))
        QShortcut(QKeySequence("PgUp"), self, activated=lambda: self.goto(self.page - 1))
        self._enable_actions(None)

    @staticmethod
    def _row(label: str, w: QWidget) -> QWidget:
        box = QWidget()
        h = QHBoxLayout(box); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(QLabel(label)); h.addWidget(w, 1)
        return box

    # ------------------------------------------------------------------ loading / paging
    def load(self, session) -> None:
        import pypdfium2 as pdfium
        self.session = session
        if self.doc is not None:
            self.doc.close()
        self.doc = pdfium.PdfDocument(str(session.work))
        n = len(self.doc)
        self.spin.blockSignals(True)
        self.spin.setMaximum(n)
        self.spin.blockSignals(False)
        self.of_label.setText(f"of {n}")
        self.selected = None
        self.goto(0, force=True)

    def unload(self) -> None:
        if self.doc is not None:
            self.doc.close()
            self.doc = None
        self.scene.clear()
        self.tree.clear()

    def goto(self, page: int, select=None, force: bool = False) -> None:
        if self.doc is None:
            return
        page = max(0, min(len(self.doc) - 1, page))
        if page == self.page and not force and select is None:
            return
        self.page = page
        self.spin.blockSignals(True); self.spin.setValue(page + 1); self.spin.blockSignals(False)
        if select is None and not force:
            self.selected = None
        elif select is not None:
            self.selected = tuple(select)
        self.refresh()

    def set_zoom(self, z: float) -> None:
        self.zoom = max(0.4, min(4.0, z))
        self.refresh()

    def fit_width(self) -> None:
        if self.doc is None:
            return
        w = self.doc[self.page].get_width()
        self.zoom = max(0.4, min(4.0, (self.view.viewport().width() - 24) / w))
        self.refresh()

    # ------------------------------------------------------------------ drawing
    def refresh(self) -> None:
        if self.doc is None or self.session is None or self.session.editor is None:
            return
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            ed = self.session.editor
            pil = self.doc[self.page].render(scale=self.zoom).to_pil().convert("RGB")
            img = QImage(pil.tobytes(), pil.width, pil.height, pil.width * 3, QImage.Format.Format_RGB888).copy()
            self.scene.clear()
            self.items = {}
            pm = QGraphicsPixmapItem(QPixmap.fromImage(img))
            pm.setZValue(0)
            self.scene.addItem(pm)
            self.scene.setSceneRect(QRectF(0, 0, pil.width, pil.height))
            g = ed.geometry(self.page)
            blocks = ed.page_blocks(self.page)
            self.blocks = {b.ref: b for b in blocks}
            for b in blocks:
                if not b.bbox:
                    continue
                x0, y0, x1, y1 = b.bbox
                r = QRectF((x0 - g.left) * self.zoom, (g.top - y1) * self.zoom, (x1 - x0) * self.zoom, (y1 - y0) * self.zoom)
                it = BlockItem(r.adjusted(-2, -2, 2, 2), b, self._pick)
                self.scene.addItem(it)
                self.items[b.ref] = it
            self._fill_tree(ed.page_tree(self.page))
        finally:
            self.unsetCursor()
        self._apply_selection(scroll=True)
        self.undo_btn.setEnabled(self.session.editor.can_undo)
        self.redo_btn.setEnabled(self.session.editor.can_redo)

    def _fill_tree(self, nodes) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()

        def add(parent, node) -> None:
            item = QTreeWidgetItem(parent if parent is not None else self.tree)
            b = self.blocks.get(node.ref)
            item.setText(0, f"{node.role}" + (f"   {node.text}" if node.text else ""))
            if b is not None:
                item.setForeground(0, colour_for(node.role))
            item.setData(0, Qt.ItemDataRole.UserRole, node.ref)
            for c in node.children:
                add(item, c)
            item.setExpanded(True)

        for n in nodes:
            add(None, n)
        self.tree.blockSignals(False)

    # ------------------------------------------------------------------ selection
    def _pick(self, ref) -> None:
        self.selected = tuple(ref)
        self._apply_selection(scroll=False)

    def _tree_selected(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            return
        ref = items[0].data(0, Qt.ItemDataRole.UserRole)
        if ref and ref[0] >= 0:
            self.selected = tuple(ref)
            self._apply_selection(scroll=True, from_tree=True)

    def _apply_selection(self, scroll: bool, from_tree: bool = False) -> None:
        for ref, it in self.items.items():
            it.set_selected_flag(ref == self.selected)
        if self.selected in self.items and scroll:
            self.view.ensureVisible(self.items[self.selected], 60, 60)
        if not from_tree:
            self.tree.blockSignals(True)
            self.tree.clearSelection()
            self._select_tree_item(self.selected)
            self.tree.blockSignals(False)
        self._enable_actions(self.selected)

    def _select_tree_item(self, ref) -> None:
        if ref is None:
            return

        def walk(it):
            if tuple(it.data(0, Qt.ItemDataRole.UserRole) or ()) == ref:
                return it
            for i in range(it.childCount()):
                r = walk(it.child(i))
                if r:
                    return r
            return None

        for i in range(self.tree.topLevelItemCount()):
            it = walk(self.tree.topLevelItem(i))
            if it:
                it.setSelected(True)
                self.tree.scrollToItem(it)
                return

    def _enable_actions(self, ref) -> None:
        ed = self.session.editor if self.session else None
        for w in (self.type_row, self.cell_row, self.table_box, self.alt_box):
            w.setVisible(False)
        for b in (self.up_btn, self.down_btn, self.merge_btn, self.hide_btn):
            b.setEnabled(False)
        self.message.setText("")
        if ref is None or ed is None:
            self.sel_label.setText("Click a block on the page, or pick one in the list.")
            return
        try:
            role = ed.role_of(ref)
        except EditError:
            self.sel_label.setText("That element no longer exists.")
            return
        b = self.blocks.get(ref)
        self.sel_label.setText(f"<b>{role}</b>" + (f"<br>{b.text[:160]}" if b and b.text else ""))
        for w in (self.up_btn, self.down_btn, self.hide_btn):
            w.setEnabled(True)
        self._loading = True
        if role in TEXT_ROLES | {"H"}:
            self.type_row.setVisible(True)
            i = self.type_combo.findData(role)
            self.type_combo.setCurrentIndex(i if i >= 0 else 0)
            self.merge_btn.setEnabled(True)
        elif role in ("Note", "TOCI", "LBody", "Title"):
            self.merge_btn.setEnabled(role == "LBody")
        if role in ("TD", "TH"):
            self.cell_row.setVisible(True)
            scope = "th_col" if role == "TH" else "td"
            if role == "TH":
                try:
                    from ..editor import _get
                    a = _get(ed.pdf, ref).get("/A")
                    items = list(a) if a is not None and hasattr(a, "__len__") and not hasattr(a, "keys") else ([a] if a is not None else [])
                    for x in items:
                        if hasattr(x, "get") and "/Scope" in x and str(x.Scope) == "/Row":
                            scope = "th_row"
                except Exception:  # noqa: BLE001
                    pass
            self.cell_combo.setCurrentIndex(max(0, self.cell_combo.findData(scope)))
        if role == "Table":
            self.table_box.setVisible(True)
            r, c = ed.table_state(ref)
            self.hdr_row_cb.setChecked(r); self.hdr_col_cb.setChecked(c)
        if role == "Figure":
            self.alt_box.setVisible(True)
            key = ed.figure_key(ref)
            d = self.session.decisions.get(key, {})
            self.alt_edit.setPlainText(d.get("alt", "") if not d.get("decorative") else "")
            self.figure_selected.emit(key)
        self._loading = False

    # ------------------------------------------------------------------ actions
    def _do(self, name: str, *args, note: str = "") -> None:
        ed = self.session.editor if self.session else None
        if ed is None:
            return
        if name != "renumber_headings" and self.selected is None:
            self.message.setText("Select a block first.")
            return
        try:
            if name == "renumber_headings":
                ed.do(name)
            else:
                ed.do(name, list(self.selected), *args)
        except EditError as e:
            self.message.setText(str(e))
            return
        self.session.save_state()
        self.refresh()
        self.message.setText(note)
        self.edited.emit()

    def _type_chosen(self) -> None:
        if self._loading:
            return
        self._do("set_role", self.type_combo.currentData(), note="Type changed. If this makes a heading, check the "
                                                                 "heading levels (Fix heading levels).")

    def _cell_chosen(self) -> None:
        if not self._loading:
            self._do("cell", self.cell_combo.currentData(), note="Cell type changed.")

    def _table_hdr(self) -> None:
        self._do("table_headers", self.hdr_row_cb.isChecked(), self.hdr_col_cb.isChecked(), note="Table headers updated.")

    def _artifact(self) -> None:
        if self.selected is None:
            return
        node_text = ""
        b = self.blocks.get(self.selected)
        if b is None:
            ans = QMessageBox.question(self, "Mark as decoration",
                                       "This group contains several blocks. Screen readers will skip all of them.\n\nContinue? (You can undo.)")
            if ans != QMessageBox.StandardButton.Yes:
                return
        self._do("artifact", note="Marked as decoration. Ctrl+Z to undo.")

    def _save_alt(self) -> None:
        if self.selected is None or self.session is None:
            return
        key = self.session.editor.figure_key(self.selected)
        text = self.alt_edit.toPlainText().strip()
        if not text:
            self.message.setText("Type a description first, or choose Decorative.")
            return
        self.session.set_alt(key, text)
        self.message.setText("Alt text saved.")
        self.edited.emit()

    def _set_decorative(self) -> None:
        if self.selected is None or self.session is None:
            return
        self.session.set_decorative(self.session.editor.figure_key(self.selected), True)
        self.alt_edit.clear()
        self.message.setText("Marked as decorative.")
        self.edited.emit()

    def undo(self) -> None:
        ed = self.session.editor if self.session else None
        if ed and ed.undo():
            self.session.save_state()
            self.refresh()
            self.message.setText("Undone.")
            self.edited.emit()

    def redo(self) -> None:
        ed = self.session.editor if self.session else None
        if ed and ed.redo():
            self.session.save_state()
            self.refresh()
            self.message.setText("Redone.")
            self.edited.emit()
