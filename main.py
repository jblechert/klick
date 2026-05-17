#!/usr/bin/env python3
"""
atspi-search – KDE element search overlay with live highlighting

Dependencies:
    pip install pyatspi PyQt6
"""

import signal
import sys
import time

try:
    import pyatspi
except ImportError:
    sys.exit("Error: pyatspi not installed — run: pip install pyatspi")

try:
    from PyQt6.QtCore import Qt, QRect, QTimer
    from PyQt6.QtGui import QColor, QPainter, QPen
    from PyQt6.QtWidgets import (
        QApplication, QFrame, QLabel, QLineEdit,
        QListWidget, QListWidgetItem, QVBoxLayout, QWidget,
    )
except ImportError:
    sys.exit("Error: PyQt6 not installed — run: pip install PyQt6")

# ── AT-SPI config ─────────────────────────────────────────────────────────────

INTERESTING_ROLES = {
    "push button", "toggle button", "menu item", "check menu item",
    "radio menu item", "menu", "check box", "radio button",
    "combo box", "tool bar", "page tab",
}

ROLE_ICONS: dict[str, str] = {
    "push button":     "⬡",
    "toggle button":   "⬡",
    "menu item":       "▸",
    "check menu item": "✓",
    "radio menu item": "◉",
    "menu":            "▾",
    "check box":       "☐",
    "radio button":    "◎",
    "combo box":       "▼",
    "tool bar":        "━",
    "page tab":        "⬜",
}

MAX_DEPTH = 20


# ── AT-SPI helpers ────────────────────────────────────────────────────────────

def get_focused_app(desktop):
    for app in desktop:
        if app is None:
            continue
        try:
            for i in range(app.childCount):
                win = app.getChildAtIndex(i)
                if win.getState().contains(pyatspi.STATE_ACTIVE):
                    return app
        except Exception:
            continue
    return None


def collect_elements(node, results: list, depth: int = 0, breadcrumb: str = ""):
    if depth > MAX_DEPTH:
        return
    try:
        role = node.getRoleName()
        name = (node.name or "").strip()

        crumb = (f"{breadcrumb} › {name}" if breadcrumb else name) \
            if (role == "menu" and name) else breadcrumb

        if role in INTERESTING_ROLES and name:
            display = f"{crumb} › {name}" if (crumb and role != "menu") else name
            rect: QRect | None = None
            try:
                ext = node.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
                if ext.width > 0 and ext.height > 0:
                    rect = QRect(ext.x, ext.y, ext.width, ext.height)
            except Exception:
                pass
            results.append({"display": display, "role": role, "node": node, "rect": rect})

        for i in range(node.childCount):
            try:
                collect_elements(node.getChildAtIndex(i), results, depth + 1, crumb)
            except Exception:
                continue
    except Exception:
        pass


def activate_element(node):
    try:
        action = node.queryAction()
        preferred = {"click", "activate", "press", "toggle", "expand or contract", "open"}
        for i in range(action.nActions):
            if action.getName(i).lower() in preferred:
                action.doAction(i)
                return
        if action.nActions > 0:
            action.doAction(0)
            return
    except Exception:
        pass
    try:
        node.grabFocus()
    except Exception:
        pass


# ── Highlight overlay ─────────────────────────────────────────────────────────

_HIGHLIGHT = QColor(99, 162, 255)   # KDE accent blue


class HighlightOverlay(QWidget):
    """Transparent frameless window drawn over the target element."""

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._inner = QRect()

    def show_at(self, rect: QRect | None):
        if not rect or rect.isEmpty():
            self.hide()
            return
        pad = 4
        self.setGeometry(rect.adjusted(-pad, -pad, pad, pad))
        self._inner = QRect(pad, pad, rect.width(), rect.height())
        self.show()
        self.raise_()
        self.update()

    def paintEvent(self, _):
        if self._inner.isEmpty():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill = QColor(_HIGHLIGHT)
        fill.setAlpha(50)
        p.fillRect(self._inner, fill)
        pen = QPen(_HIGHLIGHT, 2)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.drawRoundedRect(self._inner.adjusted(1, 1, -1, -1), 3, 3)


# ── Search dialog ─────────────────────────────────────────────────────────────

_STYLE = """
QFrame#shell {
    background: #1e1e2e;
    border: 1px solid #585b70;
    border-radius: 10px;
}
QLineEdit {
    background: #313244;
    color: #cdd6f4;
    border: 1px solid #585b70;
    border-radius: 5px;
    padding: 7px 10px;
    font-size: 14px;
}
QListWidget {
    background: transparent;
    color: #cdd6f4;
    border: none;
    font-size: 13px;
    outline: 0;
}
QListWidget::item {
    padding: 5px 8px;
    border-radius: 4px;
}
QListWidget::item:selected {
    background: #45475a;
}
QLabel#count {
    color: #6c7086;
    font-size: 11px;
    padding: 0 2px 2px 2px;
}
"""


class SearchDialog(QWidget):
    def __init__(self, elements: list, app_name: str):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.elements = elements
        self.filtered: list = []
        self.overlay = HighlightOverlay()
        self._build_ui(app_name)
        self._filter("")

    def _build_ui(self, app_name: str):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        shell = QFrame()
        shell.setObjectName("shell")
        shell.setStyleSheet(_STYLE)
        v = QVBoxLayout(shell)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(6)

        self.search = QLineEdit()
        self.search.setPlaceholderText(f"Search in {app_name}…")
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(self._activate)

        self.count_label = QLabel()
        self.count_label.setObjectName("count")

        self.lst = QListWidget()
        self.lst.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.lst.itemSelectionChanged.connect(self._on_row_changed)
        self.lst.itemDoubleClicked.connect(self._activate)

        v.addWidget(self.search)
        v.addWidget(self.count_label)
        v.addWidget(self.lst)
        outer.addWidget(shell)

        self.resize(640, 420)
        geo = QApplication.primaryScreen().geometry()
        self.move((geo.width() - self.width()) // 2, geo.height() // 5)

    # ── filtering ────────────────────────────────────────────────────────────

    def _filter(self, text: str):
        q = text.lower()
        self.filtered = (
            [e for e in self.elements if q in e["display"].lower()]
            if q else self.elements[:]
        )
        self.lst.clear()
        for el in self.filtered:
            icon = ROLE_ICONS.get(el["role"], "•")
            self.lst.addItem(QListWidgetItem(f"{icon}  {el['display']}"))
        n = len(self.filtered)
        self.count_label.setText(f"{n} element{'s' if n != 1 else ''}")
        if self.filtered:
            self.lst.setCurrentRow(0)
        else:
            self.overlay.hide()

    def _on_row_changed(self):
        row = self.lst.currentRow()
        if 0 <= row < len(self.filtered):
            self.overlay.show_at(self.filtered[row].get("rect"))

    # ── activation ───────────────────────────────────────────────────────────

    def _activate(self):
        row = self.lst.currentRow()
        if not (0 <= row < len(self.filtered)):
            return
        node = self.filtered[row]["node"]
        self.overlay.hide()
        self.hide()
        QTimer.singleShot(80, lambda: activate_element(node))
        QTimer.singleShot(160, QApplication.quit)

    # ── keyboard ─────────────────────────────────────────────────────────────

    def keyPressEvent(self, ev):
        k = ev.key()
        shift = bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)

        if k == Qt.Key.Key_Escape:
            self.overlay.hide()
            QApplication.quit()
        elif k == Qt.Key.Key_Down or (k == Qt.Key.Key_Tab and not shift):
            row = self.lst.currentRow()
            if row < self.lst.count() - 1:
                self.lst.setCurrentRow(row + 1)
        elif k == Qt.Key.Key_Up or (k == Qt.Key.Key_Tab and shift):
            row = self.lst.currentRow()
            if row > 0:
                self.lst.setCurrentRow(row - 1)
        elif k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._activate()
        else:
            self.search.setFocus()
            QApplication.sendEvent(self.search, ev)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    # Collect elements BEFORE Qt init so the focused app is still the target.
    desktop = pyatspi.Registry.getDesktop(0)
    app_node = get_focused_app(desktop)
    if not app_node:
        time.sleep(0.15)
        app_node = get_focused_app(desktop)
    if not app_node:
        sys.exit("[Error] No active app found.")

    elements: list = []
    collect_elements(app_node, elements)
    if not elements:
        sys.exit(f"[Info] No AT-SPI elements found in '{app_node.name}'.")

    qt_app = QApplication(sys.argv)
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    dialog = SearchDialog(elements, app_node.name or "app")
    dialog.show()
    dialog.search.setFocus()

    sys.exit(qt_app.exec())


if __name__ == "__main__":
    main()
