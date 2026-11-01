# Troubleshooting

`~/.local/state/preflight/launch.log` records each run. If it ends at `window ready`, Preflight
started fine and the problem is elsewhere. If there is no entry at all, Steam
never launched it — Steam sometimes believes a shortcut is still running and
the Play button silently does nothing, which a Steam restart clears.

`tools/stage-art.py` rebuilds the button glyphs from Kenney's pack: the
GameCube set, and two derived layers for the Switch set from the art already
committed. It is safe to run twice.

`shot.py` renders the check screen to a PNG instead of the TV, which is how
a layout change gets checked: `./shot.py out.png --pads 4`. It can draw either
map (`--layout gamecube`) and fake any input, so a pressed button or a stick at
full deflection can be looked at without a controller in reach:
`./shot.py out.png --pads 1 --press 1:a,start --axes 1:0=-32768`.

The log also records **what the pad actually sent** — every press as SDL
delivered it, and the first time each axis moves:

```
press: P1 btn=11 (D-Up)
axis:  P1 axis=4 (Trigger L) reached 27312
```

That is the diagnostic for "this button does the wrong thing". Steam Input
sits between the pad and Preflight, and a per-game layout can bind a shoulder
to something else entirely — seen for real: a Steam Controller whose L1 and
both back paddles all arrived as `btn=11 (D-Up)`, so Z never lit. Preflight is
showing exactly what it receives; the binding is Steam's, and the only place
to change it is that shortcut's controller layout (Steam's per-game controller
settings — switching it to a plain **Gamepad** template binds the shoulders as
shoulders). Note Dolphin is bound to the physical device through evdev, so a
remapped pad can look wrong here and still play correctly.

`tools/steam-shortcut.py` lists Steam's non-Steam shortcuts and can put a
launcher in front of one, reading and rewriting `shortcuts.vdf` in place with a
byte-identical round-trip check and a timestamped backup. Useful when checking
what a shortcut actually runs; SelfSteam is what creates them normally.

`phase0.py` is a standalone diagnostic that prints every controller the system
can see, how the emulator will identify it, and whether Steam is intercepting.
Run it if something looks wrong and you want the full picture.
