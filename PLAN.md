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
- Shortcut target `~/preflight/preflight.sh`, ROM path quoted in Launch
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
preflight.sh     Steam shortcut target; logs to the state dir
preflight.py     the tool: model, config writer, screen, launcher
sdlui.py         ctypes bindings for system libSDL2 + libSDL2_ttf
theme.json       shipped default; the user's copy lives in CONFIG_DIR
games.json       shipped default; the user's copy lives in CONFIG_DIR
phase0.py        standalone diagnostics, kept for future debugging
run-report.sh    wrapper that saves phase0 output to STATE_DIR/reports
(no state/)      known_pads.json, backups/, launch.log all live
                 outside the install directory — see section 8
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
- Back up to `STATE_DIR/backups/`, write to a temp file, `os.replace()`.

## 5. Design decisions worth not undoing

**Do not auto-detect Nintendo vs Xbox button layout** (proposed and rejected
2026-09-07). SDL can report a controller's type — `SDL_GameControllerGetType`,
or `SDL_GetGamepadButtonLabel` in SDL3 — and it is tempting to use it to pick
each pad's default face mapping. Tested against real hardware, plain identity
gave WYSIWYG on all four pads *including* the Nintendo-labelled 8BitDo: in
X-input mode it does not merely claim to be an Xbox One S, it maps to match,
so the button printed A reports as SDL's A and the relabelling cancels out.
Detection that "corrected" it from its Nintendo lineage would break the one
pad that was already right. The ID says what a pad pretends to be, not what is
printed on it, and the manual `L`+`R` toggle remains the honest answer.


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

**One skeleton, many maps** (2026-09-16). `Layout` holds where the controls
go — the two rows, the four lanes, the sizes, and the offsets across the top
row measured out from the point between the sticks — and BOTH maps draw into
it. Only the glyphs differ. That is deliberate and it is the thing to protect:
a player who has read one of these screens should be able to read the next
one, and the next one is coming (an N64 map for Gopher64 was asked about the
day this landed).

Adding a map is therefore: an art set under `art/<name>/`, a
`_<name>_controls(g, held, axes, swap, holds, wys)` that draws into `Layout`,
and a row in `BACKEND_LAYOUT`. The top row is positioned by ROLE rather than
by name — `Layout.TRIGGER`, `SHOULDER`, `INNER` — so the analog triggers land
in the same two places whatever the pad calls them, which is how a GameCube's
L and R sit where a Switch's ZL and ZR do. Where a pad has fewer controls,
leave the slot empty rather than moving the others: a GameCube has one Start
where the Switch map has minus and plus, and it goes between them.

The one part a new map really does own is its cluster. `gc_cluster` fits the
GameCube's four buttons into the shared face lane from the PSD's own pixels;
an N64's would do the same with its own numbers.

**The top row hangs off the sticks, not the middle of the strip** (Switch
map, 2026-09-16). Those are not the same point: the face cluster's lane is
wider than the d-pad's, so the row of lanes sits left of the strip's centre,
and a top row placed at fixed fractions of the strip was visibly out of line
with everything below it. It is now mirrored about the midpoint between the
two stick lanes and scaled to whatever fits once it is off-centre, keeping
its own spacing. Both maps are also drawn higher in their bay, with the
controller's name further below: at the old spacing the lowest buttons very
nearly touched it.

**The map follows the emulator, not the pad in the player's hands**
(2026-09-15). Dolphin gets a GameCube map: one Start, Z on its own, two analog
shoulders, and a cluster built round a big A. It lights by what Dolphin will
bind — Z is the pad's right shoulder, L and R its triggers — so a press on
screen predicts the press in the game, which is the whole point of the screen.
A physical pad's own layout is not drawn and never was: see the face-button
note above.

Four things about how it is built, each of which was learned by getting it
wrong first:

* **Both maps are drawn from ONE pack** — Kenney's *Input Prompts*, CC0,
  which has a GameCube section as well as a Switch one. Zacksly's GameCube
  pack came first and was dropped for it on 2026-09-16: same artist means
  identical stroke weights and canvases, and it retired a pile of machinery
  that existed only to reconcile the two — blob-slicing out of a mock-up,
  letter re-centring on interior centroids, stroke normalisation, rotating X
  and standing its letter back up — along with a CC BY attribution. The
  lesson for the next map: take the whole set from one hand.
* **The layout file sets the cluster; the lanes set everything else.**
  `GC_FACES` is all a map owns now — an offset from A and a (width, height)
  per button, measured off `gc.psd`. Two things were learned the hard way
  there. A size is a BOX, not ink, so each glyph keeps the shape Kenney drew
  it at; and the width and height are kept SEPARATE, because that file
  stretches X by 11% and that stretch is what stands it upright — averaging
  the two into one number put the lean back and cost a round trip to work
  out why. `gc_cluster` then grows the cluster until it meets the lane plus
  its air, the gap up to the top row, or the bottom of the strip, whichever
  comes first.
* **A wysiwyg ring is the button's own outline, recoloured, on BOTH maps.**
  Not one of A, B, X, Y is a circle on a GameCube pad, so the annulus the
  Switch map used had nothing to be concentric with; painting the ink itself
  green or amber is exact. `Bay.face` draws three layers from one glyph,
  bottom up: the press fills the whole shape, the outline paints its edge in
  the wysiwyg colour, the label goes on last in white.

  **The press goes UNDERNEATH, and that is the whole trick.** Drawn on top it
  has to be eroded to sit inside the outline, and there is no good amount:
  eroded enough to clear the line it leaves a dark ring of background inside
  every pressed button, eroded less it covers half the line's width. Under
  it, nothing is eroded at all — the colour runs uniformly to a full-weight
  edge — which also deleted the blur, the gap constant and the ring-thickness
  measurement that existed only to serve them.

  Two traps in deriving those layers, both hit: measure a ring's thickness on
  the **ring alone**, since the thickest part of a glyph is its letter; and
  **close the label's hole first**, because Kenney's filled art already has
  one and it shows as a dark letter-shaped halo round the white one.

  The fill used to knock the label out of itself, for a negative. That reads
  well on a GameCube's big A and not at all on a Switch's small circles,
  where Kenney's letter is nearly as wide as the room inside the ring.
* **Two things that looked like art problems were not.** Faces that read as
  jagged were SDL2 point-sampling every scaled texture — nothing was setting
  `SDL_RENDER_SCALE_QUALITY`, which defaults to nearest, and the face buttons
  showed it worst because they are the one thing drawn LARGER than its
  canvas. And outlines of visibly different weights were arithmetic: a line
  scales with the box it is drawn in, so one 6px line came out 6.5px on A and
  3.5px on B. `layout_weights()` reads the ratio out of `Layout` itself and
  `match_weight` thins or thickens each glyph to match, on a 4x copy because
  whole-pixel erosion overshot. Both fixes are global: every glyph on both
  maps got sharper, not just the faces.
* **L and R fill; they do not switch — and only on this map.** They are
  analog on this pad and Dolphin binds them to the analog triggers, so the
  filled glyph is drawn over the outline and clipped as the trigger goes
  down. Clip against the INK, not the canvas: every glyph in the pack sits in
  its own margin, L's is a fifth of the canvas, and measuring from the box
  meant the first fifth of the travel filled empty space — the button looked
  like it was ignoring a slow press.

  **A trigger needs its own deadzone.** One 6000 was applied to every axis,
  which is a stick's rule: a stick jitters around its centre and the screen
  never settles without it, while a trigger rests at zero and stays there.
  Under 6000 the value was stored as nothing at all, so the first fifth of
  the travel — exactly what the gauge is for — drew nothing, and it read as
  the trigger being ignored until something else woke the screen. Triggers
  are on 400 now (`AXIS_DEADZONE`).

  Sharing the top row briefly gave the Switch map the same treatment, and
  that was wrong: the GameCube is the only Nintendo console that ever had
  analog shoulders, and a Pro Controller's ZL and ZR are switches wearing a
  trigger's shape. `Control.analog` is what says which, per entry, so a map
  draws its pad and not the one next door.

Two gestures had to move, and both for the same reason — **preflight's own
controls have to be reachable on the map being drawn**:

* **Quit.** A GameCube pad has no select button, so there is nothing to hold.
  Start carries both gestures instead: alone it starts, with Z it quits. Z
  being lit says which, and the ring's colour backs that up — white, not the
  obvious red, because P1 *is* red and the two rings came out identical on
  the one pad that does the starting. The physical Back button still quits,
  as a way out if a pad's shoulders are being remapped out from under us.
* **Swap ABXY.** Both analog triggers, on every map. It was the two shoulders
  once, which is wrong on a GameCube pad twice over: the legend draws L and R
  and those ARE the triggers there, so a player went to the wrong pair of
  controls; and both shoulders together are Z, so flipping the face mapping
  as a side effect of pressing Z was a trap. The triggers are ZL and ZR on a
  Switch pad, so one gesture covers both maps and the legend just letters it
  differently. Axes carry no press events, so it is judged from the values
  each frame, with separate on and off thresholds so a trigger resting near
  the line cannot rattle the mapping.

**A caution about this map and Dolphin.** Preflight reads SDL, which under
Steam Input means the virtual pad; Dolphin is bound to the physical device
through evdev, by kernel name. So a Steam Input layout that remaps a button
changes what the map shows without changing what the game receives.

Seen from both sides on 2026-09-16. On the **2026 Steam Controller** the
Wheel Wizard shortcut delivered `btn=11 (D-Up)` for L and for both back
paddles, while R arrived correctly as 10, so Z would not light and the old
two-shoulder swap could never fire. That pad has no evdev binding either —
Steam holds it at hidraw level and publishes only a virtual pad, so the
Dolphin backend has no kernel device to write. On **every other pad** L maps
to Z in the game exactly as written. The layout itself is not readable from
disk: the shortcut is `UseSteamControllerConfig 2`, `controller_configs/` is
empty, and `launcher.vdf` binds the bumpers correctly, so Steam is keeping
the applied per-game layout in the cloud. It can only be changed in Steam's
own UI.

The map is still the right diagnostic — it shows exactly what arrives, which
is how this was found at all — but the two are reading different layers, and
a report of "button X does the wrong thing" has to start with the press log.

## 6. Known limitations

- **The gap.** If a pad sleeps or wakes between the config write and Ryujinx's
  own startup, ids can shift. Much smaller now that indices come from the
  emulator's enumeration, but not zero.
- **`controller_type` is inherited** from the cloned template — everyone gets
  Pro Controller. Handheld and Joy-Con pair are not settable.
- **`input_config` is replaced wholesale.** A controller asleep during
  preflight gets no Ryujinx binding at all.
- **A pad shows `Steam pad xxxx` until someone touches it.** The hardware
  pairing needs a press — and a QUIET one: pairing is skipped while another
  pad is being pressed, since a node that fired 200ms ago belongs to whoever
  pressed it rather than to the next pad to ask. A Steam Controller never
  pairs at all: Steam holds it at hidraw level, so it has no kernel node, and
  left to itself it took an 8bitdo's (2026-09-21), wore its name and
  inherited its Nintendo button layout with it.
- **Steam Input's off switch is per-pad, not per-game.** Measured 2026-09-21:
  with Steam Input disabled for a shortcut, other pads arrive as themselves —
  their own Bluetooth GUIDs reach the config — while the Steam Controller
  still comes through as a Steam Virtual Gamepad. It has no unvirtualised
  mode to fall back to, so a family session is routinely a MIXED set, and
  anything here that assumes "Steam Input on" or "off" as a global state is
  wrong.
- Mario Kart asks for the Switch's `ShowControllerSupport` applet and Ryujinx
  stubs it (`ControllerApplet ReturnResult 1 1`). Not caused here, but it is
  why that title screen behaves oddly.

## 7. Supporting other emulators

Dolphin and Eden were planned as separate backends. The split is cleaner than
it looks: roughly
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
which one the shortcut points at — and since 2026-09-03 the shortcut says so
outright. `preflight.sh -- <command>` passes the launch command through
verbatim; `command_target()` pulls the flatpak app id (or binary name) out of
it, and `backend_for()` matches that against the `BACKENDS` table. Adding an
emulator therefore starts with one line in `BACKENDS` and ends with the
functions above.

Wheel Wizard is a launcher rather than an emulator. Its native Linux shortcut
is recognized as a Dolphin backend, so Preflight writes the normal Dolphin
Flatpak config before handing the original command to Wheel Wizard. Wheel
Wizard then performs its own mod preparation and starts Dolphin with those
bindings. The Steam virtual-pad SDL hint is also preserved through that child
launch.

**Gyro through DSU, as of 2026-09-19.** `DEFAULT_ENTRY`'s motion block asked
for the CemuHook backend with no address at all (`dsu_server_host: None`,
`dsu_server_port: 0`), so a profile Preflight wrote pointed CemuHook at
nothing and no gyro ever arrived. It now writes `127.0.0.1:26760`, the
standard DSU address, which is what SteamDeckGyroDSU publishes the Deck's own
IMU on — proven on the Deck with Breath of the Wild's shrines and bow aiming,
SteamOS gyro off. Ryujinx's own GamepadDriver backend cannot do this under
Steam: it reads the Steam virtual gamepad, which has no IMU.

`repair_motion()` does the same for a CLONED entry, because everything but
the face mapping is inherited and an empty host would otherwise survive for
ever. Only the empty case is filled; a host the user set is left alone.
SelfSteam writes the same two values when it creates a Ryubing shortcut, so
the two now agree instead of overwriting each other at every launch.

**DuckStation, as of 2026-09-21.** Read out of its own source
(`src/util/sdl_input_source.cpp`, `src/core/analog_controller.cpp`,
`src/core/controller.cpp`) plus a settings.ini it wrote. Plain INI, one
`[PadN]` section per player, `Type = AnalogController`, one line per control.

- A binding is `SDL-<player index>/<name>`, and the index is SDL's **player**
  index, not a device index — the same concept in SDL2 and SDL3, so unlike
  gopher64 there is a stable number to write. It is read through
  DuckStation's OWN SDL3, extracted from the AppImage by the cache that
  already exists for Ryubing, and pads are matched to it by GUID.
- Button names are SDL's Xbox-style ones (`A`, `LeftShoulder`, `DPadUp`)
  whatever pad is held; the Cross/Circle/Square/Triangle names in that file
  are for display only. Axes carry a direction: `+LeftX`, `-LeftY`,
  `+LeftTrigger`, or `Full` for the whole throw. Motors are
  `LargeMotor`/`SmallMotor`, and both are bound, so a DualShock rumbles.
- **Four players work, through the multitap.** `MultitapMode = Port1Only` is
  written as soon as three pads are claimed. The sections are then NOT
  consecutive: port 1's slots are pads 0, 2, 3, 4 in its own numbering
  (`Controller::PortDisplayOrder`), so four players land in Pad1, Pad3, Pad4
  and Pad5. Ports we do not fill are set to `Type = None`, or a stale
  binding leaves a phantom player.
- Face mapping is by POSITION, which is what the shapes are: Cross is the
  bottom button and so is SDL's A.

The check screen draws a PlayStation map from Kenney's own PS set, with the
"alternative" shoulders and triggers (the plain L2 carries a lip that reads as
a smudge at this size) and Select/Start keeping their captions, since the
shapes alone — a box and a wedge — say nothing.

**Shapes are positions, but SDL's letters are not.** Measured on an 8Bitdo
SF30 Pro in X-input mode, through Steam Input (2026-09-21): pressing the
BOTTOM button lit circle and pressing east lit cross. Steam feeds a
Nintendo-lettered pad's labelled A through as SDL's A, so the pad arrives
already swapped and nothing downstream can see it. The map and the bindings
therefore compensate from the pad's own hardware — `nintendo_layout()`, which
now knows 8BitDo's vendor (0x2dc8) as well as Nintendo's — automatically,
with nothing for the player to set.

**The swap GESTURE is disabled on this map.** Every other map
swaps because a LETTER can lie about position: the button marked A is in
different places on a Switch pad and an Xbox pad. A shape cannot lie — north
is triangle, south cross, east circle, west square, on every pad — so the
bindings are written by position with no mirrored variant, the badge is not
drawn, and the two triggers do nothing. That last part matters: swap state
follows a pad to the OTHER emulators, where it does act.

**gopher64, as of 2026-09-20.** Built from its own source (`src/ui/`) and a
config it wrote: `config.json` keeps named input profiles, each an array of 19
slots in `input_profile.rs`'s order, every slot a [keyboard, controller] pair.
Preflight writes one profile per player, binds it to that port and **enables
the port** — ports 2-4 ship disabled, so multiplayer could not work without
that. Only the controller half is written; the keyboard half is cloned from
gopher64's own default profile.

- `controller_assignment` is a **kernel device path**, and only gopher64's
  statically-linked SDL3 knows which path it will open for a pad — there is no
  library to borrow as there is for Dolphin and Cemu. So the assignment is
  made by gopher64 itself: `--list-controllers` to see them (which also
  creates config.json on a fresh install), `--assign-controller N --port P` to
  record one. It round-trips the whole file, keeping what preflight wrote,
  and preflight re-reads it afterwards to check.
- It prints SDL3's joystick name, which for a Steam virtual pad is the KERNEL
  name — "Microsoft X-Box 360 pad 0" where this tool says "Steam Virtual
  Gamepad". Measured on the machine; every name a pad answers to is tried.
- Its own default puts N64 **B on West** (X on an Xbox-labelled pad), which
  follows the N64's shape but breaks the one rule this tool has. B is written
  on B. Confirmed wrong-way-round on the machine first.
- The map is the N64 one: the C buttons are four buttons, not a stick, even
  though gopher64 binds them to the right stick — that is how they are played,
  not what the pad has. Art is the user's own icons (`selfsteam assets/n64
  kenney'd icons`), cut apart in stage-art.py; the layout comes from
  n64-3.psd, measured off the COMPOSITE's ink rather than its layer boxes,
  which carry margin and halved the C buttons. Sizes measured off art are ink,
  and preflight draws ink at 0.78 of its box (`N64_BOX`) — forgetting that
  drew every button a fifth small and opened gaps in the cross.
- This map alone gets a taller strip (`PAD_ASPECT_N64`) and a bigger top row
  (`N64_TOP`, measured against the GameCube's 69 px shoulders), because the
  group is wide and the pad has one stick to make room with.

**Cemu, as of 2026-09-19.** Built from Cemu 2.6's own source (`src/input/`)
rather than a saved profile: `InputManager::load/save` for the file,
`VPADController.h`/`ProController.h` for mapping ids, `Controller.h`'s
`Buttons2` for button codes, `VPADController::set_default_mapping` for which
SDL code each control takes. Notes worth keeping:

- One `controllerProfiles/controllerN.xml` per player. The `<uuid>` is
  `<n>_<SDL GUID>`, where n counts only pads sharing that GUID, and only
  devices SDL calls game controllers — not a global index.
- The GamePad and the Pro Controller number their controls differently (Pro
  has Home between Minus and the d-pad), so each has its own table.
- Its flatpak links the freedesktop runtime's SDL2. Pick the runtime Cemu's
  own `metadata` names: 26.08 sat beside Cemu's 25.08 on the machine, and
  "newest installed" chose the wrong one.
- P1 is the GamePad by default because many games will not boot without one,
  but Wind Waker HD puts its map and items on the GamePad's screen, which does
  not exist in Game Mode. `+`and `-` together switch P1 to a Pro Controller,
  stored per ROM in `cemu-p1-pro.json`. The gesture cancels both hold timers,
  since the same two buttons start and quit.
- A different controllerN.xml naming a pad we just assigned is moved to the
  backups, or yesterday's P2 drives a second player with today's P1.
- The map is the Switch one unchanged: the GamePad has the same control set.
  The bay corner carries a Kenney Wii U GamePad/Pro badge, with the ABXY swap
  badge under it.

**Three players confirmed on the TV 2026-09-21, with Steam Input both on and
off.** Off is the more interesting half: the pads then arrive as themselves,
and their own GUIDs went into the profiles (`0500...` Bluetooth ids for a
Stadia and an Xbox Series pad) matched exactly, while a Steam Controller in
the same run still came through as a virtual pad. So the per-GUID ordinal
holds for real hardware and a mixed set, not just for virtual pads.

Four players confirmed on the TV 2026-09-21, after two failures worth
recording:

- **Its enumeration order is not stable.** Two runs a minute apart listed the
  same pads in different orders, and since the listing and each assignment are
  separate processes, an index taken from one was wrong in the next: port 4
  silently got no device. Fixed by not using indices at all — preflight's own
  SDL reports the same evdev node for a Steam virtual pad that gopher64's SDL3
  opens (four pads at event20/22/24/26, matched by GUID), so the path is
  written straight into the config. The CLI route survives only for a pad
  whose path we cannot know, such as one SDL hands us as /dev/hidraw.
- **A pad it could not name refused the whole launch.** Under Steam Input
  every pad arrives here as "Steam Virtual Gamepad" while gopher64 lists the
  hardware ("Google Stadia Controller"), so matching failed on three of four.
  Names now include the paired hardware's own, and anything still unmatched
  disables its port instead of stopping the game — unless it is P1's.

**Rumble is out of scope, deliberately.** It is the Rumble Pak, chosen at
runtime by holding the hotkey and pressing B, and gopher64 hard-codes MemPak
at startup for every game but Chameleon Twist (`get_default_handler`). No
config field, so nothing preflight can write. Upstream does not take feature
requests (bug-report template only, blank issues disabled), so this is not
being pursued there either — and no hint is shown on the check screen, since
the map is for testing inputs, not for teaching another program's hotkeys.
Anyone who wants rumble presses Select+B per player, per session.

Untested so far: the AppImage and portable paths.

Guessing was the old way and it does not survive a second Switch emulator:
`find_app_id()` grepped `flatpak list` for "ryu", and no ROM path can say
whether a `.nsp` is meant for Ryujinx or Eden.

With no matching backend, the check still runs and the command is still
exec'd — the roster screen warns that no bindings will be written, and pad
identities are still remembered. That makes Preflight useful in front of an
emulator it knows nothing about.

**Non-flatpak Ryujinx builds, as of 2026-09-03.** Two of the three pieces
are done. `find_config()` no longer confuses installs: a flatpak target uses
its `~/.var/app` sandbox, a binary target uses a `portable/` folder beside it
or `~/.config/Ryujinx`, and neither falls through to the other. That crossover
was a silent failure — write the flatpak's config, launch the AppImage, and it
looks exactly like the tool having done nothing. `find_emulator_sdl(exe)` now
looks beside the binary, so **tar builds work**, and it deliberately does not
fall back to the flatpak's library: mixing one install's SDL with another's is
the mismatch the function exists to prevent.

**AppImage works too, as of 2026-09-03.** `appimage_sdl_dir()` asks the image
for its payload offset (`--appimage-offset`), runs `unsquashfs` at that offset
for `usr/lib/libSDL*`, and caches the result under
`STATE_DIR/sdl-cache/<basename>-<size>-<mtime>/`. No FUSE, no mounting, no
root; the cost is paid once per emulator update and a hit is instant. Measured
on a Canary AppImage: 2.8 MB cached, one `libSDL3.so`.

Two traps, both of which cost time here:

- `--appimage-extract` with a pattern **exits 0 and writes nothing**. Check for
  output files, never the exit code. `unsquashfs -o <offset>` is the reliable
  route for a squashfs payload.
- The dead-entry prune ran *before* the rename, and `<basename>-*` matches the
  temporary directory as well, so it deleted what it had just extracted and the
  whole thing failed silently. Prune after the rename, and skip the entry just
  written.

**Canary is NOT a free ride, corrected 2026-09-03 by downloading one.**
Ryubing Canary 1.3.351 bundles **libSDL3.so, version 3.5.0** — in both the
AppImage and the tar — while the flatpak stable build bundles SDL 2.30.0. The
config *location* is shared (`~/.config/Ryujinx`, unless portable mode), and
the `ryujinx` needle in `BACKENDS` matches its filename, so config writing is
fine. What is not fine is §3: `EMU_ENUM` speaks the SDL2 C API, and ids
computed with the wrong SDL look perfectly valid and match nothing.

`emulator_sdl_libs()` therefore collects both majors, and `EMU_ENUM3` is the
SDL3 flavour of the enumeration subprocess: `SDL_GetJoysticks` returns a
malloc'd array of instance ids instead of a count, so the array's own order
stands in for SDL2's device index, and the getters are renamed
(`SDL_GetJoystickGUIDForID`, `SDL_GetJoystickNameForID`, `SDL_GUIDToString`).
`emulator_gamepads()` tries SDL2 first and falls back to SDL3.

**Verified on the Steam Machine with a real pad, 2026-09-03.** The same
controller enumerated through Ryujinx's SDL 2.30, SteamOS's SDL 2.32 and
Canary's SDL 3.5.0 gives a byte-identical GUID:

```
030079f6de280000ff11000001000000
```

So SDL3 needs no id conversion of its own. The *name* does differ — 2.30 says
"Steam Virtual Gamepad", the newer two report the kernel name "Microsoft X-Box
360 pad 0" — but the CRC field inside the GUID stayed the same, and Ryujinx
zeroes it anyway.

THIRD load-bearing discovery, found while testing that: **SDL 2.32 and SDL3
hide Steam's virtual pads from any process Steam did not launch, and SDL 2.30
does not.** With Steam Input on, the virtual pads are the only pads there are,
so enumeration came back completely empty — silently, exit code 0. The unlock
is `SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD=1`, and it has to be set in
the subprocess **environment**: SDL3 reads it directly from the environment
before the hint system is consulted, so calling `SDL_SetHint` inside the script
works for SDL2 and is ignored by SDL3. Both are set now, belt and braces.

This never bit us in normal use because preflight runs under Steam and inherits
the environment that makes SDL trust it. It would have bitten the moment anyone
ran the tool outside Steam against a modern SDL.

**AppImage payload formats differ by emulator, which the extraction plan has
to survive.** Measured: Ryubing Canary's AppImage is classic squashfs (`hsqs`
at the offset from `--appimage-offset`, 31 entries, SDL under `usr/lib/`), so
`unsquashfs -o <offset> -d <dest> <image> 'usr/lib/libSDL*'` pulls the library
out with no FUSE involved. Eden's AppImage is **DwarFS**, where unsquashfs is
useless and `--appimage-mount` is the way in — that works and needs no root.
On both, `--appimage-extract` with a pattern exited 0 and produced no files,
which is a nasty way to fail: check for output, never for the exit code.

**What will differ, and what to expect:**

- **Dolphin: done for GameCube, 2026-09-05.** `write_dolphin_config()` writes
  `[GCPad1..4]` into `GCPadNew.ini` and sets `SIDevice<n> = 6` in `Dolphin.ini`
  — without that last part the mappings exist and the port stays empty, which
  looks exactly like nothing having been written. Read back from a real config
  rather than guessed, which turned up three things:

  * Dolphin identifies devices as `<backend>/<index>/<name>` with **no GUID**,
    so none of §3 applies — no CRC zeroing, no bus byte, no enumerating through
    the emulator's own SDL.
  * The name is SDL's **gamepad** name, not the joystick name: `Xbox One
    controller`, not `Microsoft X-Box 360 pad 0`. Under Steam Input every pad
    is therefore `Steam Virtual Gamepad` and **only the index separates them**.
    That index counts devices *sharing that name*, not all devices.
  * Backends can be mixed within one file, and their vocabularies differ
    entirely — `evdev/0/8Bitdo SF30 Pro` uses `EAST`/`Axis 7-` where
    `SDL/0/Steam Controller` uses `` `Button E` ``/`` `Pad N` ``. Preflight
    always writes SDL devices in SDL vocabulary rather than trying to speak
    both.

  Face buttons are positional (`Buttons/A = Button S`), so the A/B swap is
  simply S/E versus E/S.

  **Getting Dolphin to SEE the pads took three attempts; do not undo the
  third.** Its SDL is 2.32, which hides Steam's virtual pads from processes
  Steam did not launch, and the flatpak sandbox strips the environment that
  would say otherwise. Writing the hint into Dolphin's own `[SDL_Hints]`
  section does not work — measured, twice; it is applied too late. Matching
  each pad to its physical hardware by MAC does not work either, because
  `RealWatcher` cannot open `/dev/input` under Steam here, so no pad ever
  learns its MAC (`note: cannot read /dev/input directly` in launch.log).
  `prepare_command()` adds `--env=SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD=1`
  to the `flatpak run` line, which does get the devices in front of Dolphin —
  `/proc/<pid>/fd` confirms it opens event20/22/24 — but the game still had no
  input, because **SDL's name for a virtual pad is not the kernel's**. SDL
  calls all of them "Steam Virtual Gamepad"; the kernel calls them "Microsoft
  X-Box 360 pad 0/1/2". Dolphin's SDL backend uses one of the two and there is
  no way to tell which from outside.

  **So the backend writes `evdev/<n>/<kernel name>` instead**, which has none
  of these problems: kernel names are unique per pad, no hint is needed, and
  no visibility rule applies. The vocabulary is completely different from the
  SDL backend's — bare `SOUTH`/`EAST`, `Axis 7-` for the d-pad, `Full Axis 2+`
  for triggers, axis numbers being positions among the device's absolute axes
  rather than kernel codes — and it was copied in shape from a working
  hand-made entry, then checked against the device's own reported
  capabilities. Do not "simplify" this back to SDL device strings. Wii remotes are untouched: `WiimoteNew.ini` defaults
  to `XInput2/0/Virtual core pointer` and is a separate problem.
- **Eden: done, 2026-09-11.** `write_eden_config()` edits `[Controls]` in
  `~/.config/eden/qt-config.ini`. Read back from a real config, as always:

  * Eden **zeroes the 16-bit name-CRC too**, exactly as Ryujinx does — the
    same §3 discovery, in a second emulator. It stores the plain 32 hex
    digits, not .NET's dashed byte order, and `port:` is its SDL enumeration
    index, so identical pads have the same id-plus-different-port problem.
  * Its SDL is **statically linked into the binary** (2.33.0, read out with
    `strings`), so there is nothing to enumerate through. That turns out not
    to matter: 2.33 shares the newer bus-byte convention with SteamOS's 2.32,
    which was verified against the real config — system SDL gives
    `030079f6de28…`, zero the CRC and you get exactly the `03000000de28…`
    Eden had stored.
  * Bindings are **raw joystick numbers** (`button:9`, `axis:2,threshold:…`,
    `hat:0,direction:up`), which differ per pad. They come from
    `SDL_GameControllerGetBindForButton/Axis` rather than an assumed Xbox
    layout — verified to reproduce Eden's own numbers for every control.
  * Every key carries a `\default` twin that must be set to `false`, or the
    value is treated as untouched. `set_ini_keys()` is line-surgical for this
    reason: 1610 lines in, 1610 out, 44 changed, none outside `[Controls]`.
  * Players beyond the assigned ones get `connected=false`, or a phantom pad
    from a previous session turns up in the game.
  * **The GUID depends on which SDL driver claims the pad**, and so does the
    enumeration order that becomes `port:`. Measured on one Xbox pad:
    HIDAPI gives `05005f805e040000e002000000006800` at `/dev/hidraw7`, evdev
    gives `…e002000003090000` at `/dev/input/event23` — the version field
    differs. The orders differ wholesale too: HIDAPI put the virtual pad last,
    evdev put it first. This is why the first Steam-Input-off run failed with
    a config that looked perfectly correct.

    Guessing Eden's default would be another coin flip, so the driver is
    pinned instead: `eden_devices()` enumerates with `SDL_JOYSTICK_HIDAPI=0`
    and `prepare_command()` launches Eden with the same, so both sides agree
    by construction. Pads are matched between the two views by MAC, and a
    Steam virtual pad by its GUID, which carries a unique name-CRC.
  * **The flatpak (`dev.eden_emu.eden`) works too, verified 2026-09-15** on
    Rhythm Heaven, and needed one fix rather than a new backend. Its config
    lands at `~/.var/app/dev.eden_emu.eden/config/eden/qt-config.ini`, which
    `eden_config_target()` already resolved, and it ships **no SDL of its
    own**: unlike the AppImage's static SDL 2.33 it links the KDE runtime's
    **2.32**, the version that hides Steam's virtual pads — behind a sandbox
    that strips the environment. Exactly the Dolphin trap.

    `prepare_command()` had an `eden` branch that added `SDL_JOYSTICK_HIDAPI=0`
    and returned before the virtual-pad hint was reached, so under Steam Input
    the flatpak would have seen no pads at all. It is now a `BACKEND_ENV`
    table, so a backend's whole environment is one entry and no branch can
    return early past half of it. Its `filesystems=host:ro` and
    `devices=input` mean SD-card ROMs and controllers need no override.
  * **UNRESOLVED: Eden gets no input with Steam Input OFF.** It works with
    Steam Input on, which is the configuration in use, so this was parked
    2026-09-11 rather than solved. What was established, so a future attempt
    does not repeat it:

    - preflight runs, writes, and launches correctly in that mode. The log
      shows the right pads, the config holds evdev-form ids with matching
      ports, and the hint reaches Eden's environment.
    - Pinning `SDL_JOYSTICK_HIDAPI=0` on both sides did not fix it, so a
      HIDAPI-versus-evdev id mismatch is not the whole story (it *is* a real
      difference, measured, and worth keeping pinned regardless).
    - Eden logs nothing whatsoever from its SDL input driver, even with
      `log_filter="*:Info Input:Trace"` — only its GameCube-adapter, UDP and
      Joycon drivers appear. So Eden offers no window into what its SDL sees,
      which is why three hypotheses in a row were guesses.

    **The next step is a measurement, not a theory:** with Steam Input off,
    bind one button on P1 inside Eden's own controller UI and read the guid
    and port it writes. That is the only way to see Eden's own view, and it
    costs a minute. Everything else here was inference.
  * **A fresh install has no qt-config.ini at all**, and refusing to write
    would strand exactly the person this is for. `set_ini_keys()` creates
    the file and the `[Controls]` section when absent; Qt fills in every
    other setting. Verified by moving a real config aside: 438 lines
    written from nothing, bindings correct. Dolphin's config directory is
    created the same way.

  Its AppImage is **DwarFS**, not squashfs, and `--appimage-extract` with a
  pattern writes nothing while exiting 0 — but no extraction is needed, given
  the static link. `--appimage-mount` is the way in if it ever is.

Neither should be assumed to behave like Ryujinx. **Budget a diagnostic pass
for each** — the two discoveries in §3 were both invisible until a real launch
failed, and there is no reason to think the next emulator lacks its own. Run
`phase0.py` against it, write one config by hand from the emulator's own UI,
then read back exactly what it wrote and compare. That is what found both.

## 8. Contract with SelfSteam

SelfSteam (the user's shortcut-creation project, formerly Gridge) **embeds**
this rather than installing it as a neighbour: Preflight ships inside SelfSteam
and updates whenever SelfSteam updates. Nobody is expected to install or update
it on its own.

Preflight keeps its own repository — github.com/ScarletPachydermDev/Preflight —
which is upstream. The copy inside SelfSteam is downstream and must never be
edited in place, or the two diverge silently and the version number starts
lying. Whether that copy arrives by submodule, subtree or a sync script is
SelfSteam's choice; what matters is that it is a copy of a tagged upstream
state and not a fork.

Installed to `~/.local/share/selfsteam/preflight/`, with Steam shortcuts
pointed at it.

The boundary is the API and should not shift casually:

- **`preflight.sh` is the entry point.** Either `-- <command to run>`, which
  is the form SelfSteam should generate, or a bare ROM path for the Ryujinx
  shorthand. Everything else inside this project can be rewritten freely.
- **`VERSION`** is a plain version string, so SelfSteam can report which build
  it shipped — it is written into every `launch.log` run header. The patch
  number is bumped automatically by `hooks/pre-commit`, so the number in a log
  identifies the exact commit; enable it in a clone with
  `git config core.hooksPath hooks`. Staging `VERSION` by hand suppresses the
  bump, which is how a deliberate minor or major version is set.
- **State and config live outside the install directory**, so SelfSteam never
  has to ship or preserve them. `known_pads.json` accumulates controller MAC
  addresses and must stay out of the repository either way.

### Updating must not wipe the user's data

Three things in the install folder belong to the user, not to the release:

| `state/known_pads.json` | every controller's identity and its A/B swap preference |
|:---|:---|
| **`theme.json`** | **colours and rumble pacing** |
| **`games.json`** | **per-game expectations** |

`known_pads.json` is the one that hurts. It is what lets a pad keep its real
name and its own mirror setting across sessions; wiping it means every
controller is a stranger again and every player's swap silently reverts to the
default. That is the tool failing at exactly the job it exists to do.

This is why none of them live in the install directory any more (done
2026-09-03). Paths resolve as:

```
STATE_DIR   $PREFLIGHT_STATE_DIR  | $XDG_STATE_HOME/preflight  | ~/.local/state/preflight
CONFIG_DIR  $PREFLIGHT_CONFIG_DIR | $XDG_CONFIG_HOME/preflight | ~/.config/preflight
```

`theme.json` and `games.json` ship as defaults in the install folder and are
read through `user_file()`, which prefers the user's copy in `CONFIG_DIR`.
`adopt_user_files()` runs at startup: it moves any legacy `state/` contents out
of the install directory and copies the shipped defaults into `CONFIG_DIR` if
they are not there yet. It never overwrites an existing file, so it is safe to
run on every launch, and it leaves the old `launch.log` behind because that is
history rather than settings.

**So SelfSteam's update is simply: delete the install directory and unpack the
new one.** There is nothing of the user's inside it to lose. `preflight.sh` and
`run-report.sh` compute the same paths in shell and must be kept in step with
`preflight.py` if the rules ever change.

SelfSteam's side of the job: detect emulators (`flatpak list`, plus browse for
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
- **Steam Input layouts can be applied from outside Steam**, proven on the
  machine 2026-09-20: `steam://controllerconfig/<shortcut appid>/<published
  workshop id>` reaches a running Steam, prompts once, and applies the layout
  to a shortcut that had never used it. Only PUBLISHED layouts work — a
  private one has no id and no local file. The layout itself lands in
  `userdata/<id>/ugc/referenced/<hash>/<id>_controller_config.vdf`, but the
  SELECTION is cloud-only: nothing on disk says which layout a shortcut uses,
  so it can be set and never read back. A second prompt (Controller Conflict)
  appears when the account already has a cloud layout for that shortcut. This
  is how SelfSteam could ship a layout with an emulator — e.g. the N64 C-button
  shift, which gopher64 itself cannot express. SteamInputDB
  (github.com/Alia5/steaminputdb.com) does the same thing through Steam's CEF
  debug port instead; that needs Steam started with remote debugging, which is
  not on by default, so the link is the cheaper route.
- Cemu and gopher64 have only been run as flatpaks. Their AppImage and
  portable paths are written and will stay unexercised: SelfSteam offers
  neither as an install type, so there is nothing to test them from.
- Eden with Steam Input OFF is still unexplained (§6).

## 10. Gotchas seen more than once

- **After changing shortcut settings, Steam can believe the app is still
  running** and the Play button silently does nothing. Restart Steam
  (Steam → Power → Restart Steam). `state/launch.log` proves whose fault it is:
  no new run header means the tool never started.
- **Never launch this on `DISPLAY=:1` over SSH while Game Mode is live** — two
  fullscreen SDL windows appearing from nowhere will wedge gamescope.
- When changing the UI, run **`./shot.py out.png`** and **look at it**. That
  caught a badly distorted gamepad, an off-screen glyph bar, and overlapping
  controls — all of which compiled and ran perfectly. It renders headlessly
  (`SDL_VIDEODRIVER=offscreen` plus `SDL_RenderReadPixels`, PNG written by hand
  from zlib), so nothing has to go on the TV — launching the real thing on a
  live Game Mode session has frozen it before. `--pads N` uses synthetic pads,
  so it works with nothing awake; `--alert` and `--swap` stage the states that
  are otherwise awkward to reach. Needs libSDL2_ttf, so it runs on the deck and
  not necessarily on a dev box. It used to be a throwaway script rewritten from
  scratch each time it was needed, which is why it is committed now.
