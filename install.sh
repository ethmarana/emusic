#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP="${XDG_DATA_HOME:-$HOME/.local/share}/neon-music"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/neon-music"
BIN="$HOME/.local/bin"
DESKTOP="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
command -v playerctl >/dev/null || { echo 'Install playerctl: sudo pacman -S playerctl' >&2; exit 1; }
/usr/bin/python -c 'import PyQt6.QtWidgets, PyQt6.QtNetwork' 2>/dev/null || {
    echo 'Install PyQt6: sudo pacman -S python-pyqt6' >&2; exit 1;
}
mkdir -p "$APP" "$CONFIG" "$BIN" "$DESKTOP"
# Back up only the files this installer owns. Never delete auth, caches,
# user settings, Waybar, or the main Hyprland configuration.
BACKUP="$APP/backup-$(date +%Y%m%d-%H%M%S)"
if [[ -f "$APP/spotify_widget.py" || -f "$APP/app.py" || -f "$CONFIG/hyprland.lua" ]]; then
    mkdir -p "$BACKUP"
    for file in spotify_widget.py app.py backend.py library.py; do
        [[ ! -f "$APP/$file" ]] || cp -p "$APP/$file" "$BACKUP/$file"
    done
    [[ ! -f "$CONFIG/hyprland.lua" ]] || cp -p "$CONFIG/hyprland.lua" "$BACKUP/hyprland.lua"
fi
install -m 644 "$HERE/app.py" "$HERE/backend.py" "$HERE/library.py" "$APP/"
install -m 644 "$HERE/hyprland.lua" "$CONFIG/hyprland.lua"
install -m 755 "$HERE/neon-music" "$BIN/neon-music"
/usr/bin/python - "$HERE/neon-music.desktop" "$DESKTOP/neon-music.desktop" "$BIN/neon-music" <<'PY'
from pathlib import Path
import sys
source, destination, launcher = map(Path, sys.argv[1:])
destination.write_text(source.read_text().replace('Exec=neon-music', 'Exec=' + str(launcher)))
PY
cat <<'OUT'

NEON MUSIC / MONO installed.
Your original Hyprland, Waybar, Spotify config, and authentication were not changed.
The previous widget source was backed up when present.

Your existing Lua dofile line can stay. If you have not added it, put this
line ONCE at the bottom of ~/.config/hypr/hyprland.lua:

    dofile(os.getenv("HOME") .. "/.config/neon-music/hyprland.lua")

Run hyprctl reload once to replace any old widget sizing rules.
Then launch: ~/.local/bin/neon-music
The widget enables its own scoped hiding rule before starting Spotify.

If a crash ever leaves Spotify hidden:
    ~/.local/bin/neon-music --restore
OUT
