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

IS_ARCH=0
command -v pacman &>/dev/null && [ -f /etc/arch-release ] && IS_ARCH=1

# pairs: "python import statement" "pacman package"
declare -A _IMPORT=(
    [pyatspi]="import pyatspi"
    [pywayland]="import pywayland"
    [dbus]="import dbus"
    [pyqt6]="import PyQt6.QtCore"
    [gobject]="from gi.repository import GLib"
)
declare -A _PACMAN=(
    [pyatspi]="python-pyatspi"
    [pywayland]="python-pywayland"
    [dbus]="python-dbus"
    [pyqt6]="python-pyqt6"
    [gobject]="python-gobject"
)

missing_pkgs=()
for key in pyatspi pywayland dbus pyqt6 gobject; do
    if ! python3 -c "${_IMPORT[$key]}" 2>/dev/null; then
        echo "  MISSING: ${_PACMAN[$key]}"
        missing_pkgs+=("${_PACMAN[$key]}")
    fi
done

if [ "${#missing_pkgs[@]}" -gt 0 ]; then
    if [ "$IS_ARCH" -eq 1 ]; then
        echo ""
        read -r -p "Install missing packages with pacman? [Y/n] " _ans
        _ans="${_ans:-Y}"
        if [[ "$_ans" =~ ^[Yy]$ ]]; then
            sudo pacman -S --needed "${missing_pkgs[@]}"
            # verify after install
            for key in pyatspi pywayland dbus pyqt6 gobject; do
                python3 -c "${_IMPORT[$key]}" 2>/dev/null \
                    || { echo "  Still missing after install: ${_PACMAN[$key]}"; exit 1; }
            done
        else
            exit 1
        fi
    else
        echo ""
        echo "Install the missing packages listed above, then re-run install.sh."
        exit 1
    fi
fi

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

# ── 7. optional: patch .desktop files for accessibility ──────────────────────
echo ""
echo "==> Scanning installed apps for accessibility compatibility..."
python3 - << 'PYEOF'
import os, pathlib, re, sys

FLAG = "--force-renderer-accessibility"
USER_APPS = pathlib.Path.home() / ".local/share/applications"

CHROMIUM_BINS = {
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
    "brave-browser", "brave", "vivaldi-stable", "vivaldi", "opera",
    "thorium", "ungoogled-chromium", "microsoft-edge", "microsoft-edge-stable",
    "code", "code-oss", "codium", "vscodium",
}

def is_electron_binary(binary: str) -> bool:
    try:
        p = pathlib.Path(binary).resolve()
        if not p.is_file():
            return False
        data = p.read_bytes()
        if data[:2] == b'#!' and b'electron' in data[:512].lower():
            return True
        for d in [p.parent, p.parent.parent]:
            if (d / "resources" / "app.asar").exists():
                return True
    except Exception:
        pass
    return False

def find_candidates():
    seen, results = set(), []
    for d in [pathlib.Path("/usr/share/applications"),
              pathlib.Path.home() / ".local/share/applications"]:
        if not d.exists():
            continue
        for f in sorted(d.glob("*.desktop")):
            if f.name in seen:
                continue
            seen.add(f.name)
            try:
                text = f.read_text()
                if FLAG in text:
                    continue
                exec_lines = [l for l in text.splitlines() if re.match(r'^Exec\s*=', l)]
                if not exec_lines:
                    continue
                binary = exec_lines[0].split("=", 1)[1].strip().split()[0]
                bname = os.path.basename(binary)
                if bname in CHROMIUM_BINS or is_electron_binary(binary):
                    name_m = re.search(r'^Name=(.+)$', text, re.MULTILINE)
                    results.append((name_m.group(1) if name_m else f.stem, f, text))
            except Exception:
                continue
    return results

def patch_exec(text: str) -> str:
    def _fix(m):
        val = m.group(1)
        if FLAG in val:
            return "Exec=" + val
        parts = val.split(None, 1)
        if not parts or os.path.basename(parts[0]) in ("env", "sh", "bash", "fish"):
            return "Exec=" + val
        rest = (" " + parts[1]) if len(parts) > 1 else ""
        return f"Exec={parts[0]} {FLAG}{rest}"
    return re.sub(r'^Exec=(.+)$', _fix, text, flags=re.MULTILINE)

candidates = find_candidates()
if not candidates:
    print("  No Electron/Chromium apps found that need patching.")
    sys.exit(0)

print(f"  Found {len(candidates)} app(s) that may benefit from {FLAG}:\n")
for i, (name, path, _) in enumerate(candidates, 1):
    print(f"  [{i}] {name}  ({path})")
print()

try:
    sys.stdout.write("  Patch all? [Y/n/list e.g. 1,3] ")
    sys.stdout.flush()
    with open("/dev/tty") as tty:
        ans = tty.readline().strip()
except Exception:
    sys.exit(0)

if not ans or ans.lower() == "y":
    selected = candidates
elif ans.lower() == "n":
    sys.exit(0)
else:
    try:
        indices = [int(x.strip()) - 1 for x in ans.split(",")]
        selected = [candidates[i] for i in indices if 0 <= i < len(candidates)]
    except Exception:
        print("  Invalid input, skipping.")
        sys.exit(0)

USER_APPS.mkdir(parents=True, exist_ok=True)
for name, src_path, text in selected:
    dest = USER_APPS / src_path.name
    dest.write_text(patch_exec(text))
    print(f"  Patched → {dest}")
PYEOF

# ── 8. PATH reminder ──────────────────────────────────────────────────────────
if ! echo ":$PATH:" | grep -q ":$HOME/.local/bin:"; then
    echo ""
    echo "Note: $HOME/.local/bin is not in your PATH."
    echo "Add to your shell config:  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo ""
echo "Done. Press $SHORTCUT over any window to search its UI elements."
echo "(Daemon runs in background; main.py launches on demand and closes on focus loss.)"
