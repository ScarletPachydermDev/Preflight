# Install

## Requirements

- **Linux on x86_64 (x64)**: a Steam Deck, a Steam Machine, or an x86_64
  Linux PC with Steam. Preflight does not run on Windows or macOS.
- One of the [supported emulators](SUPPORTED.md).
- SDL2, and SDL2_ttf or FreeType for text. SteamOS has them; SteamOS 3.9
  dropped SDL2_ttf, and Preflight then uses FreeType.
- `unsquashfs`, for emulators installed as AppImages. SteamOS has it.

No Python packages: SteamOS has no `pip` and a read-only `/usr`, so Preflight
uses the system's own libraries through ctypes.

## With SelfSteam

Turn on **Enable preflight** on SelfSteam's Emulators tab (or the Apps tab
for Wheel Wizard). SelfSteam installs Preflight, keeps it up to date, and
puts it in front of the shortcut's launch command.

## By hand

```
git clone https://github.com/ScarletPachydermDev/Preflight ~/preflight
```

Then, in Steam, set the game's shortcut to run Preflight with the emulator
command after `--`:

```
~/preflight/preflight.sh -- flatpak run io.github.ryubing.Ryujinx -f "/run/media/deck/mSD/ROMs/Switch/game.nsp"
```

Everything after `--` is the command Preflight starts once the check passes,
and it is also how Preflight knows which emulator it is configuring.

## Steam Input

Set the shortcut's controller layout to **Steam Input on**. That is the
setup everything is tested with. Eden and DuckStation need it on; the Steam
Controller only reaches an emulator through it. See
[Decisions](DECISIONS.md#steam-input-stays-on).

## Updating

With SelfSteam, it updates Preflight when SelfSteam itself updates. By hand,
`git pull` in `~/preflight`. Updates replace only the install folder: your
settings and history live elsewhere (see [Using](USING.md#where-things-live)).

## Uninstalling

Remove the install folder, and the shortcut's `preflight.sh -- ` prefix.
Your files stay in `~/.config/preflight/` and `~/.local/state/preflight/`
until you delete them.
