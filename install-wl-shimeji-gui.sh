#!/usr/bin/env bash
set -euo pipefail
BIN="$HOME/.local/bin"; APPS="$HOME/.local/share/applications"; mkdir -p "$BIN" "$APPS"
install -m755 "$(dirname "$0")/wl_shimeji_gui.py" "$BIN/wl-shimeji-gui"
cat > "$APPS/wl-shimeji-gui.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=wl_shimeji GUI
Comment=Qt frontend for wl_shimeji
Exec=$BIN/wl-shimeji-gui
Icon=applications-games
Terminal=false
Categories=Utility;Game;
StartupNotify=true
EOF
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" || true
echo "Installed. Run: $BIN/wl-shimeji-gui"
