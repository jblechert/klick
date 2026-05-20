#!/usr/bin/env python3
"""
atspi-search – KDE element search overlay with live highlighting

Dependencies:
    pip install pyatspi PyQt6 pywayland
    pacman -S wlr-protocols   (then regenerate wlr_layer_shell_unstable_v1/)
"""

import mmap
import os
import signal
import struct
import subprocess
import sys
import time

try:
    import pyatspi
except ImportError:
    sys.exit("Error: pyatspi not installed — run: pip install pyatspi")

try:
    from PyQt6.QtCore import Qt, QEvent, QRect, QSocketNotifier, QTimer
    from PyQt6.QtWidgets import (
        QApplication, QFrame, QLabel, QLineEdit,
        QListWidget, QListWidgetItem, QVBoxLayout, QWidget,
    )
except ImportError:
    sys.exit("Error: PyQt6 not installed — run: pip install PyQt6")

try:
    from pywayland.client import Display as WlDisplay
    from pywayland.protocol.wayland import WlCompositor, WlShm
    from wlr_layer_shell_unstable_v1 import ZwlrLayerShellV1, ZwlrLayerSurfaceV1
except ImportError as e:
    sys.exit(f"Error: Wayland layer-shell setup incomplete — {e}")

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


# ── Wayland highlight overlay ─────────────────────────────────────────────────

def _render_highlight(width: int, height: int) -> bytes:
    """Return an ARGB8888 buffer: semi-transparent blue fill with 2px solid border."""
    # ARGB8888 on little-endian: uint32 = (A<<24)|(R<<16)|(G<<8)|B
    fill   = struct.pack("<I", (50  << 24) | (99 << 16) | (162 << 8) | 255)
    border = struct.pack("<I", (220 << 24) | (99 << 16) | (162 << 8) | 255)
    bw = 2
    full_border_row = border * width
    rows: list[bytes] = []
    for y in range(height):
        if y < bw or y >= height - bw:
            rows.append(full_border_row)
        else:
            row = bytearray(fill * width)
            for x in list(range(bw)) + list(range(width - bw, width)):
                row[x * 4: x * 4 + 4] = border
            rows.append(bytes(row))
    return b"".join(rows)


class WaylandHighlightOverlay:
    """Layer-shell surface that draws a highlight rect at exact screen coordinates."""

    def __init__(self):
        self._wl: WlDisplay = WlDisplay()
        self._wl.connect()

        self._compositor: WlCompositor | None = None
        self._shm:        WlShm         | None = None
        self._shell                      = None   # ZwlrLayerShellV1Proxy

        self._wl_surface     = None
        self._layer_surface  = None
        self._wl_buffer      = None
        self._shm_fd:  int | None       = None
        self._shm_map: mmap.mmap | None = None
        self._pending: QRect | None     = None

        registry = self._wl.get_registry()
        registry.dispatcher["global"] = self._on_global
        self._wl.roundtrip()

        if not self._shell:
            print("[Warning] zwlr_layer_shell_v1 unavailable — highlight disabled",
                  file=sys.stderr)
            return

        # Pump Wayland events inside Qt's event loop via fd watcher.
        app = QApplication.instance()
        self._notifier = QSocketNotifier(
            self._wl.get_fd(), QSocketNotifier.Type.Read, app
        )
        self._notifier.activated.connect(self._on_readable)

    # ── Wayland registry ──────────────────────────────────────────────────────

    def _on_global(self, registry, id_: int, interface: str, version: int):
        if interface == "wl_compositor":
            self._compositor = registry.bind(id_, WlCompositor, min(version, 4))
        elif interface == "wl_shm":
            self._shm = registry.bind(id_, WlShm, min(version, 1))
        elif interface == "zwlr_layer_shell_v1":
            self._shell = registry.bind(id_, ZwlrLayerShellV1, min(version, 4))

    def _on_readable(self):
        self._wl.dispatch(block=True)
        self._wl.flush()

    # ── Public interface ──────────────────────────────────────────────────────

    def show_at(self, rect: QRect | None):
        self._drop_surface()
        if not rect or rect.isEmpty() or not self._shell:
            return

        self._pending = rect
        pad = 4
        w, h = rect.width() + pad * 2, rect.height() + pad * 2

        self._wl_surface = self._compositor.create_surface()

        # Pass-through for pointer input.
        empty = self._compositor.create_region()
        self._wl_surface.set_input_region(empty)
        empty.destroy()

        self._layer_surface = self._shell.get_layer_surface(
            self._wl_surface, None,
            ZwlrLayerShellV1.layer.overlay,
            "atspi-highlight",
        )

        anchor = int(ZwlrLayerSurfaceV1.anchor.top | ZwlrLayerSurfaceV1.anchor.left)
        self._layer_surface.set_size(w, h)
        self._layer_surface.set_anchor(anchor)
        self._layer_surface.set_margin(rect.y() - pad, 0, 0, rect.x() - pad)
        self._layer_surface.set_exclusive_zone(-1)
        self._layer_surface.set_keyboard_interactivity(
            ZwlrLayerSurfaceV1.keyboard_interactivity.none
        )
        self._layer_surface.dispatcher["configure"] = self._on_configure
        self._layer_surface.dispatcher["closed"]    = self._on_closed

        # Empty initial commit — compositor replies with configure.
        self._wl_surface.commit()
        self._wl.flush()

    def hide(self):
        self._drop_surface()

    def destroy(self):
        self._drop_surface()
        if hasattr(self, "_notifier"):
            self._notifier.setEnabled(False)
        if self._wl:
            self._wl.disconnect()
            self._wl = None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_configure(self, layer_surface, serial: int, _w: int, _h: int):
        layer_surface.ack_configure(serial)

        rect = self._pending
        if rect is None or self._wl_surface is None:
            return

        pad = 4
        w, h = rect.width() + pad * 2, rect.height() + pad * 2
        stride, size = w * 4, w * 4 * h

        self._free_buffer()
        self._shm_fd = os.memfd_create("atspi-highlight")
        os.ftruncate(self._shm_fd, size)
        self._shm_map = mmap.mmap(self._shm_fd, size)
        self._shm_map.write(_render_highlight(w, h))
        self._shm_map.flush()

        pool = self._shm.create_pool(self._shm_fd, size)
        self._wl_buffer = pool.create_buffer(
            0, w, h, stride, WlShm.format.argb8888.value
        )
        pool.destroy()

        self._wl_surface.attach(self._wl_buffer, 0, 0)
        self._wl_surface.damage_buffer(0, 0, w, h)
        self._wl_surface.commit()
        self._wl.flush()

    def _on_closed(self, _layer_surface):
        self._layer_surface = None
        self._wl_surface = None

    def _free_buffer(self):
        if self._wl_buffer:
            self._wl_buffer.destroy()
            self._wl_buffer = None
        if self._shm_map:
            self._shm_map.close()
            self._shm_map = None
        if self._shm_fd is not None:
            os.close(self._shm_fd)
            self._shm_fd = None

    def _drop_surface(self):
        self._free_buffer()
        for attr in ("_layer_surface", "_wl_surface"):
            obj = getattr(self, attr, None)
            if obj:
                try:
                    obj.destroy()
                except Exception:
                    pass
                setattr(self, attr, None)
        self._pending = None
        if self._wl:
            self._wl.flush()


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
    def __init__(self, elements: list, app_name: str, overlay: WaylandHighlightOverlay):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.elements = elements
        self.filtered: list = []
        self.overlay = overlay
        self._activated_once = False   # guard: only quit on focus-loss after first activation
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

    # ── filtering ─────────────────────────────────────────────────────────────

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

    # ── activation ────────────────────────────────────────────────────────────

    def _quit(self):
        self.overlay.destroy()
        QApplication.quit()

    def _activate(self):
        row = self.lst.currentRow()
        if not (0 <= row < len(self.filtered)):
            return
        node = self.filtered[row]["node"]
        self.overlay.hide()
        self.hide()
        QTimer.singleShot(80,  lambda: activate_element(node))
        QTimer.singleShot(160, self._quit)

    # ── focus loss ────────────────────────────────────────────────────────────

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() == QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                self._activated_once = True
            elif self._activated_once:
                self._quit()

    # ── keyboard ──────────────────────────────────────────────────────────────

    def keyPressEvent(self, ev):
        k = ev.key()
        shift = bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)

        if k == Qt.Key.Key_Escape:
            self._quit()
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

def _notify(summary: str, body: str) -> None:
    """Show a desktop notification (best-effort; silent if notify-send missing)."""
    try:
        subprocess.run(
            ["notify-send", "-a", "atspi-search", "-i", "dialog-information",
             "-t", "4000", summary, body],
            check=False,
        )
    except FileNotFoundError:
        pass  # notify-send not installed; silently ignore


def main():
    # Collect AT-SPI elements BEFORE Qt takes focus away from the target app.
    desktop = pyatspi.Registry.getDesktop(0)
    app_node = get_focused_app(desktop)
    if not app_node:
        time.sleep(0.15)
        app_node = get_focused_app(desktop)
    if not app_node:
        _notify("atspi-search", "No focused window found.\nMake sure a window is active before pressing the shortcut.")
        sys.exit("[Error] No active app found.")

    elements: list = []
    collect_elements(app_node, elements)
    if not elements:
        name = app_node.name or "this application"
        _notify(
            f"{name} is not supported",
            f"'{name}' does not expose accessible UI elements.\n"
            "Apps like VS Code and Chromium require accessibility to be explicitly enabled:\n"
            "• Chromium/Chrome: launch with --force-renderer-accessibility\n"
            "• VS Code: add \"editor.accessibilitySupport\": \"on\" to settings.json",
        )
        sys.exit(f"[Info] No AT-SPI elements found in '{name}'.")

    qt_app = QApplication(sys.argv)
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    overlay = WaylandHighlightOverlay()
    dialog = SearchDialog(elements, app_node.name or "app", overlay)
    dialog.show()
    dialog.activateWindow()
    dialog.search.setFocus()

    sys.exit(qt_app.exec())


if __name__ == "__main__":
    main()
