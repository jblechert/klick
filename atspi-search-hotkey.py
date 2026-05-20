#!/usr/bin/env python3
"""
atspi-search-hotkey — registers a global shortcut with kglobalaccel and
launches main.py directly when triggered.

Usage: atspi-search-hotkey [shortcut]
  shortcut  human-readable key combo, e.g. "Meta+F" (default)

How it works
------------
kglobalaccel does NOT call back into your process.  Instead it emits
the globalShortcutPressed signal on the component's D-Bus object.  We:
  1. Call doRegister + setShortcut (flags 0x6 = SetPresent|NoAutoloading)
     so KWin physically grabs the key.
  2. Call getComponent to obtain the object-path of our component.
  3. Subscribe to org.kde.kglobalaccel.Component.globalShortcutPressed
     on that path and react when the signal arrives.

main.py is launched as a direct child process (no systemd) so it inherits
the full session environment and closes on focus loss by itself.
"""

import os
import signal
import subprocess
import sys

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

# ── Qt modifier/key constants ──────────────────────────────────────────────────

_MODS: dict[str, int] = {
    "meta":  0x10000000,
    "ctrl":  0x04000000,
    "alt":   0x08000000,
    "shift": 0x02000000,
}

_NAMED_KEYS: dict[str, int] = {
    "space":     0x20,
    "return":    0x01000004,
    "escape":    0x01000000,
    "tab":       0x01000001,
    "backspace": 0x01000003,
    "f1":  0x01000030, "f2":  0x01000031, "f3":  0x01000032, "f4":  0x01000033,
    "f5":  0x01000034, "f6":  0x01000035, "f7":  0x01000036, "f8":  0x01000037,
    "f9":  0x01000038, "f10": 0x01000039, "f11": 0x0100003a, "f12": 0x0100003b,
}


def shortcut_to_keycode(s: str) -> int:
    """Convert 'Meta+F' or 'Ctrl+Alt+T' to a Qt key code integer."""
    code = 0
    for part in s.split("+"):
        pl = part.lower()
        if pl in _MODS:
            code |= _MODS[pl]
        elif pl in _NAMED_KEYS:
            code |= _NAMED_KEYS[pl]
        elif len(part) == 1:
            code |= ord(part.upper())
    return code


# ── D-Bus constants ────────────────────────────────────────────────────────────

COMPONENT_UNIQUE  = "atspisearch"
COMPONENT_DISPLAY = "atspi-search"
ACTION_UNIQUE     = "launch"
ACTION_DISPLAY    = "Search UI Elements"

KGLOBALACCEL_BUS        = "org.kde.kglobalaccel"
KGLOBALACCEL_PATH       = "/kglobalaccel"
KGLOBALACCEL_IFACE      = "org.kde.KGlobalAccel"
KGLOBALACCEL_COMP_IFACE = "org.kde.kglobalaccel.Component"

# SetPresent (0x2): mark the shortcut as actively present so kglobalacceld
#   calls setIsPresent(true) → setActive() → _manager->grabKey() → KWin grabs it.
#   Without this the key is recorded in the registry but NEVER grabbed by KWin.
# NoAutoloading (0x4): use the key we supply, not whatever is saved in config.
# Combined: 0x2 | 0x4 = 0x6
SET_SHORTCUT_FLAGS = dbus.UInt32(0x6)


# main.py lives next to this file in the install directory
_MAIN_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
_search_proc: "subprocess.Popen | None" = None


def _launch_search():
    global _search_proc
    # Don't stack multiple instances — if the previous one is still running, skip.
    if _search_proc is not None and _search_proc.poll() is None:
        print("[atspi-search-hotkey] already running, skipping", flush=True)
        return
    print("[atspi-search-hotkey] triggered — launching main.py", flush=True)
    _search_proc = subprocess.Popen(
        [sys.executable, _MAIN_PY],
        start_new_session=False,   # inherit session so Wayland/AT-SPI env is available
    )


def _on_shortcut_pressed(component_unique, shortcut_unique, timestamp):
    """Called when kglobalaccel fires globalShortcutPressed on our component."""
    if component_unique == COMPONENT_UNIQUE and shortcut_unique == ACTION_UNIQUE:
        _launch_search()


def _register_and_connect(bus: dbus.SessionBus, key_code: int) -> bool:
    """
    Register the shortcut with kglobalaccel and subscribe to the
    globalShortcutPressed signal.  Returns True on success.
    """
    try:
        kga_obj   = bus.get_object(KGLOBALACCEL_BUS, KGLOBALACCEL_PATH)
        kga_iface = dbus.Interface(kga_obj, KGLOBALACCEL_IFACE)

        action_id = dbus.Array(
            [COMPONENT_UNIQUE, ACTION_UNIQUE, COMPONENT_DISPLAY, ACTION_DISPLAY],
            signature="s",
        )

        # Step 1 – register the action so kglobalaccel knows about it.
        kga_iface.doRegister(action_id)

        # Step 2 – assign the key.  ai = list of Qt key-code ints.
        keys = dbus.Array([dbus.Int32(key_code)], signature="i")
        kga_iface.setShortcut(action_id, keys, SET_SHORTCUT_FLAGS)
        print(f"[atspi-search-hotkey] registered key={key_code:#010x}", flush=True)

        # Step 3 – obtain the object-path of our component so we can
        #           connect to its globalShortcutPressed signal.
        comp_path = str(kga_iface.getComponent(COMPONENT_UNIQUE))
        comp_obj  = bus.get_object(KGLOBALACCEL_BUS, comp_path)

        comp_obj.connect_to_signal(
            "globalShortcutPressed",
            _on_shortcut_pressed,
            dbus_interface=KGLOBALACCEL_COMP_IFACE,
        )
        print(f"[atspi-search-hotkey] listening on {comp_path}", flush=True)
        return True

    except dbus.DBusException as exc:
        print(f"[atspi-search-hotkey] registration failed: {exc}",
              file=sys.stderr, flush=True)
        return False


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    shortcut = sys.argv[1] if len(sys.argv) > 1 else "Meta+F"
    key_code = shortcut_to_keycode(shortcut)

    bus = dbus.SessionBus()
    _register_and_connect(bus, key_code)

    loop = GLib.MainLoop()
    signal.signal(signal.SIGTERM, lambda *_: loop.quit())
    signal.signal(signal.SIGINT,  lambda *_: loop.quit())
    print(f"[atspi-search-hotkey] listening for {shortcut!r} ({key_code:#010x})",
          flush=True)
    loop.run()


if __name__ == "__main__":
    main()
