#!/usr/bin/env bash
# Runs phase0.py and saves both a readable report and a JSON dump into
# ./reports/. Safe to use as a Steam shortcut target — Steam gives us no
# visible terminal, so everything is captured to disk.
#
# Auto-names the output by which world it detected itself in, so you can't
# mix up the Steam-Input-on and Steam-Input-off runs.

set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$DIR/reports"
mkdir -p "$OUT"

if [ -n "${1:-}" ]; then
    TAG="$1"
elif [ -n "${SDL_GAMECONTROLLER_IGNORE_DEVICES:-}" ]; then
    TAG="steam-input-ON"
elif [ -n "${SteamAppId:-}${STEAM_COMPAT_CLIENT_INSTALL_PATH:-}" ]; then
    TAG="steam-input-OFF"
else
    TAG="desktop-baseline"
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
BASE="$OUT/$TAG-$STAMP"

python3 "$DIR/phase0.py" --json "$BASE.json" 2>&1 | tee "$BASE.txt"

echo
echo "Saved:"
echo "  $BASE.txt"
echo "  $BASE.json"

# When launched from Steam there's no terminal to read; pause only if there is.
if [ -t 0 ]; then
    echo
    read -r -p "Press Enter to close..." _
fi
