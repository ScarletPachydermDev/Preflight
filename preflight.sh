#!/usr/bin/env bash
# Steam shortcut target for preflight.
#
#   Target:         /home/deck/preflight/preflight.sh
#   Launch Options: -- flatpak run io.github.ryubing.Ryujinx -f "/roms/Game.nsp"
#
# Everything after "--" is the command to run once the check passes, and it is
# what tells preflight which emulator it is configuring. A bare ROM path with
# no "--" still works and assumes Ryujinx.
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

# State lives outside the install directory, so SelfSteam can replace this
# folder wholesale without destroying known_pads.json. Must agree with the
# paths preflight.py computes.
STATE_DIR="${PREFLIGHT_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/preflight}"
mkdir -p "$STATE_DIR"
LOG="$STATE_DIR/launch.log"

# Version goes in the header rather than coming from preflight.py, so a run
# that dies before Python gets going still records which build it was.
VERSION="$(cat "$DIR/VERSION" 2>/dev/null || true)"

# Log what we were asked to do. Note "$@" is never shifted — it has to reach
# preflight.py exactly as Steam handed it over.
if [ "${1:-}" = "--" ]; then
    DESC="command: ${*:2}"
else
    ROM="${1:-}"
    case "$ROM" in --*) ROM="" ;; esac   # a flag is not a ROM
    DESC="rom: ${ROM:-<none, opening Ryujinx game list>}"
fi

{
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="
    echo "preflight ${VERSION:-unknown}"
    echo "$DESC"
} >>"$LOG"

# Not exec'd: keeping this shell alive for the pipeline means Steam continues
# to show the game as running for as long as Ryujinx is up.
python3 "$DIR/preflight.py" "$@" 2>&1 | tee -a "$LOG"
