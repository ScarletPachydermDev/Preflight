#!/usr/bin/env bash
# Steam shortcut target for preflight.
#
#   Target:         /home/deck/preflight/launch.sh
#   Launch Options: "/run/media/deck/mSD/ROMs/Switch/Your Game.nsp"
#
# Set that shortcut's controller layout to Steam Input DISABLED, so the Xbox,
# Stadia and 8BitDo reach Ryujinx as real hardware. The Steam Controller stays
# virtual either way — Valve always manages its own pad — and it is the only
# virtual one, so nothing can collide with it.
#
# If a run fails, the log below is the place to look; Steam gives us no
# terminal in Game Mode.

set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$DIR/state"
LOG="$DIR/state/launch.log"

{
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="
    echo "rom: ${1:-<none, opening Ryujinx game list>}"
} >>"$LOG"

# Not exec'd: keeping this shell alive for the pipeline means Steam continues
# to show the game as running for as long as Ryujinx is up.
python3 "$DIR/preflight.py" "$@" 2>&1 | tee -a "$LOG"
