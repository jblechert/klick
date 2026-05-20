#!/usr/bin/env bash
# install.sh – set up atspi-search and bind it to a global shortcut in KDE Plasma 6
#
# Usage:  ./install.sh [shortcut]
#   shortcut defaults to Meta+F
#   Examples: ./install.sh "Meta+E"  ./install.sh "Ctrl+Alt+Space"
#
# A small background daemon (atspi-search-hotkey.py) registers the shortcut
# with kglobalaccel using the SetPresent flag (0x6) so KWin physically grabs
# the key.  When triggered the daemon launches main.py directly as a child
# process (full session environment, no systemd / KIO in the path).
# main.py closes itself when it loses window focus.
set -euo pipefail

SHORTCUT="${1:-Meta+F}"
INSTALL_DIR="$HOME/.local/share/atspi-search"
BIN="$HOME/.local/bin/atspi-search"
HOTKEY_BIN="$HOME/.local/bin/atspi-search-hotkey"
AUTOSTART_DIR="$HOME/.config/autostart"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 1. dependency check ───────────────────────────────────────────────────────
echo "==> Checking Python dependencies..."
missing=0
for pkg in pyatspi pywayland dbus; do
    python3 -c "import $pkg" 2>/dev/null || { echo "  MISSING: $pkg  →  pip install $pkg  or  pacman -S python-dbus"; missing=1; }
done
python3 -c "import PyQt6.QtCore" 2>/dev/null || { echo "  MISSING: PyQt6  →  pip install PyQt6"; missing=1; }
python3 -c "from gi.repository import GLib" 2>/dev/null || { echo "  MISSING: python-gobject  →  pacman -S python-gobject"; missing=1; }
[ "$missing" -eq 0 ] || exit 1

# ── 2. install files ──────────────────────────────────────────────────────────
echo "==> Installing to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/main.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/atspi-search-hotkey.py" "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR/wlr_layer_shell_unstable_v1" "$INSTALL_DIR/"

# ── 3. wrapper scripts ────────────────────────────────────────────────────────
echo "==> Creating wrappers in ~/.local/bin/..."
mkdir -p "$HOME/.local/bin"
cat > "$BIN" << EOF
#!/usr/bin/env bash
exec python3 "$INSTALL_DIR/main.py" "\$@"
EOF
chmod +x "$BIN"

cat > "$HOTKEY_BIN" << EOF
#!/usr/bin/env bash
exec python3 "$INSTALL_DIR/atspi-search-hotkey.py" "\$@"
EOF
chmod +x "$HOTKEY_BIN"

# ── 4. KDE autostart for the hotkey daemon ────────────────────────────────────
echo "==> Installing KDE autostart entry..."
mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/atspi-search-hotkey.desktop" << EOF
[Desktop Entry]
Type=Application
Name=atspi-search Hotkey
Comment=Global shortcut daemon for atspi-search
Exec=$HOTKEY_BIN $SHORTCUT
NoDisplay=true
X-KDE-autostart-phase=2
EOF

# ── 5. clean up leftovers from the desktop-file-based install attempt ─────────
echo "==> Cleaning up previous install artefacts (if any)..."
rm -f "$HOME/.local/share/kglobalaccel/atspi-search.desktop"
# Remove [services][atspi-search.desktop] from kglobalshortcutsrc
python3 - << 'PYEOF'
import pathlib, re
p = pathlib.Path.home() / ".config/kglobalshortcutsrc"
if not p.exists():
    raise SystemExit
text = p.read_text()
# Remove [services][atspi-search.desktop] subsection
cleaned = re.sub(r'^\[services\]\[atspi-search\.desktop\][^\n]*\n(?:[^\[]*\n)*', '',
                 text, flags=re.MULTILINE)
if cleaned != text:
    p.write_text(cleaned)
PYEOF

# ── 6. start / restart the hotkey daemon ─────────────────────────────────────
echo "==> Starting hotkey daemon..."
pkill -f "atspi-search-hotkey" 2>/dev/null || true
sleep 0.3
"$HOTKEY_BIN" "$SHORTCUT" &
disown
sleep 1   # wait for daemon to register with kglobalaccel

echo "    Shortcut $SHORTCUT is active."

# ── 7. PATH reminder ──────────────────────────────────────────────────────────
if ! echo ":$PATH:" | grep -q ":$HOME/.local/bin:"; then
    echo ""
    echo "Note: $HOME/.local/bin is not in your PATH."
    echo "Add to your shell config:  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo ""
echo "Done. Press $SHORTCUT over any window to search its UI elements."
echo "(Daemon runs in background; main.py launches on demand and closes on focus loss.)"
