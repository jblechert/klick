#!/usr/bin/env python3
"""
wayland-palette.py – Globale Command Palette für Wayland
=========================================================
Liest über AT-SPI alle Buttons, Menüpunkte und interaktiven
Elemente der aktiven App und zeigt sie in fuzzel/wofi/rofi zur Suche an.

Abhängigkeiten:
  pip install pyatspi
  + fuzzel ODER wofi ODER rofi installiert

Hotkey-Setup (Beispiel für Sway/Hyprland):
  sway:     bindsym $mod+Space exec python3 /pfad/wayland-palette.py
  hyprland: bind = $mainMod, Space, exec, python3 /pfad/wayland-palette.py
  GNOME:    Einstellungen → Tastaturkürzel → Benutzerdefiniert
"""

import subprocess
import sys
import os
import time

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

# Welche Rollen sollen angezeigt werden?
INTERESTING_ROLES = {
    "push button",
    "toggle button",
    "menu item",
    "check menu item",
    "radio menu item",
    "menu",
    "check box",
    "radio button",
    "combo box",
    "tool bar",
    "split pane",
    "page tab",
}

# Maximale Baumtiefe (verhindert Endlosrekursion bei komplexen Apps)
MAX_DEPTH = 20

# ---------------------------------------------------------------------------
# Launcher-Erkennung
# ---------------------------------------------------------------------------

def find_launcher():
    """Gibt den ersten verfügbaren dmenu-kompatiblen Launcher zurück."""
    candidates = [
        ("fuzzel",  ["fuzzel", "--dmenu", "--prompt", "  Aktion: ", "--width", "60"]),
        ("wofi",    ["wofi", "--dmenu", "--prompt", "Aktion: ", "--insensitive", "--allow-markup"]),
        ("rofi",    ["rofi", "-dmenu", "-i", "-p", "Aktion"]),
        ("dmenu",   ["dmenu", "-p", "Aktion:"]),
    ]
    for name, cmd in candidates:
        result = subprocess.run(["which", name], capture_output=True)
        if result.returncode == 0:
            return name, cmd
    return None, None

# ---------------------------------------------------------------------------
# AT-SPI: Aktive App finden
# ---------------------------------------------------------------------------

def get_focused_app(desktop):
    """Gibt die App zurück, deren Fenster gerade den Fokus hat."""
    for app in desktop:
        if app is None:
            continue
        try:
            for i in range(app.childCount):
                window = app.getChildAtIndex(i)
                state = window.getState()
                if state.contains(pyatspi.STATE_ACTIVE):
                    return app
        except Exception:
            continue
    return None

# ---------------------------------------------------------------------------
# AT-SPI: UI-Elemente rekursiv sammeln
# ---------------------------------------------------------------------------

def collect_elements(node, results, depth=0, breadcrumb=""):
    """Durchläuft den AT-SPI-Baum und sammelt interaktive Elemente."""
    if depth > MAX_DEPTH:
        return

    try:
        role = node.getRoleName()
        name = (node.name or "").strip()

        # Breadcrumb für Menüpfade aufbauen
        if role == "menu" and name:
            current_crumb = f"{breadcrumb} › {name}" if breadcrumb else name
        else:
            current_crumb = breadcrumb

        # Interessante Elemente mit Namen speichern
        if role in INTERESTING_ROLES and name:
            display_name = f"{current_crumb} › {name}" if current_crumb and role != "menu" else name
            results.append({
                "display":  display_name,
                "role":     role,
                "node":     node,
                "name":     name,
            })

        # Kinder traversieren
        for i in range(node.childCount):
            try:
                child = node.getChildAtIndex(i)
                collect_elements(child, results, depth + 1, current_crumb)
            except Exception:
                continue

    except Exception:
        pass

# ---------------------------------------------------------------------------
# AT-SPI: Element aktivieren
# ---------------------------------------------------------------------------

def activate_element(node):
    """Führt die primäre Aktion eines AT-SPI-Elements aus."""
    try:
        action = node.queryAction()
        # Bevorzugte Aktionsnamen
        preferred = ["click", "activate", "press", "toggle", "expand or contract", "open"]
        for i in range(action.nActions):
            if action.getName(i).lower() in preferred:
                action.doAction(i)
                return True
        # Fallback: erste verfügbare Aktion
        if action.nActions > 0:
            action.doAction(0)
            return True
    except Exception:
        pass

    # Fallback: Fokus setzen und Enter simulieren
    try:
        node.grabFocus()
    except Exception:
        pass

    return False

# ---------------------------------------------------------------------------
# Launcher anzeigen und Auswahl verarbeiten
# ---------------------------------------------------------------------------

def show_palette(elements, launcher_name, launcher_cmd):
    """Zeigt die Command Palette und gibt das gewählte Element zurück."""
    ROLE_ICONS = {
        "push button":      "⬡",
        "toggle button":    "⬡",
        "menu item":        "▸",
        "check menu item":  "✓",
        "radio menu item":  "◉",
        "menu":             "▾",
        "check box":        "☐",
        "radio button":     "◎",
        "combo box":        "▼",
        "tool bar":         "━",
        "page tab":         "⬜",
    }

    lines = []
    for el in elements:
        icon = ROLE_ICONS.get(el["role"], "•")
        lines.append(f"{icon}  {el['display']}")

    input_text = "\n".join(lines)

    try:
        result = subprocess.run(
            launcher_cmd,
            input=input_text,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except FileNotFoundError:
        print(f"[Fehler] Launcher '{launcher_name}' nicht gefunden.", file=sys.stderr)
    except Exception as e:
        print(f"[Fehler] Launcher: {e}", file=sys.stderr)

    return None

# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main():
    # pyatspi importieren (erst hier, damit Fehlermeldung klar ist)
    global pyatspi
    try:
        import pyatspi as _pyatspi
        pyatspi = _pyatspi
    except ImportError:
        print(
            "[Fehler] pyatspi nicht installiert.\n"
            "  Installieren mit: pip install pyatspi",
            file=sys.stderr,
        )
        sys.exit(1)

    # Launcher suchen
    launcher_name, launcher_cmd = find_launcher()
    if not launcher_name:
        print(
            "[Fehler] Kein Launcher gefunden.\n"
            "  Installiere einen dieser: fuzzel, wofi, rofi, dmenu",
            file=sys.stderr,
        )
        sys.exit(1)

    # Desktop-Baum holen
    desktop = pyatspi.Registry.getDesktop(0)

    # Aktive App finden
    app = get_focused_app(desktop)
    if not app:
        # Kurz warten und nochmal versuchen (Hotkey-Lag)
        time.sleep(0.15)
        app = get_focused_app(desktop)

    if not app:
        print("[Fehler] Keine aktive App gefunden.", file=sys.stderr)
        sys.exit(1)

    app_name = app.name or "Unbekannte App"

    # UI-Elemente sammeln
    elements = []
    collect_elements(app, elements)

    if not elements:
        print(f"[Info] Keine AT-SPI-Elemente in '{app_name}' gefunden.", file=sys.stderr)
        print("       Möglicherweise unterstützt diese App kein AT-SPI.", file=sys.stderr)
        sys.exit(1)

    # Palette anzeigen
    # Launcher-Prompt mit App-Name anpassen (nur fuzzel/wofi/rofi)
    if launcher_name == "fuzzel":
        launcher_cmd = ["fuzzel", "--dmenu", "--prompt", f"  {app_name}: ", "--width", "70"]
    elif launcher_name == "wofi":
        launcher_cmd = ["wofi", "--dmenu", "--prompt", f"{app_name}: ", "--insensitive"]
    elif launcher_name == "rofi":
        launcher_cmd = ["rofi", "-dmenu", "-i", "-p", app_name]

    selection = show_palette(elements, launcher_name, launcher_cmd)

    if not selection:
        sys.exit(0)  # Abgebrochen

    # Gewähltes Element finden und aktivieren
    for el in elements:
        icon = {"push button": "⬡", "toggle button": "⬡", "menu item": "▸",
                "check menu item": "✓", "radio menu item": "◉", "menu": "▾",
                "check box": "☐", "radio button": "◎", "combo box": "▼",
                "tool bar": "━", "page tab": "⬜"}.get(el["role"], "•")
        expected = f"{icon}  {el['display']}"
        if expected == selection:
            success = activate_element(el["node"])
            if success:
                print(f"[OK] Aktiviert: {el['display']}")
            else:
                print(f"[Warn] Element gefunden, aber Aktivierung fehlgeschlagen: {el['display']}")
            break
    else:
        print(f"[Warn] Gewähltes Element nicht zuordenbar: {selection}", file=sys.stderr)


if __name__ == "__main__":
    main()
