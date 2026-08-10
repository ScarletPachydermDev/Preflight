# Preflight

A pre-launch controller check for Ryubing (Ryujinx) on SteamOS.

It sits between the Steam shortcut and the emulator: everyone confirms which
player they are and that their buttons land where the labels say, then Player 1
holds `+` and the game starts. It writes Ryujinx's `Config.json` and hands off.

**Status: working.** First successful real launch 2026-08-09 — four controllers
drove Mario Kart 8 correctly, with zero `No matching controllers` warnings in
Ryujinx's log.

Target: Valve Steam Machine, SteamOS Game Mode, Ryubing installed as the
flatpak `io.github.ryubing.Ryujinx`, one Steam shortcut per game.

---

## 1. The problem

Ryujinx identifies a controller in `Config.json` as `<sdl_index>-<guid>`. The
index is SDL enumeration order — i.e. the order controllers happened to
connect. Change that and player slots move. Observed before this existed:

- Player 1 and Player 3 swapping between sessions.
- Duplicate `id` values written for distinct pads (Ryubing/Issues#10).
- If nothing lands on Player 1, **no** controller works at all.
- No way to tell, before starting, whether a pad is actually connected or
  whether its buttons are mapped the way the labels suggest.

Ryujinx's own controller applet reports that *a* controller exists. It does not
tell you which physical pad it is, whether Bluetooth actually came up, or
whether A does what A says.

## 2. Known-good setup

Do not drift from this without testing:

- **Steam Input ENABLED** on the shortcut. Counter-intuitive, and the opposite
  of where this project started. See §5.
- Shortcut target `~/preflight/launch.sh`, ROM path quoted in Launch
  Options, one shortcut per game.
- `flatpak override --user --filesystem=/run/media/deck/mSD/ROMs/Switch:ro
  io.github.ryubing.Ryujinx` — the flatpak otherwise has no access to the SD
  card and only reaches ROMs through a document-portal handle.
- The 8BitDo must stay in **X-input** mode. Switch mode makes the kernel bind
  `hid-nintendo`, whose Nintendo-specific handshake times out on third-party
  hardware (`ret=-110`) and drops the pad every ~26 seconds.

## 3. The two load-bearing discoveries

Neither is documented anywhere; both took a diagnostic phase to establish, and
nothing works if either is undone.

**Ryujinx zeroes the name-CRC.** SDL puts a 16-bit CRC of the device name in
bytes 2–3 of the GUID — it is what separates two devices sharing a vendor and
product. Ryujinx discards it when building its config id. Any code generating
ids must reproduce that zeroing or nothing will ever match.

**SDL changed the bus byte between 2.30 and 2.32.** Ryujinx bundles SDL 2.30.0;
SteamOS ships 2.32.x. The same Bluetooth pad is `00000005-18d1-…` to the system
SDL and `00000003-18d1-…` to Ryujinx's. Ids computed with the wrong SDL are
wrong by one byte, look perfect on our side, and produce
`Hid Remap: No matching controllers found` in the emulator.

So ids are computed by loading **Ryujinx's own `libSDL2.so`** (under
`/var/lib/flatpak/app/…/files/bin/`) in a **subprocess** — two SDL builds share
a SONAME, so loading both in one process silently yields whichever landed
first. The UI keeps the system SDL; only id computation borrows the emulator's.
Pads are matched between the two enumerations by vendor/product, preferring an
exact name-CRC hit.

## 4. Architecture

```
launch.sh        Steam shortcut target; logs to state/launch.log
preflight.py     the tool: model, config writer, screen, launcher
sdlui.py         ctypes bindings for system libSDL2 + libSDL2_ttf
theme.json       colours and rumble pacing
games.json       optional per-game notes
phase0.py        standalone diagnostics, kept for future debugging
run-report.sh    wrapper that saves phase0 output to reports/
state/           known_pads.json, backups/, launch.log
```

Zero dependencies. SteamOS has no pip and a read-only `/usr`, so everything
talks to libraries already present. `sdlui` includes a hand-written PNG decoder
and font-metric handling for the same reason.

### Identity

Three tiers, best first:

1. **Real MAC**, from the physical device. Belongs to one piece of hardware
   forever.
2. **Name-CRC** (`crc:f679`), for a Steam virtual pad. Identifies the *slot*,
   not the device — Steam reassigns them between sessions.
3. SDL's reported name, for display only.

Under Steam Input every pad is a virtual pad with no MAC, so tier 1 would be
unavailable — except that Steam only *hides* the physical devices from SDL via
`SDL_GAMECONTROLLER_IGNORE_DEVICES`. The kernel devices remain. `RealWatcher`
reads those evdev nodes and pairs a virtual pad to its hardware by correlating
the same button press on both (250 ms window; refuses to pair when two devices
fire at once). That is what turns `Steam pad f679` into
`Xbox Series X|S Controller` and restores durable per-pad settings.

### Config writing

- Clone an existing `input_config` entry as a template — never author button
  maps from scratch — then re-stamp `id` and `player_index`. A fresh Ryujinx
  install has nothing to clone, so `DEFAULT_ENTRY` supplies a complete
  standard SDL binding; its field names and value spellings are copied
  verbatim from a real Ryujinx-written entry, since the schema is
  undocumented.
- The whole entry is written, not just the face buttons — d-pad, sticks,
  shoulders, triggers, deadzones, motion, rumble. Only the face mapping is set
  explicitly; everything else rides along from the template, which is what
  preserves bindings the user customised inside Ryujinx.
- Write the face mapping outright: identity (`button_a: "A"`, WYSIWYG) or
  mirrored. Setting it beats swapping, which depends on the template's state.
- Refuse on a duplicate `id` or a missing Player 1.
- Check every binding the entry is about to carry and repair any that are
  absent, blank or `Unbound` from `DEFAULT_ENTRY`. Only the face mapping is
  authored; sticks, d-pad, shoulders and triggers ride along from the
  template, so a hole there is invisible — the tool reads inputs through SDL
  and shows them working while the emulator gets nothing. The roster warns up
  front when the config being cloned has gaps.
- Back up to `state/backups/`, write to a temp file, `os.replace()`.

## 5. Design decisions worth not undoing

**Steam Input stays ON.** It was disabled for a long stretch, because it turns
every pad into an identical Valve virtual device. Two things changed: ids now
come from the emulator's own SDL, and pads are matched by name-CRC, which
vendor/product alone cannot do. With Steam Input *off* the Steam Controller
reaches this tool but never the emulator — it exists only through Steam Input.

**One screen, and taps do nothing.** Every action is a hold or a combo, so
players can mash buttons to test them without setting anything off.

| | |
|---|---|
| `+` hold | P1 launches — dot ring shows progress |
| `−` hold | anyone exits — ring appears on that player's own bay |
| `L3`+`R3` | claim Player 1; once per session, then greys out |
| `L`+`R` | that player mirrors their own A/B and X/Y |

Exit is deliberately available to everyone. Gating it to P1 meant that if P1's
pad slept, nobody could close the tool.

**Claiming P1 is one-shot** so a second player cannot keep taking it back.

**Face buttons light by name, not position.** Drawn in the Switch arrangement;
press the button marked A and the circle marked A lights, wherever it sits.
Location accuracy is traded away deliberately.

**No controller body is drawn.** Any outline is wrong for most real pads. Only
the inputs are drawn. (Steam ships usable line art under
`steamui/images/controller/` which can be read in place, never redistributed —
tried and rejected on looks.)

**Swap is only stored against a real MAC**, never a `crc:` key, so an
unidentified pad gets the safe default rather than inheriting a setting that
belonged to whichever controller Steam parked in that slot last time.

**Slots pack contiguously by wake order.** A pad that sleeps and returns
rejoins at the end rather than reclaiming its old slot — whoever took over
while it was away keeps their place.

## 6. Known limitations

- **The gap.** If a pad sleeps or wakes between the config write and Ryujinx's
  own startup, ids can shift. Much smaller now that indices come from the
  emulator's enumeration, but not zero.
- **`controller_type` is inherited** from the cloned template — everyone gets
  Pro Controller. Handheld and Joy-Con pair are not settable.
- **`input_config` is replaced wholesale.** A controller asleep during
  preflight gets no Ryujinx binding at all.
- **A pad shows `Steam pad xxxx` until someone touches it.** The hardware
  pairing needs a press.
- Mario Kart asks for the Switch's `ShowControllerSupport` applet and Ryujinx
  stubs it (`ControllerApplet ReturnResult 1 1`). Not caused here, but it is
  why that title screen behaves oddly.

## 7. Supporting other emulators

Dolphin and Eden are planned. The split is cleaner than it looks: roughly
1200 of ~1500 lines — the screen, input model, identity work, `RealWatcher`,
rumble, theming, launching — know nothing about Ryujinx. The emulator-specific
surface is these, and nothing else:

| | |
|---|---|
| `DEFAULT_APP_ID`, `find_app_id`, `find_config` | where the emulator and its config live |
| `find_emulator_sdl`, `emulator_gamepads`, `EMU_ENUM` | enumerating through the emulator's own SDL |
| `ryujinx_guid`, `emulator_id_for` | turning an SDL GUID into whatever the config calls a device |
| `DEFAULT_ENTRY`, `pick_template`, `build_entries`, `write_config` | reading and writing the config |
| `FACE_IDENTITY`, `FACE_MIRRORED`, `apply_face_mapping` | the face-button mapping |
| `launch` | how the game is started |

The right shape is a small backend per emulator behind that list, chosen by
which one the shortcut points at.

**AppImage support is planned, with one thing to solve.** §3 loads the
emulator's own `libSDL2.so` off disk, which works because a flatpak unpacks its
files; an AppImage keeps them inside a squashfs image. Worth trying: extract or
mount it once and cache the library, or read its SDL version and apply the
matching rules without loading it. If neither turns out practical, flatpak-only
is a fine place to land.

**What will differ, and what to expect:**

- **Dolphin** keeps controller setup in INI files, not JSON, with its own
  device-naming scheme. Four-player GameCube games make the "which pad is P1"
  problem more common than on Switch, so it is arguably the better fit.
- **Eden** is a Yuzu continuation and inherits Yuzu's INI config, where each
  binding is an engine/guid/port string rather than a single device id.

Neither should be assumed to behave like Ryujinx. **Budget a diagnostic pass
for each** — the two discoveries in §3 were both invisible until a real launch
failed, and there is no reason to think the next emulator lacks its own. Run
`phase0.py` against it, write one config by hand from the emulator's own UI,
then read back exactly what it wrote and compare. That is what found both.

## 8. Contract with Gridge

Gridge (the user's shortcut-creation project) installs this to
`~/.local/share/gridge/preflight/` and points Steam shortcuts at it. This is a
separate repository, so that boundary is the API and should not shift casually:

- **`launch.sh` is the entry point.** One argument, the ROM path. Everything
  else inside this project can be rewritten freely.
- **`VERSION`** is a plain version string, so Gridge can tell what it installed
  and offer an update.
- **`state/` is runtime-only** — Gridge must never ship it, and it is
  gitignored, since `known_pads.json` accumulates controller MAC addresses.

Gridge's side of the job: detect emulators (`flatpak list`, plus browse for
AppImages), read each emulator's own configured game folders so the user never
enters a ROM path, list the games with a per-game toggle for the controller
check, then create shortcuts with SteamGridDB artwork, enable Steam Input on
the flagged ones, apply the flatpak ROM permission, and restart Steam. It keeps
those games listed afterwards so artwork and the toggle can be changed later.

## 9. Remaining work

- PlayStation pads would need their own layout if body art ever returns — both
  sticks sit along the bottom, so Xbox coordinates do not transfer.
- Optional: a sit-out control for a pad that is awake but not playing.
- Possible upstream fix: have Ryubing key on SDL serial/MAC rather than
  enumeration index, which would make most of this unnecessary.

## 10. Gotchas seen more than once

- **After changing shortcut settings, Steam can believe the app is still
  running** and the Play button silently does nothing. Restart Steam
  (Steam → Power → Restart Steam). `state/launch.log` proves whose fault it is:
  no new run header means the tool never started.
- **Never launch this on `DISPLAY=:1` over SSH while Game Mode is live** — two
  fullscreen SDL windows appearing from nowhere will wedge gamescope.
- When changing the UI, render it headlessly (`SDL_VIDEODRIVER=offscreen` plus
  `SDL_RenderReadPixels`, write a PNG) and **look at it**. That caught a badly
  distorted gamepad, an off-screen glyph bar, and overlapping controls — all of
  which compiled and ran perfectly.
