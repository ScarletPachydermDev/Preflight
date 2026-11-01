# Supported emulators

All of them are also on [SelfSteam](https://github.com/ScarletPachydermDev/SelfSteam)'s
Emulators tab, where Preflight is one toggle away.

| Emulator | Package | Notes |
|:---|:---|:---|
| **Ryubing (Ryujinx)** | flatpak, AppImage | Pro Controllers |
| **Eden** | flatpak, AppImage | Pro Controllers. **Steam Input on only** |
| **Dolphin** | flatpak | GameCube pads only; Wii remotes untouched. |
| **Wheel Wizard** | flatpak | writes Dolphin's GameCube pads |
| **Cemu** | flatpak | P1 GamePad or Pro Controller (**+**/**−**, remembered per game) |
| **gopher64** | flatpak | No rumble |
| **Rosalie's Mupen GUI** | flatpak | Rumble Pak on |
| **M64Py** | flatpak | Rumble Pak on |
| **DuckStation** | AppImage | **Steam Input on only** |
| **PCSX2** | flatpak |  |
| **Play!** | flatpak | only one Steam Controller per game |
| **RPCS3** | flatpak |  |
| **shadPS4** | flatpak | **no claim P1**: seats pads in SDL's order |
| **xemu** | flatpak |  |
| **Xenia Canary** | AppImage | **no claim P1**: seats pads in Steam's order |
| **Flycast** | flatpak | |
| **BigPEmu** | flatpak | |
| **Vita3K** | AppImage |  |
| **PPSSPP** | flatpak |  |
| **melonDS** | flatpak | L3 swaps screens |
| **Azahar** | flatpak |  L3 swaps screens, R3 cycles layouts |

## Which map each one gets

The check screen draws the emulated console's controls, so a press on screen
predicts the press in the game.

| Map | Emulators |
|---|---|
| Switch | Ryubing, Eden, Cemu |
| GameCube | Dolphin, Wheel Wizard |
| N64 | gopher64, Rosalie's Mupen GUI, M64Py |
| PlayStation | DuckStation, PCSX2, Play!, RPCS3, shadPS4 |
| Xbox | xemu, Xenia Canary, BigPEmu |
| Dreamcast | Flycast |
| PSP | PPSSPP |
| Vita | Vita3K |
| DS | melonDS |
| 3DS | Azahar |

## Notes

- PlayStation emulators bind by position: Cross is the bottom button (south)
  on any pad.
- Handhelds remember the last controller played, except a Steam Controller
  (it has no hardware id).
- DuckStation takes four players through the multitap, set up as soon as
  three pads are claimed.
- Eden may pop up its own multiplayer controller window during gameplay,
  when a game asks to pick controllers (seen in Rhythm Paradise Groove and
  Big Brain Academy). Ryubing skips that screen.
- All verified with Steam Input **on**. Steam Input off is untested on most.
- Preflight recognises the emulator from the launch command after `--`:
  its Flatpak id, or the AppImage's name.
