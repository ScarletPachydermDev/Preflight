# Decisions

Why things are the way they are, learned by getting them wrong first. The
full history is in [PLAN.md](../PLAN.md).

### Steam Input stays on

It was off for a long time, because it makes every pad an identical virtual
device. Two things changed that: ids now come from the emulator's own SDL,
and pads are matched by name-CRC. With Steam Input off, the Steam Controller
reaches Preflight but never the emulator: it exists only through Steam
Input.

### No guessing which pad is which

An earlier shortcut paired the one unidentified pad with the one unclaimed
device before anyone pressed anything. It crossed two controllers on one run
and the reverse on the next, while the log looked fine. Pads are only paired
on evidence: the same press seen on both sides.

### No automatic Nintendo or Xbox layout

SDL can say what a pad claims to be, but an 8BitDo in X-input mode already
maps to match its printed letters, so "correcting" it would break the one pad
that was right. Players swap with both triggers instead.

### One screen, and taps do nothing

Every action is a hold or a combo, so players can mash buttons to test them.
Claiming P1 is one-shot, so a second player cannot keep taking it back.

### The map follows the emulator

The screen draws the emulated console's controls, not the pad in your hands,
and lights a button by name, not position: press the button marked A and
the A lights, wherever it sits. No controller body is drawn: any outline is
wrong for most real pads.

### One skeleton, many maps

Every map draws into the same layout, positioned by role (trigger, shoulder,
inner buttons), so a player who has read one screen can read the next. All
art comes from one pack, so stroke weights match.

### No Python packages

SteamOS has no `pip` and a read-only `/usr`. Everything uses the system's
own libraries through ctypes, including a hand-written PNG decoder, and
FreeType where SDL2_ttf is missing.

### Your data lives outside the install folder

So an update can replace the folder wholesale.

### A swap is stored only against a real MAC

Never against a slot id, so an unidentified pad gets the safe default rather
than whatever the last controller in that slot used.
