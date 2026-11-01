# Development

## Adding an emulator

About four fifths of `preflight.py` (screen, input model, identity, rumble,
theming, launching) knows nothing about any one emulator. A new one needs:

1. A row in `BACKENDS`: the backend name and the fragments of its Flatpak id
   or AppImage name that identify it in the launch command.
2. Where it and its config live, and how to enumerate pads through its own
   SDL.
3. Turning an SDL GUID into whatever its config calls a device.
4. Reading and writing its config (template, entries, face mapping).
5. A row in `BACKEND_LAYOUT` for its map, or a new map: an art set under
   `art/<name>/`, a controls function that draws into the shared `Layout`,
   positioned by role.

Read the emulator's own source for its config format and ids; do not guess.
[PLAN.md](../PLAN.md) section 7 has worked examples (DuckStation, Dolphin,
gyro through DSU).

## Tools

- `shot.py out.png --pads 4` renders the check screen to a PNG, no TV or
  controllers needed. It can pick a map (`--layout gamecube`) and fake input:
  `--press 1:a,start --axes 1:0=-32768`.
- `phase0.py` prints every controller the system sees, how the emulator will
  identify it, and whether Steam is intercepting.
- `tools/stage-art.py` rebuilds the button art from Kenney's pack.
- `tools/steam-shortcut.py` lists Steam's non-Steam shortcuts and can put a
  launcher in front of one, with a byte-identical round-trip check and a
  backup.

## Commits

`git config core.hooksPath hooks` enables the pre-commit hook, which bumps
`VERSION`'s patch number on every commit, so `launch.log` always says which
build produced a run.

## More

[PLAN.md](../PLAN.md) holds the full design history: the two discoveries
everything rests on, known limitations, remaining work, and gotchas seen
more than once.
