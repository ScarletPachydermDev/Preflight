# How it works

## Files

| File | Job |
|---|---|
| `preflight.sh` | Steam shortcut target; logs each run to the state folder |
| `preflight.py` | The tool: pad model, identity, config writers, check screen, launcher |
| `sdlui.py` | ctypes bindings for the system's SDL2 (and SDL2_ttf or FreeType), a PNG decoder |
| `theme.json`, `games.json` | Shipped defaults; your copies live in `~/.config/preflight/` |
| `phase0.py`, `run-report.sh` | Standalone diagnostics |
| `art/` | Button art per map (Kenney's *Input Prompts*, CC0) |

## A launch, step by step

1. Steam runs `preflight.sh -- <emulator command>`.
2. Preflight works out the emulator from the command (`BACKENDS`) and opens
   the check screen with that emulator's map.
3. Players test, claim P1, swap A/B if they want; P1 holds Start.
4. Preflight writes the emulator's controller config, then starts the
   original command unchanged.

## Telling controllers apart

With Steam Input on, every pad reaches SDL as an identical Valve virtual
pad, and the names SDL reports are put on the wrong pads. Preflight
identifies pads in three tiers, best first:

1. **Real MAC**, read from the physical device. Steam only hides those
   devices from SDL; the kernel still has them. Preflight pairs each virtual
   pad to its hardware by seeing the same press on both (within 250 ms, and
   never when two devices fire at once), through evdev and hidraw.
2. **Name-CRC** (`crc:f679`) of a Steam virtual pad: identifies the slot,
   not the device.
3. SDL's reported name: never trusted, not even for display.

A real MAC is what keeps per-pad settings (like a swapped A/B) with the
controller across sessions.

## Matching the emulator's own ids

Emulators build controller ids from SDL's GUID, and their SDL can differ
from the system's: SteamOS's SDL 2.32 and Ryubing's bundled 2.30 disagree
on one byte for the same Bluetooth pad, and Ryubing also zeroes the
name-CRC. So ids are computed by loading the **emulator's own SDL**, in a
subprocess (two SDL builds cannot share one process). AppImages are
unpacked with `unsquashfs` and cached for this.

## Writing configs

- Start from an existing entry in the emulator's config as a template, or a
  complete default where there is none; re-stamp the device id and player.
- Only the face mapping is set outright (as printed, or mirrored); sticks,
  d-pad, shoulders, triggers, deadzones, motion and rumble ride along from
  the template, which keeps what you set in the emulator.
- Every binding is checked; any that is blank or unbound is filled from the
  default, since a hole there is invisible on the check screen.
- Refuses a duplicate id or a missing Player 1.
- Backs up to the state folder, writes a temp file, then replaces the
  config in one step.

## With SelfSteam

SelfSteam installs Preflight under `~/.local/share/selfsteam/preflight`,
updates it, and prefixes `preflight.sh -- ` to a shortcut's command when its
toggle is on. An update replaces only the install folder; user data lives
outside it.
