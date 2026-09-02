#!/usr/bin/env bash
# Steam shortcut target for preflight.
#
#   Target:         /home/deck/preflight/preflight.sh
#   Launch Options: "/run/media/deck/mSD/ROMs/Switch/Your Game.nsp"
#
# Set that shortcut's controller layout to Steam Input ENABLED. That is the
# configuration this has been proven on: every pad arrives as an identical
# Valve virtual controller, and preflight tells them apart by reading the
# physical devices Steam hides and matching each one to its virtual twin.
# The Steam Controller only reaches the emulator this way at all.
#
# With Steam Input disabled the pads arrive as real hardware, which is the
# simpler path on paper, but it has not been tested end to end here and the
# Steam Controller drops out of the game entirely.
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
