#!/usr/bin/env bash
# install.sh – set up atspi-search and bind it to a global shortcut in KDE Plasma 6
#
# Usage:  ./install.sh [shortcut]
#   shortcut defaults to Meta+Shift+F
#   Examples: ./install.sh "Meta+E"  ./install.sh "Ctrl+Alt+Space"
set -euo pipefail

SHORTCUT="${1:-Meta+Shift+F}"
INSTALL_DIR="$HOME/.local/share/atspi-search"
BIN="$HOME/.local/bin/atspi-search"
KWIN_DIR="$HOME/.local/share/kwin/scripts/atspisearch"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 1. dependency check ───────────────────────────────────────────────────────
echo "==> Checking Python dependencies..."
missing=0
for pkg in pyatspi pywayland; do
    python3 -c "import $pkg" 2>/dev/null || { echo "  MISSING: $pkg  →  pip install $pkg"; missing=1; }
done
python3 -c "import PyQt6.QtCore" 2>/dev/null || { echo "  MISSING: PyQt6  →  pip install PyQt6"; missing=1; }
[ "$missing" -eq 0 ] || exit 1

# ── 2. install files ──────────────────────────────────────────────────────────
echo "==> Installing to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/main.py" "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR/wlr_layer_shell_unstable_v1" "$INSTALL_DIR/"

# ── 3. wrapper script ─────────────────────────────────────────────────────────
echo "==> Creating $BIN..."
mkdir -p "$HOME/.local/bin"
cat > "$BIN" << EOF
#!/usr/bin/env bash
exec python3 "$INSTALL_DIR/main.py" "\$@"
EOF
chmod +x "$BIN"

# ── 4. systemd user service ───────────────────────────────────────────────────
echo "==> Installing systemd user service..."
mkdir -p "$SYSTEMD_DIR"
cat > "$SYSTEMD_DIR/atspi-search.service" << EOF
[Unit]
Description=atspi-search UI element finder

[Service]
Type=oneshot
Slice=app.slice
ExecStart=$BIN
EOF
systemctl --user daemon-reload

# ── 5. KWin script ────────────────────────────────────────────────────────────
echo "==> Installing KWin script (shortcut: $SHORTCUT)..."
mkdir -p "$KWIN_DIR/contents/code"

cat > "$KWIN_DIR/metadata.json" << 'EOF'
{
    "KPackageStructure": "KWin/Script",
    "KPlugin": {
        "Description": "Launch atspi-search element search overlay",
        "Name": "Search UI Elements",
        "UniqueId": "atspisearch"
    }
}
EOF

# callDBus StartUnit uses only two string args — works cleanly with KWin's callDBus.
cat > "$KWIN_DIR/contents/code/main.js" << EOF
registerShortcut(
    "atspi-search",
    "Search UI Elements",
    "$SHORTCUT",
    function () {
        callDBus(
            "org.freedesktop.systemd1",
            "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager",
            "StartUnit",
            "atspi-search.service",
            "replace"
        );
    }
);
EOF

# ── 6. enable script and register shortcut ────────────────────────────────────
echo "==> Enabling KWin script..."
kwriteconfig6 --file kwinrc --group Plugins --key atspisearchEnabled true

# Write the shortcut key directly into kglobalshortcutsrc.
# reconfigure alone does not override an existing (possibly empty) stored binding,
# so we set it explicitly before loading the script.
kwriteconfig6 --file kglobalshortcutsrc --group kwin \
    --key "atspi-search" "$SHORTCUT,$SHORTCUT,Search UI Elements"

# Load the script and start execution (reconfigure alone does not load new scripts).
echo "==> Loading KWin script..."
qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.unloadScript atspisearch 2>/dev/null || true
qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.loadScript \
    "$KWIN_DIR/contents/code/main.js" "atspisearch"
qdbus6 org.kde.KWin /Scripting org.kde.kwin.Scripting.start
qdbus6 org.kde.KWin /KWin reconfigure

echo "    Shortcut $SHORTCUT is active."

# ── 7. PATH reminder ──────────────────────────────────────────────────────────
if ! echo ":$PATH:" | grep -q ":$HOME/.local/bin:"; then
    echo ""
    echo "Note: $HOME/.local/bin is not in your PATH."
    echo "Add to your shell config:  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo ""
echo "Done. Press $SHORTCUT over any window to search its UI elements."
