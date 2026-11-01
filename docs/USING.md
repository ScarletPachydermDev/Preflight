# Using Preflight

## The check screen

When the game's shortcut starts, Preflight shows one bay per player, P1 to
P4, before the emulator opens. Each bay draws the emulated console's
controls, and lights every button, both sticks, the d-pad and both triggers
live as you press them. Press freely: a tap never does anything.

The map follows the emulator, not the pad in your hands: a GameCube layout
for Dolphin, a PlayStation one for the PlayStation emulators, a Switch one
for Ryubing, and so on. A press on screen shows what the game will get.

## Controls

Every action is a hold or a combo, so mashing buttons to test them sets
nothing off.

| Input | Does |
|---|---|
| Hold **Start** (+, Menu) | P1 starts the game; a ring shows the progress |
| Hold **Select** (−, View) | P1 quits without starting |
| **L3 + R3** | Claim Player 1, once per session |
| **Both triggers** | That player swaps A/B and X/Y |

The legend at the bottom of the screen draws these with the emulated pad's
own buttons, so they change with the map:

- **GameCube, N64:** no Select, so quit is **Z + Start**. N64 has no swap.
- **Dreamcast:** quit is **L + Start**; no swap.
- **PlayStation:** no swap: the shapes are positions, and the pad's own
  vendor settles which letter sits where.
- **Handhelds (DS, 3DS, PSP, Vita):** L3 + R3 claims the handheld.

## Players

- Players fill P1 to P4 in the order their pads wake. A pad that sleeps and
  comes back joins at the end; whoever took over keeps their place.
- Claiming P1 is one-shot, so a second player cannot keep taking it back.
- A pad asleep during the check gets no binding at all: wake every pad
  first.

## Per-emulator notes

- **Cemu:** P1 is a Wii U GamePad or a Pro Controller; **+** and **−**
  together switch, remembered per game. *Why:* many Wii U games will not
  boot without a GamePad, so it is the default, but some (Wind Waker HD) put
  the map and items on the GamePad's own screen, which does not exist in
  Game Mode.
- **Dolphin, Wheel Wizard:** GameCube pads only; Wii remotes are left alone.
  *Why:* Wii remotes live in a separate Dolphin config (`WiimoteNew.ini`)
  with a pointer-based setup of their own. Support for them is planned.
- **melonDS:** L3 swaps screens. **Azahar:** L3 swaps screens, R3 cycles
  layouts. *Why:* DS and 3DS games use two screens, and Game Mode gives no
  easy way to change their layout, so Preflight binds the emulators' own
  hotkeys to the stick clicks.
- **shadPS4, Xenia Canary:** no P1 claim; they seat pads in their own order.
  *Why:* neither has a setting for which pad is which player. shadPS4 seats
  pads in SDL's order and Xenia at Steam's slot, so the screen shows that
  order instead of offering a claim they would ignore.
- **Play!:** only one Steam Controller per game. *Why:* Play! reads
  controllers below Steam Input and tells them apart by hardware id, which
  Preflight finds for normal pads. A Steam Controller only exists as Steam's
  virtual pad, and every virtual pad looks the same to Play!, so a second one
  would drive the first player too.
- **PlayStation emulators** bind by position: Cross is the bottom button on
  any pad. *Why:* PlayStation buttons are shapes, not letters, so where a
  button sits is what counts.
- **Handhelds** remember the last controller played, except a Steam
  Controller. *Why:* a handheld has one player, remembered by the pad's
  hardware id; a Steam Controller has none Preflight can see, only Steam's
  virtual one.

## Where things live

Nothing you own sits next to the code, so an update can replace the install
folder without losing anything.

| Where | What |
|---|---|
| `~/.config/preflight/` | `theme.json` (colours, rumble pacing), `games.json` — yours to edit |
| `~/.local/state/preflight/` | `known_pads.json`, `launch.log`, config backups, reports |
| the install folder | code and shipped defaults only |

`PREFLIGHT_CONFIG_DIR` and `PREFLIGHT_STATE_DIR` move them; otherwise the
usual `XDG_*` variables are honoured.
