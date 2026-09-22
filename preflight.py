#!/usr/bin/env python3
"""
preflight — controller gate for Ryubing (Ryujinx) on SteamOS.

Confirms which physical controller is which player, verifies the face-button
labelling, writes Ryujinx's Config.json, then launches the game.

    preflight.py -- flatpak run <app-id> -f "<rom>"   # check, then run that
    preflight.py "/path/to/Game.nsp"     # same, assuming Ryujinx
    preflight.py                         # check, then open the Ryujinx list
    preflight.py --dry-run "<rom>"       # everything except writing/launching
    preflight.py --version               # print the version and exit

Everything after a bare "--" is the command to exec once the check passes.
Preflight reads the emulator out of that command and picks its config backend
from it; with no backend for what it finds, it still runs the check and still
launches, it just writes no bindings.

Zero dependencies: ctypes against the system libSDL2 and libSDL2_ttf.

Two behaviours here are empirical, established by phase0 against real
hardware, not guesses:

  * Ryujinx builds its config id as "<sdl_index>-<guid>" where the guid is
    SDL's, converted through .NET's Guid(byte[]) byte order, with the 16-bit
    name-CRC field ZEROED. Reproducing that zeroing is essential; without it
    nothing we write will ever match.
  * Because Ryujinx discards that CRC, Steam's virtual pads all collapse to
    one identical id. We keep the CRC ourselves so we can still tell them
    apart even when Ryujinx cannot.
"""

import collections
import copy
import ctypes
import select
import struct
import json
import os
import shutil
import subprocess
import sys
import time

import sdlui
from sdlui import (UI, BTN_A, BTN_B, BTN_X, BTN_Y, BTN_START, BTN_BACK,
                   BTN_LSHOULDER, BTN_RSHOULDER, BTN_LSTICK, BTN_RSTICK,
                   BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT,
                   BUTTON_NAMES, SWITCH_EQUIVALENT, SDLK_ESCAPE)

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(HERE, "VERSION")
ART_DIR = os.path.join(HERE, "art")


def art(name):
    """Path to a button glyph. Kenney's outline set, CC0, drawn in pure white
    so SDL's colour modulation can tint it to any player colour — the reason
    this set was chosen over the shaded ones."""
    return os.path.join(ART_DIR, name + ".png")


# The ring around each face button: is the label telling the truth?
RING_OK = (74, 232, 122)
RING_BAD = (232, 162, 60)

# A pad whose buttons carry NINTENDO semantics: the one SDL calls A is the
# one the player knows as B. Keyed on what the pad REPORTS ITSELF AS, not on
# what is printed on the plastic — because that is what decides the semantics.
# An 8BitDo SF30 Pro has Nintendo lettering, but in X-input mode it presents
# itself as an Xbox pad and its buttons behave that way, so the default
# mapping is already truthful; flipped into Switch mode the same pad reports
# as a Pro Controller and the mirrored mapping becomes the truthful one.
# Getting this from the silkscreen was wrong in exactly that case.
NINTENDO_LAYOUT_HINTS = (
    "nintendo", "switch pro", "pro controller", "joy-con", "joycon",
    "famicom", "super nintendo", "8bitdo",
)
# Nintendo, and 8BitDo — whose pads wear Nintendo lettering whatever mode
# they are in. Measured on an 8Bitdo SF30 Pro (2dc8:6101) in X-input mode:
# Steam still fed its LABELLED A through as SDL's A, so the pad arrives
# swapped and only the hardware behind it says so.
NINTENDO_VENDORS = (0x057E, 0x2DC8)
NINTENDO_VENDOR = 0x057E


def steam_relabelled(pad):
    """True when this pad's SDL letters are LABELS rather than positions.

    Not a property of the hardware but of what is between it and us. Measured
    both ways on an 8Bitdo SF30 Pro (2026-09-21):

      Steam Input ON  — the pad arrives as a Steam virtual pad and Steam
                        feeds its labelled A through as SDL's A, which sits
                        east. Pressing south gave B. Compensation needed.
      Steam Input OFF — the pad arrives as itself and SDL maps it by
                        position. Pressing south gave A. Compensating then
                        swaps it the WRONG way, which is what put cross on
                        the east button.

    So the question is never "is this a Nintendo pad" but "is Steam
    relabelling it": Nintendo lettering behind a Steam virtual pad.
    """
    return (nintendo_layout(pad)
            and (pad.vendor, pad.product) == STEAM_VIRTUAL)


def nintendo_layout(pad):
    """True when this pad's A and B are the other way round from SDL's.

    Reads the physical device when preflight has paired one — under Steam
    Input the virtual pad's own vendor is always Valve's and says nothing.
    """
    real = getattr(pad, "real", None) or {}
    vendor = real.get("vendor") if real else pad.vendor
    if vendor in NINTENDO_VENDORS:
        return True
    name = (real.get("name") if real else None) or pad.name or ""
    return any(h in name.lower() for h in NINTENDO_LAYOUT_HINTS)


def default_swap(pad):
    """The swap setting that makes this pad truthful with nobody touching it.

    Every controller should be WYSIWYG out of the box, whichever layout it
    has; L+R is there for someone who would rather have position accuracy.
    """
    return nintendo_layout(pad)


def pad_wysiwyg(pad):
    """True when the button printed A really acts as A on this pad.

    Identity mapping is truthful on an Xbox-layout pad; the mirrored one is
    truthful on a Nintendo-layout pad. So the two agree exactly when the
    swap setting matches the layout.
    """
    return bool(pad.swap_faces) == nintendo_layout(pad)

# Nothing the user owns lives beside the code. SelfSteam embeds this project
# and replaces the whole directory when it updates, so the install folder has
# to be disposable: state and config live under the XDG paths instead, and
# HERE holds only code plus the shipped defaults.


def _xdg(env, fallback):
    base = os.environ.get(env) or os.path.expanduser(fallback)
    return os.path.join(base, "preflight")


STATE_DIR = os.environ.get("PREFLIGHT_STATE_DIR") or _xdg(
    "XDG_STATE_HOME", "~/.local/state")
CONFIG_DIR = os.environ.get("PREFLIGHT_CONFIG_DIR") or _xdg(
    "XDG_CONFIG_HOME", "~/.config")

KNOWN_PADS = os.path.join(STATE_DIR, "known_pads.json")
BACKUP_DIR = os.path.join(STATE_DIR, "backups")

LEGACY_STATE_DIR = os.path.join(HERE, "state")


def user_file(name):
    """A user-editable file: their copy under CONFIG_DIR if it exists,
    otherwise the default we ship."""
    mine = os.path.join(CONFIG_DIR, name)
    return mine if os.path.exists(mine) else os.path.join(HERE, name)


def adopt_user_files():
    """First run after the move: lift state out of the install directory, and
    take a copy of the shipped defaults the user is allowed to edit.

    Copying rather than editing in place is the whole point — an update can
    then delete the install folder outright without destroying anything.
    Existing files are never overwritten.
    """
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        os.makedirs(CONFIG_DIR, exist_ok=True)
    except OSError:
        return

    # launch.log is deliberately left behind: it is history, not settings, and
    # the shell has already written this run's header to the new location.
    for name in ("known_pads.json", "backups"):
        src = os.path.join(LEGACY_STATE_DIR, name)
        dst = os.path.join(STATE_DIR, name)
        if not os.path.exists(src) or os.path.exists(dst):
            continue
        try:
            shutil.move(src, dst)
            print(f"moved {name} to {STATE_DIR}", flush=True)
        except OSError as exc:
            print(f"could not move {name}: {exc}", file=sys.stderr)

    for name in ("theme.json", "games.json"):
        src = os.path.join(HERE, name)
        dst = os.path.join(CONFIG_DIR, name)
        if not os.path.exists(src) or os.path.exists(dst):
            continue
        try:
            shutil.copy2(src, dst)
        except OSError as exc:
            print(f"could not copy {name}: {exc}", file=sys.stderr)


def read_version():
    """Our version, from the VERSION file sitting beside this script."""
    try:
        with open(VERSION_FILE) as fh:
            return fh.read().strip() or "unknown"
    except OSError:
        return "unknown"


VERSION = read_version()

DEFAULT_APP_ID = "io.github.ryubing.Ryujinx"
MAX_PLAYERS = 4

BG = (18, 19, 24)
CARD = (32, 34, 42)
FG = (232, 233, 238)
DIM = (140, 143, 155)
ACCENT = (120, 200, 140)
WARN = (232, 180, 90)
BAD = (226, 106, 106)

# One colour per player slot. Chosen to stay distinguishable on a dark
# background and to differ in brightness as well as hue, so they still read
# apart for red/green colour blindness.
PLAYER_COLORS = [
    (232, 93, 93),     # P1 red
    (86, 156, 232),    # P2 blue
    (232, 186, 82),    # P3 amber
    (108, 199, 130),   # P4 green
]




def blend(base, tint, amount):
    return tuple(int(base[i] + (tint[i] - base[i]) * amount) for i in range(3))


def _hex_to_rgb(value):
    s = str(value).lstrip("#")
    if len(s) != 6:
        raise ValueError(value)
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def apply_theme():
    """Overlay theme.json onto the defaults. Bad entries are skipped, not fatal
    — a typo in a colour should never stop you launching a game."""
    theme = load_json(user_file("theme.json"), {})
    if not theme:
        return
    global BG, CARD, FG, DIM, ACCENT, WARN, BAD, PLAYER_COLORS
    simple = {"background": "BG", "card": "CARD", "text": "FG", "dim": "DIM",
              "accent": "ACCENT", "warning": "WARN", "error": "BAD"}
    for key, name in simple.items():
        if key in theme:
            try:
                globals()[name] = _hex_to_rgb(theme[key])
            except (ValueError, TypeError):
                print(f"theme.json: ignoring bad colour for {key!r}",
                      file=sys.stderr)
    rumble = theme.get("rumble")
    if isinstance(rumble, dict):
        global RUMBLE_ON_MS, RUMBLE_GAP_MS, RUMBLE_STRENGTH
        for key, name, lo, hi in (("on_ms", "RUMBLE_ON_MS", 50, 3000),
                                  ("gap_ms", "RUMBLE_GAP_MS", 0, 10000),
                                  ("strength", "RUMBLE_STRENGTH", 0, 0xFFFF)):
            if key in rumble:
                try:
                    globals()[name] = max(lo, min(hi, int(rumble[key])))
                except (ValueError, TypeError):
                    print(f"theme.json: ignoring bad rumble.{key}",
                          file=sys.stderr)

    if isinstance(theme.get("players"), list):
        colors = []
        for entry in theme["players"]:
            try:
                colors.append(_hex_to_rgb(entry))
            except (ValueError, TypeError):
                print(f"theme.json: ignoring bad player colour {entry!r}",
                      file=sys.stderr)
        if colors:
            PLAYER_COLORS = colors


def player_color(slot):
    return PLAYER_COLORS[(slot - 1) % len(PLAYER_COLORS)]

POWER = {-1: "?", 0: "empty", 1: "low", 2: "med", 3: "full", 4: "wired"}


# ---------------------------------------------------------------- identity

def ryujinx_guid(sdl_guid_hex):
    """SDL GUID hex -> the dashed guid Ryujinx writes, with name-CRC zeroed."""
    b = bytearray(bytes.fromhex(sdl_guid_hex))
    b[2:4] = b"\x00\x00"                      # Ryujinx drops the name CRC
    d1 = int.from_bytes(b[0:4], "little")
    d2 = int.from_bytes(b[4:6], "little")
    d3 = int.from_bytes(b[6:8], "little")
    return f"{d1:08x}-{d2:04x}-{d3:04x}-{b[8]:02x}{b[9]:02x}-{bytes(b[10:16]).hex()}"


def guid_name_crc(sdl_guid_hex):
    return int.from_bytes(bytes.fromhex(sdl_guid_hex)[2:4], "little")


def guid_vendor_product(sdl_guid_hex):
    b = bytes.fromhex(sdl_guid_hex)
    return (int.from_bytes(b[4:6], "little"), int.from_bytes(b[8:10], "little"))


# SDL reports whatever layout a pad advertises, which is often a lie: Steam
# publishes its controllers as fake Xbox 360 pads, and 8BitDo spoofs Microsoft
# or Nintendo depending on its mode. These tables turn the advertised identity
# back into something a human recognises.
DEVICE_NAMES = {
    (0x28DE, 0x11FF): "Steam Controller",
    (0x045E, 0x0B13): "Xbox Series X|S Controller",
    (0x045E, 0x02E0): "Xbox One S Controller",
    (0x045E, 0x028E): "Xbox 360 Controller",
    (0x18D1, 0x9400): "Stadia Controller",
    (0x057E, 0x2009): "Switch Pro Controller",
    (0x054C, 0x0CE6): "DualSense",
    (0x054C, 0x09CC): "DualShock 4",
}

# Names here must match MAC_OUI spelling — the mismatch check below compares
# the two, so calling 0x045e "Xbox" would make genuine Microsoft pads look
# like they were spoofing.
VENDOR_NAMES = {0x2DC8: "8BitDo", 0x045E: "Microsoft", 0x18D1: "Google",
                0x057E: "Nintendo", 0x28DE: "Valve", 0x054C: "Sony"}

# First three bytes of a MAC identify the actual manufacturer, regardless of
# what USB identity the pad is currently pretending to have.
MAC_OUI = {"e4:17:d8": "8BitDo", "98:7a:14": "Microsoft", "9c:aa:1b": "Microsoft",
           "00:1b:dc": "Nintendo", "98:b6:e9": "Nintendo", "cc:9e:00": "Sony"}

# What a spoofed vendor tells us about the mode the pad is running in.
SPOOF_MODE = {0x045E: "X-input", 0x057E: "Switch mode", 0x054C: "PS mode"}


STEAM_VIRTUAL = (0x28DE, 0x11FF)



def label_pads(pads):
    """Resolve display names with the whole set in view.

    Under Steam Input every pad is a Steam virtual pad sharing one
    vendor/product, so the vendor tables cannot name them. Steam does put a
    real device's name on each virtual pad — but it puts them on the WRONG
    ONES. Measured with four pads: the 8bitdo arrived as "Steam Controller",
    the Steam Controller as "8BitDo SN30 Pro". Every name present, every one
    misplaced, and consistently so rather than at random.

    So a virtual pad that has not been matched to real hardware is not given
    a name at all. It gets a neutral tag, which is merely uninformative,
    rather than a confident label that is wrong — which sent three rounds of
    debugging after the wrong controller.

    The CRC in that tag is the slot, not the controller: it is SDL's checksum
    of "Microsoft X-Box 360 pad N", so it distinguishes two unnamed pads
    within a session and means nothing between sessions.
    """
    for p in pads:
        if p.nickname:
            p.display = p.nickname
            continue
        if p.real:
            # Identified through its physical twin — name it properly.
            p.display = friendly_name(_Shim(name=p.real["name"],
                                            mac=p.real["mac"],
                                            vendor=p.real["vendor"],
                                            product=p.real["product"]))
        elif (p.vendor, p.product) == STEAM_VIRTUAL:
            p.display = f"Controller {p.name_crc:04x}"
        else:
            p.display = friendly_name(p)


def friendly_name(pad):
    """A label that says what the pad actually is.

    Priority: a hand-set nickname in known_pads.json, then real-manufacturer
    detection via MAC, then the vendor/product table, then whatever SDL said.
    """
    if pad.nickname:
        return pad.nickname

    advertised = DEVICE_NAMES.get((pad.vendor, pad.product))
    oui = (pad.mac or "").lower().replace("-", ":")[:8]
    maker = MAC_OUI.get(oui)
    claimed = VENDOR_NAMES.get(pad.vendor)

    # Hardware maker disagrees with the advertised vendor: the pad is spoofing
    # a standard layout. Name the real maker and say which mode it's in.
    if maker and claimed and maker.split()[0].lower() != claimed.lower():
        return f"{maker} — {SPOOF_MODE.get(pad.vendor, 'compat mode')}"
    if advertised:
        return advertised
    if claimed:
        return f"{claimed} Gamepad"
    return pad.name


def sysfs_read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def sysfs_battery(mac):
    """Some drivers publish a controller battery under its MAC. Most don't —
    plain xpad and the Microsoft HID driver report nothing at all — so this is
    a bonus when available rather than something to rely on."""
    if not mac:
        return None
    key = mac.lower().replace("-", ":")
    try:
        entries = os.listdir("/sys/class/power_supply")
    except OSError:
        return None
    for entry in entries:
        if key in entry.lower().replace("-", ":"):
            try:
                with open(f"/sys/class/power_supply/{entry}/capacity") as fh:
                    return f"{fh.read().strip()}%"
            except OSError:
                pass
    return None


def sysfs_uniq(devpath):
    if not devpath or not devpath.startswith("/dev/input/event"):
        return None
    node = os.path.basename(devpath)
    try:
        with open(f"/sys/class/input/{node}/device/uniq") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


class Pad:
    """One controller as SDL currently sees it."""

    def __init__(self, sdl, index):
        self.sdl = sdl
        self.index = index
        name = sdl.SDL_JoystickNameForIndex(index)
        self.name = name.decode(errors="replace") if name else f"Pad {index}"

        buf = ctypes.create_string_buffer(33)
        sdl.SDL_JoystickGetGUIDString(sdl.SDL_JoystickGetDeviceGUID(index),
                                      buf, 33)
        self.sdl_guid = buf.value.decode()
        self.guid = ryujinx_guid(self.sdl_guid)
        self.name_crc = guid_name_crc(self.sdl_guid)
        self.vendor, self.product = guid_vendor_product(self.sdl_guid)

        gc = getattr(sdl, "SDL_GameControllerNameForIndex", lambda _i: None)(index)
        if not isinstance(gc, bytes):
            gc = None
        # Dolphin identifies devices by SDL's *gamepad* name, not the joystick
        # name — "Xbox One controller" rather than "Microsoft X-Box 360 pad 0".
        self.gc_name = gc.decode(errors="replace") if gc else self.name

        self.handle = sdl.SDL_GameControllerOpen(index)
        self.instance_id = -1
        self.battery = "?"
        self.serial = None
        if self.handle:
            js = sdl.SDL_GameControllerGetJoystick(self.handle)
            self.instance_id = sdl.SDL_JoystickInstanceID(js)
            self.battery = POWER.get(sdl.SDL_JoystickCurrentPowerLevel(js), "?")
            if hasattr(sdl, "SDL_GameControllerGetSerial"):
                s = sdl.SDL_GameControllerGetSerial(self.handle)
                self.serial = s.decode(errors="replace") if s else None

        self.player_index = -1
        if hasattr(sdl, "SDL_JoystickGetDevicePlayerIndex"):
            self.player_index = sdl.SDL_JoystickGetDevicePlayerIndex(index)

        devpath = None
        if hasattr(sdl, "SDL_JoystickPathForIndex"):
            p = sdl.SDL_JoystickPathForIndex(index)
            devpath = p.decode(errors="replace") if p else None
        self.devpath = devpath
        self.mac = self.serial or sysfs_uniq(devpath)
        if self.battery == "?":
            self.battery = sysfs_battery(self.mac)   # None when truly unknown

        self.slot = None          # 1..4 once assigned
        self.nickname = None
        self.can_rumble = None    # None until we've actually tried
        self.held = set()         # SDL button ids currently down
        self.axes = {}            # axis id -> raw -32768..32767
        self.display = None       # filled in by label_pads()
        self.real = None          # the physical device behind a virtual pad
        # A/B and X/Y always move together — no real controller mirrors one
        # pair without the other — so this is a single setting.
        self.swap_faces = False
        self.swap_explicit = False    # True once a player has asked for it

    @property
    def store_key(self):
        """Where this pad's settings are remembered.

        Prefer the physical device's MAC once we have identified it: Steam's
        virtual pads are reassigned between sessions, so a CRC-keyed record
        can end up attached to the wrong controller.
        """
        if self.real and self.real.get("mac"):
            return self.real["mac"]
        return self.key

    @property
    def key(self):
        """Stable identity across sessions.

        A MAC when the pad exposes one. Steam's virtual pads don't, but they
        do carry a distinct name-CRC that phase0 confirmed is stable across
        reboots and launch contexts, so that is the fallback.
        """
        if self.mac:
            return self.mac.lower().replace("-", ":")
        return f"crc:{self.name_crc:04x}"

    @property
    def label(self):
        return self.display or friendly_name(self)

    def attached(self):
        """False once the pad is really gone — a slept Bluetooth pad can sit in
        SDL's list looking alive, and rumble keeps returning success on it."""
        if not self.handle:
            return False
        return bool(self.sdl.SDL_GameControllerGetAttached(self.handle))

    @property
    def ryujinx_id(self):
        return f"{self.index}-{self.guid}"

    def rumble(self, strength, duration_ms):
        """Buzz the pad. Returns False if this controller can't rumble."""
        if not self.handle or not hasattr(self.sdl, "SDL_GameControllerRumble"):
            return False
        ok = self.sdl.SDL_GameControllerRumble(
            self.handle, strength, strength, duration_ms) == 0
        if duration_ms:
            self.can_rumble = ok
        return ok

    def close(self):
        if self.handle:
            self.rumble(0, 0)      # never leave a pad buzzing behind us
            self.sdl.SDL_GameControllerClose(self.handle)
            self.handle = None


class _Shim:
    """Just enough of a Pad for friendly_name() to work on a raw device."""
    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.nickname = None


def scan_real_gamepads():
    """The physical controllers, including ones Steam Input hides from SDL.

    Steam does not remove a controller it takes over — it only sets
    SDL_GAMECONTROLLER_IGNORE_DEVICES so the *game's* SDL skips it. The kernel
    device is still there with its real name and MAC, which is the only
    durable identity available once Steam is in the way.
    """
    import glob
    out = []
    for base in sorted(glob.glob("/sys/class/input/event*"),
                       key=lambda q: int(os.path.basename(q)[5:])):
        node = os.path.basename(base)
        dev = f"{base}/device"
        caps = sysfs_read(f"{dev}/capabilities/key")
        if not caps:
            continue
        words = caps.split()[::-1]              # sysfs prints MSB group first
        idx, off = 0x130 // 64, 0x130 % 64      # BTN_SOUTH marks a gamepad
        try:
            if idx >= len(words) or not (int(words[idx], 16) >> off & 1):
                continue
        except ValueError:
            continue
        if os.path.realpath(base).startswith("/sys/devices/virtual/input"):
            continue                             # a uinput pad, not hardware
        try:
            ven = int(sysfs_read(f"{dev}/id/vendor") or "0", 16)
            prod = int(sysfs_read(f"{dev}/id/product") or "0", 16)
        except ValueError:
            ven = prod = 0
        if (ven, prod) == STEAM_VIRTUAL:
            continue
        out.append({"path": f"/dev/input/{node}",
                    "name": sysfs_read(f"{dev}/name") or node,
                    "mac": (sysfs_read(f"{dev}/uniq") or "").lower() or None,
                    "vendor": ven, "product": prod})
    return out


def dev_ident(info):
    """What makes two watched nodes the same controller.

    A pad is now watched twice — its evdev node and its hidraw node — so a
    path no longer identifies it. Claiming one must claim both, or the same
    controller is handed to two pads.
    """
    return (info.get("vendor"), info.get("product"), info.get("mac"))


def scan_hid_gamepads():
    """The physical controllers as HID devices, read through hidraw.

    The evdev route does not work under Steam Input. Steam takes a pad over
    at the HID level, and its kernel event node then exists but never fires:
    measured on this hardware with every button on two pads being pressed
    and not one event arriving. Pairing by evdev therefore never succeeded
    once, and what looked like success was pair_by_elimination guessing.

    hidraw still carries the reports, because it does not hand a device to
    one reader exclusively — Steam reads it and so can we. A press shows up
    as a CHANGE in the report: an idle pad streams the same bytes over and
    over, so arrival alone means nothing.
    """
    import glob
    out = []
    for base in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        node = os.path.basename(base)
        dev = f"{base}/device"
        uevent = sysfs_read(f"{dev}/uevent")
        fields = dict(l.split("=", 1) for l in uevent.splitlines() if "=" in l)
        # HID_ID is bus:vendor:product, all hex and zero-padded.
        parts = (fields.get("HID_ID") or "").split(":")
        if len(parts) != 3:
            continue
        try:
            ven, prod = int(parts[1], 16), int(parts[2], 16)
        except ValueError:
            continue
        # Only pads. The same BTN_SOUTH test as the evdev scan, applied to
        # whichever input devices this HID device brought with it — a
        # keyboard's hidraw node would otherwise be watched for presses.
        mac, is_pad = None, False
        for inp in sorted(glob.glob(f"{dev}/input/input*")):
            caps = sysfs_read(f"{inp}/capabilities/key")
            if caps and _has_south(caps):
                is_pad = True
                mac = (sysfs_read(f"{inp}/uniq") or "").lower() or mac
        if not is_pad:
            continue
        out.append({"path": f"/dev/{node}",
                    "name": fields.get("HID_NAME") or node,
                    "mac": mac, "vendor": ven, "product": prod,
                    "hid": True})
    return out


def _has_south(caps):
    """True when a capabilities/key bitmap has BTN_SOUTH, marking a gamepad."""
    words = caps.split()[::-1]              # sysfs prints MSB group first
    idx, off = 0x130 // 64, 0x130 % 64
    try:
        return idx < len(words) and bool(int(words[idx], 16) >> off & 1)
    except ValueError:
        return False


class RealWatcher:
    """Correlates presses on the hidden physical pads with the virtual ones.

    Steam's virtual pad carries nothing that points back at the hardware
    driving it. But a button press fires on both within milliseconds, so
    watching the real evdev nodes alongside SDL tells us which is which — and
    hands back the real MAC, which is what makes settings stick across
    sessions.
    """

    WINDOW_MS = 250
    # How fresh a real device's event must be to be claimed by a virtual pad.
    # The window above is how long events are kept; this is how close to the
    # press they have to be.
    #
    # 250ms let a pad claim the node of someone who had pressed a quarter of
    # a second earlier, which is how a Steam Controller ended up wearing an
    # 8bitdo's name. 60ms was the correction and it was too tight: pairing
    # then failed on a run where every pad was pressed, and succeeded on the
    # next — a race, which is the worst kind of bug to leave in. The theft
    # case is already prevented by the caller, which refuses to pair while
    # another pad is being pressed at all, so this only has to be tight
    # enough to rule out a stale event.
    CLAIM_MS = 150

    def __init__(self):
        self.fds = {}
        self.last = {}           # hidraw baseline report, per fd
        self.recent = []
        self.available = False
        self.last_refresh = 0

    def open(self):
        return self.refresh()

    def refresh(self):
        """Re-check which real nodes exist and open any new ones.

        Sampling once at startup was wrong: the tool deliberately does not
        wake controllers, so a pad's evdev node usually appears *after* we are
        already running. Missing it means the pad never pairs to its hardware,
        which shows up as a generic "Steam pad 36b8" label and a per-pad swap
        that cannot be remembered.
        """
        known = {info["path"] for info in self.fds.values()}
        # Both channels. evdev is the one that carries a MAC and works for a
        # pad Steam has not taken over; hidraw is the only one that carries
        # anything at all for a pad it has. See scan_hid_gamepads.
        for info in list(scan_real_gamepads()) + list(scan_hid_gamepads()):
            if info["path"] in known:
                continue
            try:
                self.fds[os.open(info["path"], os.O_RDONLY | os.O_NONBLOCK)] = info
            except OSError:
                pass
        self.available = bool(self.fds)
        return self.available

    # How often to look for device nodes that were not there a moment ago.
    # Opening only at startup and on a disconnect was wrong: a pad that
    # reconnects — which a Bluetooth pad does whenever it wakes, with a new
    # node and sometimes a new address — was then never watched at all, so
    # every press on it was invisible and it could not be identified.
    # Measured on an 8bitdo: "0 recent event(s)" while every button on it was
    # being pressed.
    REFRESH_MS = 1000

    def poll(self, now):
        if now - self.last_refresh >= self.REFRESH_MS:
            self.last_refresh = now
            self.refresh()
        if not self.fds:
            return
        try:
            ready, _, _ = select.select(list(self.fds), [], [], 0)
        except (OSError, ValueError):
            return
        for fd in ready:
            info = self.fds[fd]
            try:
                data = os.read(fd, 256 if info.get("hid") else 24 * 64)
            except OSError:
                continue
            if info.get("hid"):
                # An idle pad streams the same report forever, so arrival
                # says nothing; a press changes the bytes. The first report
                # only establishes the baseline.
                was = self.last.get(fd)
                self.last[fd] = data
                if was is not None and data != was:
                    self.recent.append((now, info))
                continue
            for off in range(0, len(data) - 23, 24):
                _, _, etype, _, value = struct.unpack_from("qqHHi", data, off)
                if etype == 1 and value == 1:       # EV_KEY press
                    self.recent.append((now, info))
        self.recent = [(t, i) for t, i in self.recent
                       if now - t <= self.WINDOW_MS]

    def claim(self, now, taken):
        """The single unclaimed real device that just fired, if unambiguous.

        Two people pressing at the same instant would make the pairing a
        guess, so that case is skipped rather than risking a wrong label.
        """
        hits = [i for t, i in self.recent
                if now - t <= self.CLAIM_MS and dev_ident(i) not in taken]
        if not hits:
            return None
        who = {dev_ident(i) for i in hits}
        return hits[-1] if len(who) == 1 else None

    def spare_devices(self, pads, sdl_pads):
        """Real devices that no pad here accounts for.

        A physical pad that SDL shows in its own right accounts for its own
        node — it is not spare. What is left is hardware driving something
        else: under Steam, a virtual pad.
        """
        taken = {dev_ident(p.real) for p in pads if p.real}
        seen = {(p.vendor, p.product) for p in sdl_pads
                if (p.vendor, p.product) != STEAM_VIRTUAL}
        out, listed = [], set()
        for info in self.fds.values():
            if dev_ident(info) in taken or dev_ident(info) in listed:
                continue
            listed.add(dev_ident(info))
            if (info["vendor"], info["product"]) in seen:
                continue
            out.append(info)
        return out

    def unclaimed_nintendo(self, pads):
        """True when a Nintendo-lettered device is present but unpaired.

        On its own an unpaired pad is harmless — it is only a name. It stops
        being harmless when one of the real devices nobody has claimed is
        Nintendo-lettered, because then the pad whose buttons are about to be
        bound might be that one.
        """
        taken = {p.real["path"] for p in pads if p.real}
        for info in self.fds.values():
            if info["path"] in taken:
                continue
            shim = _Shim(name=info["name"], mac=info["mac"],
                         vendor=info["vendor"], product=info["product"])
            shim.real = None
            if nintendo_layout(shim):
                return True
        return False

    def close(self):
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds.clear()
        self.last.clear()


def scan_pads(sdl):
    """Returns (pads, unmapped).

    SDL only reports a device as a game controller when it has a button
    mapping for it. Anything else is a bare joystick we cannot interpret —
    reported separately so an unsupported pad shows up as an explanation
    rather than as nothing at all.
    """
    pads, unmapped = [], []
    for i in range(sdl.SDL_NumJoysticks()):
        if sdl.SDL_IsGameController(i):
            pads.append(Pad(sdl, i))
        else:
            name = sdl.SDL_JoystickNameForIndex(i)
            unmapped.append(name.decode(errors="replace") if name
                            else f"device {i}")
    return pads, unmapped


# ------------------------------------------------------------------- state

def load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def save_known(known):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = KNOWN_PADS + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(known, fh, indent=2, sort_keys=True)
    os.replace(tmp, KNOWN_PADS)


def is_hardware_key(key):
    """True when a key identifies a physical controller.

    A MAC belongs to one piece of hardware forever. A `crc:` key only names a
    Steam virtual pad slot, and Steam moves controllers between slots between
    sessions — so a preference stored against one can resurface on somebody
    else's pad.
    """
    return bool(key) and not key.startswith("crc:")


def apply_known(pads, known):
    for p in pads:
        rec = known.get(p.store_key)
        if rec:
            # Records without a schema marker predate friendly labels, and
            # their `nickname` was auto-filled with whatever SDL happened to
            # report that run — which would override the real label forever.
            # Only honour a nickname from a record that knows what one means.
            p.nickname = rec.get("nickname") if rec.get("schema", 0) >= 2 else None
            # swap_faces used to mean "flip whatever the template held"; it now
            # means "write the mirrored mapping". Old values invert in effect,
            # so anything below schema 3 starts from the default. And it is
            # only trusted from a hardware-keyed record — see is_hardware_key.
            # Schema 4 records the difference between "the user chose this"
            # and "this was the default at the time". Only a deliberate choice
            # survives, so a pad whose layout we later learn about corrects
            # itself instead of staying wrong.
            # A deliberate choice is honoured on a `crc:` key too. The rule
            # against those exists because Steam parks pads in slots — but
            # these keys have proved stable per pad across sessions on the
            # test machine, and a pad that never pairs has no other key it
            # could ever be saved under. A setting that will not survive the
            # launch is worse than one that might land on a sibling's pad,
            # which two trigger squeezes undo.
            explicit = (rec.get("schema", 0) >= 4
                        and bool(rec.get("swap_explicit")))
            p.swap_explicit = explicit
            p.swap_faces = (bool(rec.get("swap_faces")) if explicit
                            else default_swap(p))



def remember(pads, known):
    for p in pads:
        if p.slot:
            known[p.store_key] = {
                "schema": 4,
                # Left null so the label stays automatic; set it by hand in
                # this file to override.
                "nickname": p.nickname,
                "guid": p.guid,
                "name": p.name,
                # The physical device, so a mis-pairing is visible in the file
                # rather than hidden behind a virtual pad's name.
                "hardware": p.real["name"] if p.real else None,
                "detected_as": p.label,
                # Only recorded against real hardware. Saving it under a
                # virtual-pad slot would hand the setting to whichever
                # controller Steam parks there next time.
                # Saved under whatever key this pad has, including a `crc:`
                # one — see apply_known.
                "swap_faces": p.swap_faces,
                "swap_explicit": bool(getattr(p, "swap_explicit", False)),
                # Recorded for the log only. It is never read back: it used
                # to be, and against a `crc:` slot key it handed one pad's
                # identity to whoever Steam parked there next.
                "last_seen": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
    save_known(known)


RUMBLE_ON_MS = 320          # long enough to feel, short enough to stay crisp
RUMBLE_GAP_MS = 900         # silence between pads so the cycle reads as steps
RUMBLE_STRENGTH = 0xA000    # ~63%; a full-power buzz is startling in the hand


class RumbleCycle:
    """Buzz each assigned pad in turn, forever, so everyone can feel which
    player they are without pressing anything.

    This is the answer to "whose controller is Player 2?" — the card lights up
    at the same moment the pad in someone's hands vibrates.
    """

    def __init__(self, sdl):
        self.sdl = sdl
        self.enabled = True
        self.pos = 0
        self.active = None       # instance_id currently buzzing
        self.next_at = 0
        self.stop_at = 0

    def update(self, pads):
        now = self.sdl.SDL_GetTicks()
        # Only pads that are genuinely still attached — buzzing a sleeping pad
        # silently "succeeds" and lights its bay for a controller nobody holds.
        # Filter before sorting: a spare pad beyond the four bays has slot
        # None, and sorting that against an int raises — which killed the
        # whole check screen on a five-pad DuckStation launch.
        seated = [p for p in pads if p.slot and p.attached()]
        targets = sorted(seated, key=lambda q: q.slot)
        if not self.enabled or not targets:
            self.active = None
            return
        if self.active is not None and now >= self.stop_at:
            self.active = None
        if now >= self.next_at:
            pad = targets[self.pos % len(targets)]
            pad.rumble(RUMBLE_STRENGTH, RUMBLE_ON_MS)
            self.active = pad.instance_id
            self.stop_at = now + RUMBLE_ON_MS
            self.next_at = now + RUMBLE_ON_MS + RUMBLE_GAP_MS
            self.pos = (self.pos + 1) % len(targets)


def new_slot_state():
    return {"order": {}, "seq": 0, "present": set()}


def bind_real(pad, info, known, pads):
    """Attach a physical device to a virtual pad and re-read its settings.

    The settings were loaded under the CRC key; now that the real MAC is
    known, anything saved against it wins.
    """
    pad.real = info
    rec = known.get(pad.store_key)
    if rec:
        pad.nickname = rec.get("nickname") if rec.get("schema", 0) >= 2 else None
        explicit = (rec.get("schema", 0) >= 4
                    and bool(rec.get("swap_explicit"))
                    and is_hardware_key(pad.store_key))
        pad.swap_explicit = explicit
        pad.swap_faces = (bool(rec.get("swap_faces")) if explicit
                          else default_swap(pad))
    else:
        # Pairing just told us what this really is; take the default it implies.
        if not getattr(pad, "swap_explicit", False):
            pad.swap_faces = default_swap(pad)
    label_pads(pads)


def resolve_slots(pads, st, claimed_p1=None):
    """Pack players into slots by wake order, contiguously.

    A pad that goes away and comes back is treated as newly arrived and joins
    at the end — it does not reclaim the slot it used to hold. Someone who
    took over while it was asleep keeps their place, which is what everyone
    in the room expects after a controller nods off mid-session.

    A pad that has claimed P1 keeps it regardless of arrival order.
    """
    for p in sorted(pads, key=lambda q: q.index):
        if p.key not in st["present"]:
            st["seq"] += 1
            st["order"][p.key] = st["seq"]
    st["present"] = {p.key for p in pads}

    order = sorted(pads, key=lambda q: (0 if q.key == claimed_p1 else 1,
                                        st["order"][q.key]))
    for i, p in enumerate(order):
        p.slot = i + 1 if i < MAX_PLAYERS else None
    return order


# ------------------------------------------------------------ ryujinx config

EMU_ENUM = r"""
import ctypes, sys
sdl = ctypes.CDLL(sys.argv[1])
class G(ctypes.Structure):
    _fields_ = [("d", ctypes.c_uint8 * 16)]
for h in (b"SDL_JOYSTICK_HIDAPI_STEAM", b"SDL_JOYSTICK_HIDAPI_STEAMDECK"):
    sdl.SDL_SetHint(h, b"0")
# SDL 2.32 and SDL3 hide Steam's virtual pads from anything not launched by
# Steam; SDL 2.30 shows them to everyone. Without this the enumeration is
# empty exactly when Steam Input is on, which is the configuration we run.
sdl.SDL_SetHint(b"SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD", b"1")
sdl.SDL_Init(0x00000200 | 0x00002000)
sdl.SDL_JoystickGetDeviceGUID.restype = G
sdl.SDL_JoystickGetDeviceGUID.argtypes = [ctypes.c_int]
sdl.SDL_JoystickGetGUIDString.argtypes = [G, ctypes.c_char_p, ctypes.c_int]
sdl.SDL_JoystickNameForIndex.restype = ctypes.c_char_p
for i in range(sdl.SDL_NumJoysticks()):
    b = ctypes.create_string_buffer(33)
    sdl.SDL_JoystickGetGUIDString(sdl.SDL_JoystickGetDeviceGUID(i), b, 33)
    n = sdl.SDL_JoystickNameForIndex(i)
    print(i, b.value.decode(), (n or b"?").decode(errors="replace"), sep="\t")
sdl.SDL_Quit()
"""


def find_emulator_sdl(exe=None):
    """Path to the libSDL2 Ryujinx actually links against.

    This matters more than it looks. SDL changed the bus type it reports for
    Bluetooth pads between 2.30 and 2.32, which lands in the GUID — the same
    Stadia pad is ...-0000-0005-... under the system SDL and ...-0000-0003-...
    under Ryujinx's bundled 2.30. Ids computed with the wrong SDL never match
    and Ryujinx logs "No matching controllers found" while every pad works
    perfectly in this tool.
    """
    for path in emulator_sdl_libs(exe):
        if "libsdl2" in os.path.basename(path).lower():
            return path
    return None


def find_emulator_sdl3(exe=None):
    """Same idea as find_emulator_sdl, for builds that ship SDL3 instead."""
    for path in emulator_sdl_libs(exe):
        if "libsdl3" in os.path.basename(path).lower():
            return path
    return None


def appimage_sdl_dir(exe):
    """Extract an AppImage's SDL library and return the directory holding it.

    An AppImage is an ELF header followed by a filesystem image, so the library
    cannot simply be read off disk. Ryubing's is plain squashfs, which
    unsquashfs opens at the offset the runtime reports — no FUSE, no mounting,
    no root. The result is cached under STATE_DIR against the image's size and
    mtime, so the cost is paid once per emulator update.

    Note --appimage-extract is NOT used: with a pattern it exits 0 and writes
    nothing at all, which is a poor way to find out something went wrong.
    """
    if not exe or not exe.lower().endswith(".appimage") or not os.path.isfile(exe):
        return None
    try:
        st = os.stat(exe)
    except OSError:
        return None

    key = f"{os.path.basename(exe)}-{st.st_size}-{int(st.st_mtime)}"
    cache = os.path.join(STATE_DIR, "sdl-cache", key)
    if os.path.isdir(cache):
        return cache
    if not shutil.which("unsquashfs"):
        return None

    try:
        off = subprocess.run([exe, "--appimage-offset"], capture_output=True,
                             text=True, timeout=20)
        offset = int(off.stdout.strip())
    except (subprocess.SubprocessError, OSError, ValueError):
        return None

    tmp = cache + ".tmp"
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
    except OSError:
        return None
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        subprocess.run(["unsquashfs", "-o", str(offset), "-d", tmp, "-no-progress",
                        exe, "usr/lib/libSDL*"],
                       capture_output=True, text=True, timeout=120)
    except (subprocess.SubprocessError, OSError):
        shutil.rmtree(tmp, ignore_errors=True)
        return None

    import glob
    if not glob.glob(os.path.join(tmp, "usr", "lib", "libSDL*")):
        shutil.rmtree(tmp, ignore_errors=True)
        return None

    try:
        os.replace(tmp, cache)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)
        return None

    # One cache entry per image; older versions of the same file are dead
    # weight the moment the emulator updates. Pruning happens after the
    # rename, and skips the entry just written — the pattern matches it too.
    for stale in glob.glob(os.path.join(os.path.dirname(cache),
                                        os.path.basename(exe) + "-*")):
        if os.path.abspath(stale) != os.path.abspath(cache):
            shutil.rmtree(stale, ignore_errors=True)
    print(f"extracted SDL from {os.path.basename(exe)}", flush=True)
    return cache


def emulator_sdl_libs(exe=None):
    """Every SDL library shipped with the install we are about to launch.

    Both majors, because which one is there is itself the answer to a
    question: Ryubing stable bundles SDL2 2.30, but Canary has moved to
    SDL3 (3.5.0 as of 1.3.351), and the two cannot be enumerated the same way.
    """
    import glob
    out = []
    if exe:
        # A tar.gz build keeps its libraries beside the binary; an AppImage
        # keeps them sealed inside, so we look in the extracted copy instead.
        # Deliberately no falling back to the flatpak's library afterwards:
        # borrowing one install's SDL to compute ids for another is the exact
        # mismatch this function exists to prevent.
        base = appimage_sdl_dir(exe) or os.path.dirname(os.path.abspath(exe))
        for pattern in ("libSDL[23]*.so*", "lib/libSDL[23]*.so*",
                        "usr/lib/libSDL[23]*.so*"):
            out.extend(sorted(glob.glob(os.path.join(base, pattern))))
        return out
    for root in ("/var/lib/flatpak/app", os.path.expanduser("~/.local/share/flatpak/app")):
        out.extend(sorted(glob.glob(f"{root}/*yu*/*/*/*/files/bin/libSDL[23]*.so*")))
    return out


def emulator_sdl3_only(exe=None):
    """True when the install ships SDL3 and no SDL2. Both are enumerated now;
    this is kept because which major an install ships is the first thing worth
    knowing when its ids come out wrong."""
    names = [os.path.basename(p).lower() for p in emulator_sdl_libs(exe)]
    return (any("libsdl3" in n for n in names)
            and not any("libsdl2" in n for n in names))


EMU_ENUM3 = r"""
import ctypes, sys
sdl = ctypes.CDLL(sys.argv[1])
class G(ctypes.Structure):
    _fields_ = [("d", ctypes.c_uint8 * 16)]
for h in (b"SDL_JOYSTICK_HIDAPI_STEAM", b"SDL_JOYSTICK_HIDAPI_STEAMDECK"):
    sdl.SDL_SetHint(h, b"0")
# SDL 2.32 and SDL3 hide Steam's virtual pads from anything not launched by
# Steam; SDL 2.30 shows them to everyone. Without this the enumeration is
# empty exactly when Steam Input is on, which is the configuration we run.
sdl.SDL_SetHint(b"SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD", b"1")
sdl.SDL_Init(0x00000200 | 0x00002000)
sdl.SDL_GetJoysticks.restype = ctypes.POINTER(ctypes.c_uint32)
sdl.SDL_GetJoysticks.argtypes = [ctypes.POINTER(ctypes.c_int)]
sdl.SDL_GetJoystickGUIDForID.restype = G
sdl.SDL_GetJoystickGUIDForID.argtypes = [ctypes.c_uint32]
sdl.SDL_GUIDToString.argtypes = [G, ctypes.c_char_p, ctypes.c_int]
sdl.SDL_GetJoystickNameForID.restype = ctypes.c_char_p
sdl.SDL_GetJoystickNameForID.argtypes = [ctypes.c_uint32]
sdl.SDL_free.argtypes = [ctypes.c_void_p]
count = ctypes.c_int(0)
ids = sdl.SDL_GetJoysticks(ctypes.byref(count))
# SDL3 dropped device indices; the array's own order is the enumeration order,
# which is the closest thing to SDL2's device index.
for i in range(count.value if ids else 0):
    b = ctypes.create_string_buffer(33)
    sdl.SDL_GUIDToString(sdl.SDL_GetJoystickGUIDForID(ids[i]), b, 33)
    n = sdl.SDL_GetJoystickNameForID(ids[i])
    print(i, b.value.decode(), (n or b"?").decode(errors="replace"), sep="\t")
if ids:
    sdl.SDL_free(ids)
sdl.SDL_Quit()
"""


def emulator_gamepads(exe=None):
    """(index, guid_hex, name) as Ryujinx's own SDL will see them.

    Run in a throwaway subprocess: two libSDL2 builds share a SONAME, so
    loading both in one process gets us whichever landed first — the UI keeps
    the system SDL, this borrows the emulator's.
    """
    lib, src = find_emulator_sdl(exe), EMU_ENUM
    if not lib:
        # Ryubing Canary moved to SDL3, which is a different C API: no device
        # indices, joystick ids come back as an array, and the getters are
        # renamed. Same GUIDs out the other end.
        lib, src = find_emulator_sdl3(exe), EMU_ENUM3
    if not lib:
        return None
    # Set in the environment, not only via SDL_SetHint: SDL3 reads this one
    # straight from the environment before the hint system is consulted, so a
    # SDL_SetHint call inside the script is ignored there.
    env = dict(os.environ, SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD="1")
    try:
        out = subprocess.run([sys.executable, "-c", src, lib], env=env,
                             capture_output=True, text=True, timeout=20)
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    rows = []
    for line in out.stdout.splitlines():
        bits = line.split("\t")
        if len(bits) == 3 and bits[0].isdigit():
            rows.append((int(bits[0]), bits[1], bits[2]))
    return rows or None


# Emulators we know how to configure, and the substrings that identify them in
# a flatpak app id or a binary name. Adding one here is not enough on its own —
# see PLAN.md section 7 for the functions a new backend has to supply.
BACKENDS = {
    "ryujinx": ("ryujinx", "ryubing"),
    "wheelwizard": ("wheelwizard",),
    "dolphin": ("dolphin",),
    "eden": ("eden",),
    "cemu": ("cemu",),
    "gopher64": ("gopher",),
    "duckstation": ("duckstation",),
}


def split_command(argv):
    """argv -> (flags, positionals, command).

    Everything after a bare "--" is the command to run once the check passes,
    and is never interpreted here — its flags belong to the emulator, not us.
    """
    if "--" in argv:
        cut = argv.index("--")
        head, cmd = argv[:cut], argv[cut + 1:]
    else:
        head, cmd = argv, []
    flags = [a for a in head if a.startswith("--")]
    positional = [a for a in head if not a.startswith("--")]
    return flags, positional, cmd


def command_target(cmd):
    """What a launch command actually runs: a flatpak app id, or a binary
    name. Returns None when we cannot tell."""
    if not cmd:
        return None
    if os.path.basename(cmd[0]) == "flatpak":
        rest = cmd[1:]
        if rest and rest[0] == "run":
            for arg in rest[1:]:
                if not arg.startswith("-"):
                    return arg          # first non-flag after "run"
        return None
    return os.path.basename(cmd[0])


def command_exe(cmd):
    """The executable a command runs, or None when it goes through flatpak."""
    if not cmd or os.path.basename(cmd[0]) == "flatpak":
        return None
    return cmd[0]


def backend_for(target):
    """The config backend that handles this target, or None if we have none
    — in which case the check still runs, it just writes nothing."""
    if not target:
        return None
    low = target.lower()
    for name, needles in BACKENDS.items():
        if any(n in low for n in needles):
            return name
    return None


def command_rom(cmd):
    """The ROM in a launch command: the last argument that is a file on disk.
    Only used for the per-game lookup, so guessing wrong costs nothing."""
    for arg in reversed(cmd):
        if not arg.startswith("-") and os.path.isfile(arg):
            return arg
    return None


def find_app_id():
    if not shutil.which("flatpak"):
        return DEFAULT_APP_ID
    try:
        out = subprocess.run(["flatpak", "list", "--app", "--columns=application"],
                             capture_output=True, text=True, timeout=15).stdout
        for line in out.splitlines():
            if "ryu" in line.lower():
                return line.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return DEFAULT_APP_ID


def find_config(app_id=None, exe=None):
    """Config.json for the install we are actually about to launch.

    A flatpak keeps it inside its own ~/.var/app sandbox; an AppImage or tar
    build uses ~/.config/Ryujinx, or a "portable" folder beside the binary.
    Confusing the two is a silent failure: we would write the flatpak's config
    and then launch the AppImage, which reads somewhere else entirely and
    behaves exactly as if the tool had done nothing.
    """
    candidates = []
    if exe:
        base = os.path.dirname(os.path.abspath(exe))
        candidates.append(os.path.join(base, "portable", "Config.json"))
    elif app_id:
        candidates.append(os.path.expanduser(
            f"~/.var/app/{app_id}/config/Ryujinx/Config.json"))
        if app_id != DEFAULT_APP_ID:
            candidates.append(os.path.expanduser(
                f"~/.var/app/{DEFAULT_APP_ID}/config/Ryujinx/Config.json"))
    else:
        candidates.append(os.path.expanduser(
            f"~/.var/app/{DEFAULT_APP_ID}/config/Ryujinx/Config.json"))
    # Stable and Canary share this one unless portable mode is on.
    candidates.append(os.path.expanduser("~/.config/Ryujinx/Config.json"))

    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


DSU_HOST = "127.0.0.1"
DSU_PORT = 26760


# A complete, standard SDL gamepad binding — used when the user's config has
# no gamepad entry to clone from, i.e. a fresh Ryujinx install. Field names and
# value spellings are taken verbatim from a real Ryujinx-written entry rather
# than guessed; the schema is undocumented.
DEFAULT_ENTRY = {
    "left_joycon_stick": {"joystick": "Left", "invert_stick_x": False,
                          "invert_stick_y": False, "rotate90_cw": False,
                          "stick_button": "LeftStick"},
    "right_joycon_stick": {"joystick": "Right", "invert_stick_x": False,
                           "invert_stick_y": False, "rotate90_cw": False,
                           "stick_button": "RightStick"},
    "deadzone_left": 0.1,
    "deadzone_right": 0.1,
    "range_left": 1,
    "range_right": 1,
    "trigger_threshold": 0.5,
    # CemuHook with a real address, not the empty one a fresh entry carries:
    # the backend is the only way to real gyro under Steam, because Ryujinx's
    # own GamepadDriver backend reads the Steam virtual gamepad, which has no
    # IMU. 127.0.0.1:26760 is the standard DSU address and what
    # SteamDeckGyroDSU publishes on; proven on the Deck 2026-09-19 with
    # Breath of the Wild's shrines and bow aiming, SteamOS gyro off. SelfSteam
    # writes the same two values when it creates a Ryubing shortcut, so the
    # two agree rather than overwriting each other every launch.
    "motion": {"slot": 0, "alt_slot": 0, "mirror_input": False,
               "dsu_server_host": DSU_HOST, "dsu_server_port": DSU_PORT,
               "motion_backend": "CemuHook", "sensitivity": 100,
               "gyro_deadzone": 1, "enable_motion": True},
    "rumble": {"strong_rumble": 1, "weak_rumble": 1, "enable_rumble": True},
    "led": {"enable_led": False, "turn_off_led": False, "use_rainbow": False,
            "led_color": 0},
    "left_joycon": {"button_minus": "Back", "button_l": "LeftShoulder",
                    "button_zl": "LeftTrigger",
                    "button_sl": "SingleLeftTrigger0",
                    "button_sr": "SingleRightTrigger0",
                    "dpad_up": "DpadUp", "dpad_down": "DpadDown",
                    "dpad_left": "DpadLeft", "dpad_right": "DpadRight"},
    "right_joycon": {"button_plus": "Start", "button_r": "RightShoulder",
                     "button_zr": "RightTrigger",
                     "button_sl": "SingleLeftTrigger1",
                     "button_sr": "SingleRightTrigger1",
                     "button_x": "X", "button_b": "B",
                     "button_y": "Y", "button_a": "A"},
    "version": 1,
    "backend": "GamepadSDL2",
    "id": "",
    "name": "",
    "controller_type": "ProController",
    "player_index": "Player1",
}


def pick_template(entries, pad):
    """Reuse the user's own button maps rather than inventing any.

    Prefer an entry already written for this exact controller; otherwise any
    SDL gamepad entry. Ryujinx stores SDL's normalised button names, so a map
    from one gamepad transfers cleanly to another.
    """
    for e in entries:
        eid = e.get("id", "")
        if "-" in eid and eid.split("-", 1)[1] == pad.guid:
            return copy.deepcopy(e)
    for e in entries:
        if e.get("backend") == "GamepadSDL2":
            return copy.deepcopy(e)
    # Nothing to clone — a fresh Ryujinx install. Everything except the face
    # buttons comes from the template, so without this the tool could not
    # write a usable entry at all.
    return copy.deepcopy(DEFAULT_ENTRY)


# Confirmed against a real GamepadSDL2 entry: Ryujinx names face buttons with
# SDL's own letters, so the identity mapping is literally what-you-see-is-what
# -you-get — press the button marked A, the game receives A.
FACE_IDENTITY = {"button_a": "A", "button_b": "B",
                 "button_x": "X", "button_y": "Y"}
FACE_MIRRORED = {"button_a": "B", "button_b": "A",
                 "button_x": "Y", "button_y": "X"}


# Every binding a gamepad entry must actually have. SL/SR are left out: they
# are Joy-Con rail buttons and are legitimately unbound on anything else.
REQUIRED_BINDINGS = {
    "left_joycon_stick": ("joystick", "stick_button"),
    "right_joycon_stick": ("joystick", "stick_button"),
    "left_joycon": ("button_minus", "button_l", "button_zl",
                    "dpad_up", "dpad_down", "dpad_left", "dpad_right"),
    "right_joycon": ("button_plus", "button_r", "button_zr",
                     "button_a", "button_b", "button_x", "button_y"),
}


def missing_bindings(entry):
    """Bindings that are absent, blank or explicitly Unbound.

    The tool reads sticks and buttons through SDL, so they look fine on screen
    whatever the config says — but only the face mapping is written from
    scratch. Everything else is copied from the existing entry, so a gap there
    is invisible until the game starts and half a controller does nothing.
    """
    gaps = []
    for section, keys in REQUIRED_BINDINGS.items():
        block = entry.get(section)
        if not isinstance(block, dict):
            gaps.extend(f"{section}.{k}" for k in keys)
            continue
        for key in keys:
            value = block.get(key)
            if value in (None, "", "Unbound"):
                gaps.append(f"{section}.{key}")
    return gaps


def repair_entry(entry):
    """Fill any gap from the known-good defaults. Returns what was repaired."""
    repaired = []
    for name in missing_bindings(entry):
        section, key = name.split(".", 1)
        entry.setdefault(section, {})[key] = DEFAULT_ENTRY[section][key]
        repaired.append(name)
    return repaired


def repair_motion(entry):
    """Give a CemuHook profile an address if it has none.

    Cloning is how everything but the face mapping is inherited, so a profile
    cloned from an entry with an empty host stays deaf to the DSU server for
    ever. Only the empty case is touched: a host the user has actually set —
    a phone, another machine — is theirs.
    """
    motion = entry.get("motion")
    if not isinstance(motion, dict) or motion.get("motion_backend") != "CemuHook":
        return False
    if motion.get("dsu_server_host") or motion.get("dsu_server_port"):
        return False
    motion["dsu_server_host"] = DSU_HOST
    motion["dsu_server_port"] = DSU_PORT
    return True


def config_binding_gaps(cfg_path):
    """Gaps in the config we would be cloning from, for warning up front."""
    data = load_json(cfg_path, None) if cfg_path else None
    if not data:
        return []
    for entry in data.get("input_config") or []:
        if entry.get("backend") == "GamepadSDL2":
            return missing_bindings(entry)
    return []


def apply_face_mapping(entry, pad):
    """Write the face mapping outright rather than swapping what was there.

    Swapping depends on the template being in a known state; setting it
    guarantees the result whatever we cloned from.
    """
    rj = entry.get("right_joycon")
    if not isinstance(rj, dict):
        return
    for key, value in (FACE_MIRRORED if pad.swap_faces else FACE_IDENTITY).items():
        if key in rj:
            rj[key] = value


def emulator_id_for(pad, rows, used):
    """Match one of our pads to the emulator's enumeration.

    Vendor and product survive the SDL version difference even though the bus
    byte does not, so they are what we match on; identical models are matched
    in order.
    """
    if rows is None:
        return None

    # Prefer an exact name-CRC hit. Steam's virtual pads all share one
    # vendor/product, so vendor alone cannot separate them — but the CRC is
    # per-device and, unlike the bus byte, agrees across SDL versions.
    for want_crc in (True, False):
        for idx, guid_hex, _ in rows:
            if (idx, guid_hex) in used:
                continue
            if guid_vendor_product(guid_hex) != (pad.vendor, pad.product):
                continue
            if want_crc and guid_name_crc(guid_hex) != pad.name_crc:
                continue
            used.add((idx, guid_hex))
            return f"{idx}-{ryujinx_guid(guid_hex)}"
    return None


def build_entries(existing, pads, rows=None):
    out, problems = [], []
    used = set()
    for p in sorted((q for q in pads if q.slot), key=lambda q: q.slot):
        tpl = pick_template(existing, p)
        tpl["id"] = emulator_id_for(p, rows, used) or p.ryujinx_id
        tpl["player_index"] = f"Player{p.slot}"
        tpl["backend"] = "GamepadSDL2"
        if "name" in tpl:
            tpl["name"] = p.label      # what Ryujinx's own input UI shows
        apply_face_mapping(tpl, p)
        # The face mapping is authored; the rest is inherited. Patch any hole
        # in what was inherited rather than shipping a half-dead controller.
        repaired = repair_entry(tpl)
        if repair_motion(tpl):
            repaired.append(f"motion.dsu_server ({DSU_HOST}:{DSU_PORT})")
        if repaired:
            print(f"repaired for {p.label}: {', '.join(repaired)}", flush=True)
        out.append(tpl)

    ids = [e["id"] for e in out]
    for i in set(ids):
        if ids.count(i) > 1:
            problems.append(f"duplicate id would be written: {i}")
    if out and not any(e["player_index"] == "Player1" for e in out):
        problems.append("nothing assigned to Player 1 — no controller would work")
    return out, problems


def write_config(cfg_path, pads, exe=None):
    data = load_json(cfg_path, None)
    if data is None:
        return ["cannot read Config.json"]

    rows = emulator_gamepads(exe)
    if rows is None:
        problems_pre = ["Could not read the emulator's own SDL — ids may not "
                        "match. Flatpak and tar builds are supported, SDL2 or "
                        "SDL3; an AppImage keeps its libraries inside the "
                        "image."]
    else:
        problems_pre = []
    entries, problems = build_entries(data.get("input_config") or [], pads, rows)
    problems = problems_pre + problems
    if problems:
        return problems
    if not entries:
        return ["no controllers assigned"]

    os.makedirs(BACKUP_DIR, exist_ok=True)
    shutil.copy2(cfg_path, os.path.join(
        BACKUP_DIR, f"Config.{time.strftime('%Y%m%d-%H%M%S')}.json"))

    data["input_config"] = entries
    tmp = cfg_path + ".preflight.tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, cfg_path)
    return []


# --------------------------------------------------------------------- eden

EDEN_APP_ID = "dev.eden_emu.eden"

# Eden stores raw joystick numbers — "button:9", "axis:2", "hat:0" — which
# differ per pad, so every one is read back from SDL rather than assumed.
EDEN_SIMPLE = {
    "button_l": BTN_LSHOULDER, "button_r": BTN_RSHOULDER,
    "button_minus": BTN_BACK, "button_plus": BTN_START,
    "button_home": sdlui.BTN_GUIDE,
    "button_lstick": BTN_LSTICK, "button_rstick": BTN_RSTICK,
    "button_dup": BTN_DPAD_UP, "button_ddown": BTN_DPAD_DOWN,
    "button_dleft": BTN_DPAD_LEFT, "button_dright": BTN_DPAD_RIGHT,
}
EDEN_FACE_BTN = {"A": BTN_A, "B": BTN_B, "X": BTN_X, "Y": BTN_Y}
EDEN_TRIGGERS = {"button_zl": sdlui.AXIS_TRIGGERLEFT,
                 "button_zr": sdlui.AXIS_TRIGGERRIGHT}
# Joy-Con SL/SR and the extras have no place on a Pro Controller. Eden's own
# word for an unset binding is the literal [empty]; leaving them mapped to the
# shoulder buttons, as a hand-made config tends to, makes L fire twice.
EDEN_EMPTY = ("button_screenshot", "button_slleft", "button_slright",
              "button_srleft", "button_srright", "motionleft", "motionright")
EDEN_HAT_DIRECTION = {1: "up", 2: "right", 4: "down", 8: "left"}


# SDL gives the same pad two different GUIDs depending on whether HIDAPI or
# evdev claims it — the version field differs, and so does the enumeration
# order that becomes Eden's `port:`. Measured on one Xbox pad:
#
#   HIDAPI  05005f805e040000e002000000006800   /dev/hidraw7
#   evdev   05005f805e040000e002000003090000   /dev/input/event23
#
# Rather than guess which one Eden picks, we force the question: enumerate
# with HIDAPI off and launch Eden with HIDAPI off, so both sides agree.
EDEN_NO_HIDAPI = "SDL_JOYSTICK_HIDAPI=0"

EDEN_ENUM = r"""
import ctypes, os, sys
sdl = ctypes.CDLL(sys.argv[1])
sdl.SDL_SetHint(b"SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD", b"1")
sdl.SDL_SetHint(b"SDL_JOYSTICK_HIDAPI", b"0")
sdl.SDL_Init(0x00000200 | 0x00002000)


class G(ctypes.Structure):
    _fields_ = [("d", ctypes.c_uint8 * 16)]


sdl.SDL_JoystickGetDeviceGUID.restype = G
sdl.SDL_JoystickGetDeviceGUID.argtypes = [ctypes.c_int]
sdl.SDL_JoystickGetGUIDString.argtypes = [G, ctypes.c_char_p, ctypes.c_int]
for f in ("SDL_JoystickNameForIndex", "SDL_JoystickPathForIndex"):
    if hasattr(sdl, f):
        getattr(sdl, f).restype = ctypes.c_char_p


def mac_of(path):
    if not path.startswith("/dev/input/event"):
        return ""
    node = "/sys/class/input/%s/device/uniq" % os.path.basename(path)
    try:
        return open(node).read().strip().lower()
    except OSError:
        return ""


for i in range(sdl.SDL_NumJoysticks()):
    b = ctypes.create_string_buffer(33)
    sdl.SDL_JoystickGetGUIDString(sdl.SDL_JoystickGetDeviceGUID(i), b, 33)
    path = b""
    if hasattr(sdl, "SDL_JoystickPathForIndex"):
        path = sdl.SDL_JoystickPathForIndex(i) or b""
    print(i, b.value.decode(), mac_of(path.decode(errors="replace")), sep="\t")
sdl.SDL_Quit()
"""


def eden_devices():
    """[(port, sdl_guid, mac)] as Eden will enumerate them: HIDAPI off."""
    lib = "libSDL2-2.0.so.0"
    key, _, value = EDEN_NO_HIDAPI.partition("=")
    env = dict(os.environ, **{key: value})
    env.pop("SDL_GAMECONTROLLER_IGNORE_DEVICES", None)
    try:
        out = subprocess.run([sys.executable, "-c", EDEN_ENUM, lib], env=env,
                             capture_output=True, text=True, timeout=20)
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    rows = []
    for line in out.stdout.splitlines():
        bits = line.split("\t")
        if len(bits) == 3 and bits[0].isdigit():
            rows.append((int(bits[0]), bits[1], bits[2]))
    return rows or None


def eden_identity(pad, rows):
    """(port, guid) for this pad in Eden's own enumeration.

    Matched by MAC for a physical pad, and by GUID for a Steam virtual one,
    which has no MAC but does carry a unique name-CRC.
    """
    if rows:
        if pad.mac:
            for port, guid, mac in rows:
                if mac and mac.lower() == pad.mac.lower():
                    return port, guid
        for port, guid, _mac in rows:
            if guid == pad.sdl_guid:
                return port, guid
    return pad.index, pad.sdl_guid


def eden_guid(sdl_guid_hex):
    """SDL GUID -> the one Eden stores: the same 32 hex digits with the
    16-bit name-CRC zeroed. Ryujinx does this too (§3); Eden writes it plain
    rather than in .NET's dashed byte order."""
    b = bytearray(bytes.fromhex(sdl_guid_hex))
    b[2:4] = b"\x00\x00"
    return bytes(b).hex()


def eden_fragment(sdl, handle, kind, index):
    """The device-specific tail of an Eden binding, straight from SDL's own
    mapping for this pad. None when the pad has no such control."""
    fn = (sdl.SDL_GameControllerGetBindForAxis if kind == "axis"
          else sdl.SDL_GameControllerGetBindForButton)
    try:
        bind = fn(handle, index)
    except (AttributeError, ctypes.ArgumentError):
        return None
    if bind.bindType == sdlui.BIND_BUTTON:
        return f"button:{bind.value.button}"
    if bind.bindType == sdlui.BIND_AXIS:
        return f"axis:{bind.value.axis},threshold:0.500000,invert:+"
    if bind.bindType == sdlui.BIND_HAT:
        direction = EDEN_HAT_DIRECTION.get(bind.value.hat.hat_mask)
        if direction:
            return f"hat:{bind.value.hat.hat},direction:{direction}"
    return None


def eden_stick(sdl, handle, x_axis, y_axis):
    for axis in (x_axis, y_axis):
        bind = sdl.SDL_GameControllerGetBindForAxis(handle, axis)
        if bind.bindType != sdlui.BIND_AXIS:
            return None
    bx = sdl.SDL_GameControllerGetBindForAxis(handle, x_axis).value.axis
    by = sdl.SDL_GameControllerGetBindForAxis(handle, y_axis).value.axis
    return (f"axis_x:{bx},axis_y:{by},offset_x:0.000000,offset_y:0.000000,"
            f"invert_x:+,invert_y:+,deadzone:0.150000")


def eden_player_values(sdl, pad, port, sdl_guid=None):
    """Every player_<n>_* value for one pad, without the player prefix."""
    guid = eden_guid(sdl_guid or pad.sdl_guid)
    head = f"engine:sdl,port:{port},guid:{guid}"
    face = FACE_MIRRORED if pad.swap_faces else FACE_IDENTITY

    out = {"type": "0",            # Pro Controller, as everywhere else here
           "connected": "true",
           "vibration_enabled": "true",
           "vibration_strength": "100",
           "profile_name": ""}

    wanted = dict(EDEN_SIMPLE)
    for key, letter in face.items():
        wanted[key] = EDEN_FACE_BTN[letter]

    missing = []
    for key, button in wanted.items():
        frag = eden_fragment(sdl, pad.handle, "button", button)
        out[key] = f'"{head},{frag}"' if frag else "[empty]"
        if not frag:
            missing.append(key)
    for key, axis in EDEN_TRIGGERS.items():
        frag = eden_fragment(sdl, pad.handle, "axis", axis)
        out[key] = f'"{head},{frag}"' if frag else "[empty]"
        if not frag:
            missing.append(key)
    for key, (x, y) in (("lstick", (sdlui.AXIS_LEFTX, sdlui.AXIS_LEFTY)),
                        ("rstick", (sdlui.AXIS_RIGHTX, sdlui.AXIS_RIGHTY))):
        frag = eden_stick(sdl, pad.handle, x, y)
        out[key] = f'"{head},{frag}"' if frag else "[empty]"
        if not frag:
            missing.append(key)
    for key in EDEN_EMPTY:
        out[key] = "[empty]"
    return out, missing


VALVE_VIRTUAL = (0x28de, 0x11ff)


def steam_input_off(pads):
    """True when a pad is reaching us as real hardware.

    With Steam Input on, every pad arrives as an identical Valve virtual
    controller; with it off, the real vendor and product come through. The
    Steam Controller stays virtual either way, since Valve always manages its
    own pad, so one non-Valve pad is enough to tell.
    """
    return any((p.vendor, p.product) != VALVE_VIRTUAL for p in pads)


def log_layouts(pads):
    """What this tool believes each pad IS, at the moment it matters.

    Every face-button bug this week has come down to one of these facts
    disagreeing with the pad in someone's hands: which hardware it paired to,
    whether that hardware letters its buttons Nintendo-style, and therefore
    which way the shapes get bound. Printed together, the next such report is
    a lookup rather than an investigation.
    """
    for p in sorted(pads, key=lambda q: q.slot or 9):
        if not p.slot:
            continue
        real = p.real or {}
        ids = (f"{real.get('vendor', 0):04x}:{real.get('product', 0):04x}"
               if real else f"{p.vendor:04x}:{p.product:04x} (virtual)")
        print(f"layout: P{p.slot} {p.display} | sdl='{p.name}'"
              f" gc='{p.gc_name}' | hardware="
              f"{real.get('name', 'none')} [{ids}]"
              f" | nintendo_layout={nintendo_layout(p)}"
              f" relabelled={steam_relabelled(p)}"
              f" | swap_faces={p.swap_faces}", flush=True)


def log_pads(pads, when):
    """One line per pad into launch.log. Counts alone are not enough: when a
    run misbehaves the question is always *which* pads were seen, and with
    Steam Input on or off the same four controllers arrive with entirely
    different ids."""
    for pad in sorted(pads, key=lambda p: (p.slot or 99, p.index)):
        print(f"{when}: slot={pad.slot or '-'} idx={pad.index} "
              f"guid={pad.sdl_guid} mac={pad.mac or '-'} "
              f"name={pad.gc_name or pad.name!r}", flush=True)


def find_eden_config(app_id=None, exe=None):
    candidates = []
    if exe:
        base = os.path.dirname(os.path.abspath(exe))
        candidates.append(os.path.join(base, "user", "config", "qt-config.ini"))
    elif app_id:
        candidates.append(os.path.expanduser(
            f"~/.var/app/{app_id}/config/eden/qt-config.ini"))
    candidates.append(os.path.expanduser("~/.config/eden/qt-config.ini"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def eden_config_target(app_id=None, exe=None):
    """Where Eden's config is, or where it would be. A user who installs Eden
    and runs preflight before ever opening Eden itself has no qt-config.ini
    at all; refusing to write would strand exactly the person this is for."""
    found = find_eden_config(app_id, exe)
    if found:
        return found
    if app_id:
        return os.path.expanduser(
            f"~/.var/app/{app_id}/config/eden/qt-config.ini")
    return os.path.expanduser("~/.config/eden/qt-config.ini")


def set_ini_keys(path, section, values):
    """Replace or add `key=value` lines inside one section of a Qt ini,
    leaving every other byte of the file alone.

    Deliberately line-surgical rather than parse-and-rewrite: qt-config.ini
    is over a thousand lines of settings we have no business reformatting,
    and its keys carry a ``\\default`` twin that a tidier writer would lose.
    """
    try:
        with open(path) as fh:
            lines = fh.read().split("\n")
    except FileNotFoundError:
        lines = []                      # nothing here yet: we are the first
    except OSError:
        return False

    start = None
    for i, line in enumerate(lines):
        if line.strip() == f"[{section}]":
            start = i + 1
            break
    if start is None:
        # A fresh install has no file, or has one without our section. Qt
        # fills in every setting it does not find, so a config holding only
        # [Controls] is a perfectly good starting point.
        while lines and not lines[-1].strip():
            lines.pop()
        lines.append(f"[{section}]")
        start = len(lines)
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].startswith("["):
            end = i
            break

    where = {}
    for i in range(start, end):
        key, sep, _ = lines[i].partition("=")
        if sep:
            where[key.strip()] = i

    additions = []
    for key, value in values.items():
        line = f"{key}={value}"
        if key in where:
            lines[where[key]] = line
        else:
            additions.append(line)
    if additions:
        lines[end:end] = additions

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        return False
    tmp = path + ".preflight.tmp"
    with open(tmp, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return True


def write_eden_config(cfg_path, pads, sdl):
    if not cfg_path:
        return ["Eden's qt-config.ini was not found."]
    assigned = sorted([p for p in pads if p.slot], key=lambda p: p.slot)
    if not assigned:
        return ["no controllers assigned"]

    rows = eden_devices()
    if rows is None:
        return ["Could not enumerate controllers the way Eden will."]

    values = {}
    problems = []
    for pad in assigned:
        if not pad.handle:
            problems.append(f"{pad.label}: SDL has no handle for this pad.")
            continue
        # Eden's port is an SDL enumeration index — but of ITS enumeration,
        # which is not ours unless we pin the driver. Same for the guid.
        port, raw_guid = eden_identity(pad, rows)
        fields, missing = eden_player_values(sdl, pad, port, raw_guid)
        if missing:
            problems.append(f"{pad.label}: no SDL mapping for "
                            f"{', '.join(missing[:3])}")
        for key, value in fields.items():
            values[f"player_{pad.slot - 1}_{key}"] = value
            values[f"player_{pad.slot - 1}_{key}\\default"] = "false"

    # Any player beyond the ones we assigned must be switched off, or Eden
    # keeps a phantom controller from a previous session in the game.
    for n in range(len(assigned), 8):
        values[f"player_{n}_connected"] = "false"
        values[f"player_{n}_connected\\default"] = "false"

    if problems:
        return problems

    os.makedirs(BACKUP_DIR, exist_ok=True)
    if os.path.isfile(cfg_path):
        shutil.copy2(cfg_path, os.path.join(
            BACKUP_DIR, f"qt-config.{time.strftime('%Y%m%d-%H%M%S')}.ini"))
    if not set_ini_keys(cfg_path, "Controls", values):
        return ["could not write the [Controls] section of qt-config.ini"]
    return []


# ---------------------------------------------------------------- duckstation

# Read out of DuckStation's own source (src/util/sdl_input_source.cpp,
# src/core/analog_controller.cpp, src/core/controller.cpp), not guessed.
#
# A binding is "SDL-<player index>/<name>". Buttons carry SDL's Xbox-style
# names whatever pad is held — the PlayStation names in that file are for
# display only — and an axis carries a direction: "+" or "-" for half of it,
# "Full" for the whole throw.
DUCK_BUTTON = {"cross": "A", "circle": "B", "square": "X", "triangle": "Y",
               "select": "Back", "start": "Start",
               "l1": "LeftShoulder", "r1": "RightShoulder",
               "l3": "LeftStick", "r3": "RightStick",
               "up": "DPadUp", "down": "DPadDown",
               "left": "DPadLeft", "right": "DPadRight"}
DUCK_AXIS = {"l2": "+LeftTrigger", "r2": "+RightTrigger",
             "lleft": "-LeftX", "lright": "+LeftX",
             "lup": "-LeftY", "ldown": "+LeftY",
             "rleft": "-RightX", "rright": "+RightX",
             "rup": "-RightY", "rdown": "+RightY"}
# Both motors, so a DualShock rumbles. Same reasoning as everywhere else
# here: a pad that buzzed on the check screen should buzz in the game.
DUCK_MOTOR = {"largemotor": "LargeMotor", "smallmotor": "SmallMotor"}
# The INI's own spelling for each, in the order DuckStation writes them.
DUCK_KEYS = {
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "cross": "Cross", "circle": "Circle", "square": "Square",
    "triangle": "Triangle", "select": "Select", "start": "Start",
    "l1": "L1", "r1": "R1", "l2": "L2", "r2": "R2",
    "l3": "L3", "r3": "R3",
    "lup": "LUp", "ldown": "LDown", "lleft": "LLeft", "lright": "LRight",
    "rup": "RUp", "rdown": "RDown", "rleft": "RLeft", "rright": "RRight",
    "largemotor": "LargeMotor", "smallmotor": "SmallMotor",
}
# What-you-see-is-what-you-get by POSITION, which is what a PlayStation pad's
# shapes are: Cross is the bottom button and SDL's A is the bottom button.
# So A is cross, B is circle, X is square, Y is triangle, for every pad.
DUCK_FACE_IDENTITY = {"cross": "A", "circle": "B", "square": "X",
                      "triangle": "Y"}
# The same shapes when SDL's letters arrive mirrored, so Cross still lands
# on the bottom button. Chosen by the pad's swap setting, not by anything
# preflight tries to detect about the pad — see duck_pad_rows.
DUCK_FACE_MIRROR = {"cross": "B", "circle": "A", "square": "Y",
                    "triangle": "X"}

# There is no automatic Nintendo twin, deliberately. A PlayStation pad's shapes ARE
# positions, so every controller binds the same way: north is triangle,
# south is cross, east is circle, west is square, whatever letters the pad
# has printed on it. Three attempts at detecting a Nintendo-lettered pad and
# binding it the other way round all landed on the wrong pad, because on
# this hardware nothing SDL or Steam reports about a pad's identity is
# reliable. The mapping does not need to know, so it no longer asks.

# Two ports, and a multitap on port 1 makes four. The pads are not numbered
# consecutively when it is on: port 0 takes slots 0-3 as pads 0, 2, 3, 4
# (Controller::PortDisplayOrder), so four players land in Pad1, Pad3, Pad4,
# Pad5. Getting this wrong is silent — the sections exist either way.
DUCK_PADS_PLAIN = (1, 2)
DUCK_PADS_MULTITAP = (1, 3, 4, 5)


def duck_sections(count):
    """(section numbers, multitap mode) for this many players."""
    if count > 2:
        return DUCK_PADS_MULTITAP[:count], "Port1Only"
    return DUCK_PADS_PLAIN[:count], "Disabled"


# DuckStation names a pad by SDL's PLAYER index, so this asks its own SDL3
# what those are rather than trusting ours: same idea as EMU_ENUM, and the
# reason ids computed with the wrong SDL match nothing (§3).
DUCK_ENUM = r"""
import ctypes, sys
sdl = ctypes.CDLL(sys.argv[1])
class G(ctypes.Structure):
    _fields_ = [("d", ctypes.c_uint8 * 16)]
sdl.SDL_SetHint(b"SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD", b"1")
sdl.SDL_Init(0x00000200 | 0x00002000)
sdl.SDL_GetJoysticks.restype = ctypes.POINTER(ctypes.c_uint32)
sdl.SDL_GetJoysticks.argtypes = [ctypes.POINTER(ctypes.c_int)]
sdl.SDL_GetJoystickGUIDForID.restype = G
sdl.SDL_GetJoystickGUIDForID.argtypes = [ctypes.c_uint32]
sdl.SDL_GUIDToString.argtypes = [G, ctypes.c_char_p, ctypes.c_int]
sdl.SDL_GetJoystickPlayerIndexForID.restype = ctypes.c_int
sdl.SDL_GetJoystickPlayerIndexForID.argtypes = [ctypes.c_uint32]
sdl.SDL_GetJoystickNameForID.restype = ctypes.c_char_p
sdl.SDL_GetJoystickNameForID.argtypes = [ctypes.c_uint32]
sdl.SDL_free.argtypes = [ctypes.c_void_p]
count = ctypes.c_int(0)
ids = sdl.SDL_GetJoysticks(ctypes.byref(count))
for i in range(count.value if ids else 0):
    b = ctypes.create_string_buffer(33)
    sdl.SDL_GUIDToString(sdl.SDL_GetJoystickGUIDForID(ids[i]), b, 33)
    n = sdl.SDL_GetJoystickNameForID(ids[i])
    print(sdl.SDL_GetJoystickPlayerIndexForID(ids[i]), b.value.decode(),
          (n or b"?").decode(errors="replace"), sep="\t")
if ids:
    sdl.SDL_free(ids)
sdl.SDL_Quit()
"""


def duck_players(exe=None):
    """[(player index, guid, name)] as DuckStation's own SDL3 sees them."""
    lib = find_emulator_sdl3(exe)
    if not lib:
        return None
    env = dict(os.environ,
               SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD="1")
    try:
        out = subprocess.run([sys.executable, "-c", DUCK_ENUM, lib], env=env,
                             capture_output=True, text=True, timeout=30)
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    rows = []
    for line in out.stdout.splitlines():
        bits = line.split("\t")
        if len(bits) == 3 and bits[0].lstrip("-").isdigit():
            rows.append((int(bits[0]), bits[1], bits[2]))
    return rows or None


def duck_player_for(pad, rows, used):
    """Which SDL player index this pad is, as DuckStation will number it.

    Matched on the GUID its own SDL reports, then on vendor/product with the
    name-CRC, which survive the version difference the bus byte does not.
    Falls back to the player index our own SDL gave us.
    """
    for exact in (True, False):
        for player, guid, _name in rows or ():
            if player < 0 or player in used:
                continue
            if exact and guid != pad.sdl_guid:
                continue
            if not exact and (guid_vendor_product(guid)
                              != (pad.vendor, pad.product)
                              or guid_name_crc(guid) != pad.name_crc):
                continue
            used.add(player)
            return player
    fallback = getattr(pad, "player_index", -1)
    if fallback >= 0 and fallback not in used:
        used.add(fallback)
        return fallback
    return None


DUCK_DATA_DIRS = ("~/.local/share/duckstation",)


def find_duck_config(exe=None):
    """settings.ini, in a portable folder beside the AppImage or in the
    user's data directory. Never one standing in for the other."""
    if exe:
        base = os.path.dirname(os.path.abspath(exe))
        if os.path.isfile(os.path.join(base, "portable.txt")):
            return os.path.join(base, "settings.ini")
    for d in DUCK_DATA_DIRS:
        path = os.path.join(os.path.expanduser(d), "settings.ini")
        if os.path.isfile(path):
            return path
    return os.path.join(os.path.expanduser(DUCK_DATA_DIRS[0]), "settings.ini")


def duck_pad_rows(pad, player):
    """One [PadN] section: a DualShock wired to this pad."""
    # Cross is the bottom button. Which SDL button that IS depends on how
    # Steam handed this pad over, which preflight cannot read — so the pad's
    # own swap setting decides, and both triggers change it on the screen.
    face = DUCK_FACE_MIRROR if pad.swap_faces else DUCK_FACE_IDENTITY
    buttons = dict(DUCK_BUTTON, **face)
    rows = [("Type", "AnalogController")]
    for role, key in DUCK_KEYS.items():
        if role in buttons:
            rows.append((key, f"SDL-{player}/{buttons[role]}"))
        elif role in DUCK_AXIS:
            rows.append((key, f"SDL-{player}/{DUCK_AXIS[role]}"))
        elif role in DUCK_MOTOR:
            rows.append((key, f"SDL-{player}/{DUCK_MOTOR[role]}"))
    return rows


def write_duck_config(cfg_path, pads, exe=None):
    """Write a DualShock per assigned pad, and turn the multitap on when
    there are more than two of them.

    Every [PadN] section is replaced wholesale, and any pad section we do not
    fill is emptied to Type = None: a port left bound to yesterday's pad is a
    second player nobody is holding.
    """
    assigned = sorted((p for p in pads if p.slot), key=lambda p: p.slot)
    if not assigned:
        return ["no controllers assigned"]
    if not os.path.isfile(cfg_path):
        return ["DuckStation's settings.ini was not found — run it once first."]
    sections = read_ini(cfg_path)
    if sections is None:
        return ["cannot read settings.ini"]

    rows = duck_players(exe)
    if rows is None:
        print("duckstation: could not read its own SDL; using ours", flush=True)
    numbers, multitap = duck_sections(len(assigned))

    used, ours, problems = set(), {}, []
    for pad, number in zip(assigned, numbers):
        player = duck_player_for(pad, rows, used)
        if player is None:
            problems.append(f"{pad.label}: no SDL player index for this pad")
            continue
        ours[f"Pad{number}"] = duck_pad_rows(pad, player)
        print(f"duckstation: P{pad.slot} {pad.label} -> Pad{number} "
              f"= SDL-{player}", flush=True)
    if problems:
        return problems

    # Ports we are not filling, emptied rather than left as they were.
    for number in (DUCK_PADS_MULTITAP + DUCK_PADS_PLAIN):
        name = f"Pad{number}"
        if name not in ours:
            ours[name] = [("Type", "None")]

    wanted = {"InputSources": {"SDL": "true"},
              "ControllerPorts": {"MultitapMode": multitap}}

    out, seen = [], set()
    for name, existing in sections:
        if name in ours:
            out.append((name, ours[name]))
            seen.add(name)
            continue
        if name in wanted:
            rows_out = list(existing)
            for key, value in wanted[name].items():
                for i, (k, _v) in enumerate(rows_out):
                    if k == key:
                        rows_out[i] = (k, value)
                        break
                else:
                    rows_out.append((key, value))
            out.append((name, rows_out))
            seen.add(name)
            continue
        out.append((name, existing))
    for name in wanted:
        if name not in seen:
            out.append((name, list(wanted[name].items())))
    for number in DUCK_PADS_MULTITAP + DUCK_PADS_PLAIN:
        name = f"Pad{number}"
        if name not in seen:
            out.append((name, ours[name]))
            seen.add(name)

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(cfg_path, os.path.join(BACKUP_DIR, f"settings.{stamp}.ini"))
    write_ini(cfg_path, out)
    return []


# ------------------------------------------------------------------ gopher64

GOPHER_APP_ID = "io.github.gopher64.gopher64"

# gopher64 keeps one profile per name in config.json and an array of 19 slots
# in each, in this order — src/ui/input_profile.rs's own constants. Each slot
# holds two bindings, [keyboard, controller]; only the second is ours.
GOPHER_SLOTS = ("dpad_right", "dpad_left", "dpad_down", "dpad_up", "start",
                "z", "b", "a", "c_right", "c_left", "c_down", "c_up",
                "r", "l", "stick_right", "stick_left", "stick_down",
                "stick_up", "hotkey")


def _button(sdl3_id):
    return {"ControllerButton": {"id": sdl3_id}}


def _axis(sdl3_axis, sign):
    return {"ControllerAxis": {"id": sdl3_axis, "axis": sign,
                               "initial_state": 0}}


# gopher64's own defaults, read back out of a config.json it wrote: SDL3
# numbering, so 0-3 are the face buttons, 9/10 the shoulders, 11-14 the d-pad,
# axes 0/1 the left stick, 2/3 the right, 4 the left trigger. The C buttons
# are the right stick, which is how an N64 pad's yellow cluster is played on
# anything modern, and Z is the left trigger.
GOPHER_IDENTITY = {
    "dpad_right": _button(14), "dpad_left": _button(13),
    "dpad_down": _button(12), "dpad_up": _button(11),
    # Z on the RIGHT trigger. gopher64's default has it on the left, but Z is
    # the N64's fire button and the left trigger is worth more free: Steam
    # Input can make a held trigger shift ABXY onto the C buttons, and
    # gopher64 itself has no notion of a held modifier.
    "start": _button(6), "z": _axis(5, 1),
    # B on the pad's B, not on West where gopher64's own default puts it.
    # Their choice follows the N64's shape — B sits left of A there, and West
    # is the left face button on a modern pad — but it means the button
    # marked B does nothing and X plays B, which is the one thing this tool
    # exists to prevent. Confirmed on the machine: pressing X gave B.
    "b": _button(1), "a": _button(0),
    "c_right": _axis(2, 1), "c_left": _axis(2, -1),
    "c_down": _axis(3, 1), "c_up": _axis(3, -1),
    "r": _button(10), "l": _button(9),
    "stick_right": _axis(0, 1), "stick_left": _axis(0, -1),
    "stick_down": _axis(1, 1), "stick_up": _axis(1, -1),
    "hotkey": _button(4),
}
# An N64 pad has two face buttons, so the swap is A and B trading places and
# nothing else. Same gesture, same meaning as the other maps.
GOPHER_MIRRORED = dict(GOPHER_IDENTITY, a=_button(1), b=_button(0))


def gopher_profile_name(slot):
    return f"preflight-p{slot}"


def gopher_run(args, app_id=None, exe=None, timeout=60):
    """One gopher64 CLI call, with Steam's virtual pads made visible.

    Its SDL3 is linked statically, so there is no library to borrow the way
    Dolphin's and Cemu's SDL can be borrowed: gopher64's own binary is the
    only thing that can enumerate the way gopher64 does, and --list-controllers
    and --assign-controller are exactly that. The hint has to cross a flatpak
    sandbox, hence --env rather than the environment.
    """
    key, _, value = VIRTUAL_PAD_HINT.partition("=")
    if exe:
        cmd = [exe] + list(args)
        env = dict(os.environ, **{key: value})
    else:
        cmd = ["flatpak", "run", f"--env={VIRTUAL_PAD_HINT}",
               app_id or GOPHER_APP_ID] + list(args)
        env = dict(os.environ)
    try:
        return subprocess.run(cmd, env=env, capture_output=True, text=True,
                              timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return None


def gopher_controllers(app_id=None, exe=None):
    """[name] in gopher64's own order, as --list-controllers prints it.

    Also the call that creates config.json on a fresh install: gopher64 writes
    the file whenever it has read it, so asking it anything at all leaves a
    config to edit.
    """
    out = gopher_run(["--list-controllers"], app_id, exe)
    if out is None or out.returncode != 0:
        return None
    names = []
    for line in out.stdout.splitlines():
        head, sep, name = line.partition(": ")
        if sep and head.startswith("Controller "):
            names.append(name.strip())
    return names or None


def gopher_names_for(pad):
    """Every name this pad might be listed under, best guess first."""
    wanted = [pad.real["name"] if pad.real else None,
              kernel_name(pad), pad.gc_name, pad.name]
    out = []
    for name in wanted:
        if name and name not in out:
            out.append(name)
    return out


def gopher_index_for(pad, names, used):
    """Which --list-controllers entry is this pad.

    gopher64 prints SDL3's joystick name, and for a Steam virtual pad that is
    the KERNEL's name, not SDL2's: "Microsoft X-Box 360 pad 0" where this tool
    says "Steam Virtual Gamepad" — measured on the machine, and the same trap
    Dolphin's evdev names were. So every name this pad answers to is tried,
    and identical models are matched in order.
    """
    # The physical pad's own name first. Under Steam Input every pad reaches
    # this tool as "Steam Virtual Gamepad", while gopher64 lists what the
    # hardware is called — "Google Stadia Controller", "Xbox One controller".
    # Measured with four pads paired: matching on SDL's name found none of
    # them, and the pad we had paired to its hardware was the only one that
    # matched at all.
    for want in gopher_names_for(pad):
        for i, name in enumerate(names or ()):
            if i not in used and name == want:
                used.add(i)
                return i
    return None


def gopher_path_for(pad):
    """The device path gopher64 will open for this pad, when we can know it.

    Its controller_assignment is a kernel device path, and for a Steam virtual
    pad — which is every pad there is under Steam Input — our own SDL reports
    exactly the same evdev node its SDL3 will: measured on the machine, four
    virtual pads at event20/22/24/26 matched by GUID.

    A physical pad is not so simple: SDL may hand us a /dev/hidraw path for
    one, and gopher64 may open it as evdev instead. Those go the long way
    round, through gopher64's own --assign-controller.
    """
    path = getattr(pad, "devpath", None)
    return path if path and path.startswith("/dev/input/event") else None


def find_gopher_config(app_id=None, exe=None):
    if exe:
        base = os.path.dirname(os.path.abspath(exe))
        portable = os.path.join(base, "portable_data", "config.json")
        if os.path.isfile(portable):
            return portable
        return os.path.expanduser("~/.config/gopher64/config.json")
    return os.path.expanduser(
        f"~/.var/app/{app_id or GOPHER_APP_ID}/config/gopher64/config.json")


def gopher_entry(pad, template):
    """One input profile for this pad: gopher64's own keyboard half kept,
    the controller half written from scratch."""
    table = GOPHER_MIRRORED if pad.swap_faces else GOPHER_IDENTITY
    rows = []
    for i, role in enumerate(GOPHER_SLOTS):
        pair = None
        if template and i < len(template):
            pair = template[i]
        keyboard = pair[0] if isinstance(pair, list) and pair else None
        rows.append([keyboard, copy.deepcopy(table[role])])
    return {"inputs": rows, "dinput": False, "deadzone": 5}


def write_gopher_config(cfg_path, pads, app_id=None, exe=None):
    """Write a profile per assigned pad, bind it to that pad's port, enable
    the port, and let gopher64 itself record which device it is.

    The device is recorded by gopher64 rather than by us on purpose: its
    controller_assignment is a kernel device path, and only gopher64's own
    statically-linked SDL3 can say which path it will open for a given pad.
    Its --assign-controller does that and rewrites the file, keeping
    everything written here.
    """
    assigned = sorted((p for p in pads if p.slot), key=lambda p: p.slot)
    if not assigned:
        return ["no controllers assigned"]

    names = gopher_controllers(app_id, exe)
    if names is None:
        return ["gopher64 would not list its controllers — cannot write."]
    print(f"gopher64 sees: {', '.join(names) or 'nothing'}", flush=True)

    data = load_json(cfg_path, None)
    if data is None:
        return ["cannot read gopher64's config.json"]
    inp = data.setdefault("input", {})
    profiles = inp.setdefault("input_profiles", {})
    template = (profiles.get("default") or {}).get("inputs")

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(cfg_path, os.path.join(BACKUP_DIR, f"gopher64.{stamp}.json"))

    binding = inp.get("input_profile_binding") or ["default"] * 4
    enabled = inp.get("controller_enabled") or [True, False, False, False]
    binding = (list(binding) + ["default"] * 4)[:4]
    enabled = (list(enabled) + [False] * 4)[:4]

    used, ports, problems, skipped = set(), [], [], []
    unmatched, direct = [], []
    for pad in assigned:
        name = gopher_profile_name(pad.slot)
        profiles[name] = gopher_entry(pad, template)
        binding[pad.slot - 1] = name
        # Only ports we are filling: a port left enabled with nothing in it
        # is a controller the game waits for and nobody holds.
        enabled[pad.slot - 1] = True
        # The path we already know beats an index into a listing that is
        # re-made by every process that reads it: gopher64's enumeration
        # order changed between two runs a minute apart, which is how port 4
        # ended up with no device at all.
        path = gopher_path_for(pad)
        if path:
            direct.append((pad, path))
            print(f"gopher64: P{pad.slot} {pad.label} -> {path}", flush=True)
            continue
        index = gopher_index_for(pad, names, used)
        if index is None:
            # Not fatal unless it is P1. Four pads on the sofa and one of
            # them unmatched used to refuse the launch outright, which left
            # nobody playing rather than three people playing.
            if pad.slot == 1:
                problems.append(
                    f"{pad.label}: gopher64 does not see P1's pad. It lists: "
                    f"{', '.join(names) or 'nothing'}")
            else:
                print(f"gopher64: no match for P{pad.slot} {pad.label} "
                      f"(tried {', '.join(gopher_names_for(pad))})", flush=True)
                unmatched.append(pad)
            continue
        ports.append((pad, index))
    # Whatever is left, by elimination. A pad only reveals what hardware it
    # is once someone presses a button on it, so a pad nobody touched on the
    # check screen has no name to match — but if the pads without a name and
    # the entries without a pad come to the same number, there is only one
    # way round they can go. "None" is gopher64's word for a device it could
    # not name, and is never a pad.
    spare = [i for i, name in enumerate(names)
             if i not in used and name != "None"]
    if unmatched and len(unmatched) == len(spare):
        for pad, index in zip(unmatched, spare):
            used.add(index)
            ports.append((pad, index))
            print(f"gopher64: P{pad.slot} {pad.label} -> controller "
                  f"{index + 1} ({names[index]}) by elimination", flush=True)
        unmatched = []
    skipped.extend(unmatched)

    for slot in range(1, 5):
        if (not any(p.slot == slot for p in assigned)
                or any(p.slot == slot for p in skipped)):
            # A port with no device behind it is a controller the game waits
            # for and nobody holds.
            enabled[slot - 1] = False

    if problems:
        return problems
    inp["input_profile_binding"] = binding
    inp["controller_enabled"] = enabled
    assignment = (list(inp.get("controller_assignment") or [])
                  + [None] * 4)[:4]
    for pad, path in direct:
        assignment[pad.slot - 1] = path
    for pad in skipped:
        assignment[pad.slot - 1] = None
    inp["controller_assignment"] = assignment
    tmp = cfg_path + ".preflight.tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, cfg_path)

    for pad, index in ports:
        out = gopher_run(["--assign-controller", str(index + 1),
                          "--port", str(pad.slot)], app_id, exe)
        if out is None or out.returncode != 0:
            return [f"gopher64 refused to take P{pad.slot}'s controller "
                    f"assignment ({names[index]})."]
        print(f"gopher64: P{pad.slot} {pad.label} -> controller {index + 1} "
              f"({names[index]})", flush=True)

    # gopher64 rewrote the file to record the devices; make sure what it kept
    # is still what we asked for, rather than trusting two writers blindly.
    fresh = load_json(cfg_path, None) or {}
    got = (fresh.get("input") or {})
    if got.get("input_profile_binding") != binding:
        return ["gopher64 did not keep the profiles preflight wrote."]
    # A port gopher64 would not record is disabled rather than fatal, unless
    # it is P1's. Four pads and one missing device used to mean nobody played.
    final = (got.get("controller_assignment") or [None] * 4)
    missing = [p.slot for p, _ in ports if not final[p.slot - 1]]
    if 1 in missing:
        return ["gopher64 recorded no device for P1's controller."]
    if missing:
        print(f"gopher64: no device recorded for port(s) "
              f"{', '.join(str(s) for s in missing)}; disabling them",
              flush=True)
        for slot in missing:
            enabled[slot - 1] = False
        fresh["input"]["controller_enabled"] = enabled
        tmp = cfg_path + ".preflight.tmp"
        with open(tmp, "w") as fh:
            json.dump(fresh, fh, indent=2)
        os.replace(tmp, cfg_path)
    return []


# --------------------------------------------------------------------- cemu

CEMU_APP_ID = "info.cemu.Cemu"

# Everything below is read out of Cemu's own source at tag v2.6 (src/input/),
# not out of a saved profile: InputManager::load/save for the file,
# VPADController.h and ProController.h for the mapping ids, Controller.h's
# Buttons2 for the button codes, and VPADController::set_default_mapping for
# which SDL code each control takes.
#
# Player 1 is the GamePad and everyone else a Pro Controller, because that is
# the Wii U: one GamePad, and multiplayer games take Pro Controllers for the
# rest. The two number their controls differently — the Pro Controller has
# Home between Minus and the d-pad — so each gets its own table.
CEMU_VPAD = {"a": 1, "b": 2, "x": 3, "y": 4, "l": 5, "r": 6, "zl": 7, "zr": 8,
             "plus": 9, "minus": 10, "up": 11, "down": 12, "left": 13,
             "right": 14, "stick_l": 15, "stick_r": 16,
             "l_up": 17, "l_down": 18, "l_left": 19, "l_right": 20,
             "r_up": 21, "r_down": 22, "r_left": 23, "r_right": 24}
CEMU_PRO = {"a": 1, "b": 2, "x": 3, "y": 4, "l": 5, "r": 6, "zl": 7, "zr": 8,
            "plus": 9, "minus": 10, "up": 12, "down": 13, "left": 14,
            "right": 15, "stick_l": 16, "stick_r": 17,
            "l_up": 18, "l_down": 19, "l_left": 20, "l_right": 21,
            "r_up": 22, "r_down": 23, "r_left": 24, "r_right": 25}

# Buttons2: 0-31 are SDL's own button numbers, then the axes. Triggers are
# the positive half of the trigger pair, sticks both halves of axis (left)
# and rotation (right), negative Y being up.
CEMU_SDL = {"l": 9, "r": 10, "zl": 42, "zr": 43, "plus": 6, "minus": 4,
            "up": 11, "down": 12, "left": 13, "right": 14,
            "stick_l": 7, "stick_r": 8,
            "l_up": 45, "l_down": 39, "l_left": 44, "l_right": 38,
            "r_up": 47, "r_down": 41, "r_left": 46, "r_right": 40}
# Same sense as FACE_IDENTITY: identity is what-you-see-is-what-you-get, SDL's
# A for the Wii U's A — Cemu's own default for a Switch Pro Controller.
# Mirrored is Cemu's default for every other pad, which goes by position.
CEMU_FACE_IDENTITY = {"a": 0, "b": 1, "x": 2, "y": 3}
CEMU_FACE_MIRRORED = {"a": 1, "b": 0, "x": 3, "y": 2}

# Cemu counts only devices SDL recognises as game controllers when it numbers
# pads that share a GUID, so this does too; a stray joystick would otherwise
# shift every ordinal after it.
CEMU_ENUM = r"""
import ctypes, sys
sdl = ctypes.CDLL(sys.argv[1])
class G(ctypes.Structure):
    _fields_ = [("d", ctypes.c_uint8 * 16)]
sdl.SDL_SetHint(b"SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD", b"1")
sdl.SDL_Init(0x00000200 | 0x00002000)
sdl.SDL_JoystickGetDeviceGUID.restype = G
sdl.SDL_JoystickGetDeviceGUID.argtypes = [ctypes.c_int]
sdl.SDL_JoystickGetGUIDString.argtypes = [G, ctypes.c_char_p, ctypes.c_int]
sdl.SDL_GameControllerNameForIndex.restype = ctypes.c_char_p
seen = {}
for i in range(sdl.SDL_NumJoysticks()):
    if not sdl.SDL_IsGameController(i):
        continue
    b = ctypes.create_string_buffer(33)
    sdl.SDL_JoystickGetGUIDString(sdl.SDL_JoystickGetDeviceGUID(i), b, 33)
    guid = b.value.decode()
    n = seen.get(guid, 0)
    seen[guid] = n + 1
    name = sdl.SDL_GameControllerNameForIndex(i) or b"?"
    print(n, guid, name.decode(errors="replace"), sep="\t")
sdl.SDL_Quit()
"""


def find_cemu_sdl(app_id=None):
    """The libSDL2 Cemu's flatpak links against: the runtime its own metadata
    names, in whichever installation holds it. Not simply the newest one
    installed — 26.08 sat beside Cemu's 25.08 on the machine, and a newer
    SDL is exactly how ids stop matching (§3)."""
    import glob
    roots = (os.path.expanduser("~/.local/share/flatpak"), "/var/lib/flatpak")
    runtime = None
    for root in roots:
        try:
            with open(f"{root}/app/{app_id or CEMU_APP_ID}/current/active/metadata") as fh:
                for line in fh:
                    if line.startswith("runtime="):
                        runtime = line.split("=", 1)[1].strip()
                        break
        except OSError:
            continue
        if runtime:
            break
    if not runtime:
        return None
    for root in roots:
        hits = sorted(glob.glob(f"{root}/runtime/{runtime}/active/files/lib/*/libSDL2-2.0.so.0"))
        if hits:
            return hits[0]
    return None


def cemu_devices():
    """[(ordinal, guid, name)] as Cemu numbers them."""
    lib = find_cemu_sdl() or "libSDL2-2.0.so.0"
    env = dict(os.environ)
    env.pop("SDL_GAMECONTROLLER_IGNORE_DEVICES", None)
    try:
        out = subprocess.run([sys.executable, "-c", CEMU_ENUM, lib], env=env,
                             capture_output=True, text=True, timeout=20)
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    rows = []
    for line in out.stdout.splitlines():
        bits = line.split("\t")
        if len(bits) == 3 and bits[0].isdigit():
            rows.append((int(bits[0]), bits[1], bits[2]))
    return rows or None


def cemu_uuid(pad, rows, used):
    """Cemu's name for a pad: which of the pads sharing its GUID it is, then
    the GUID — "0_0300...". Matched on the GUID Cemu's own SDL reports, and
    on vendor/product with the name-CRC when the two SDLs disagree, as they
    do across versions (§3)."""
    for exact in (True, False):
        for n, guid, _name in rows or ():
            key = f"{n}_{guid}"
            if key in used:
                continue
            if exact and guid != pad.sdl_guid:
                continue
            if not exact and (guid_vendor_product(guid) != (pad.vendor, pad.product)
                              or guid_name_crc(guid) != pad.name_crc):
                continue
            used.add(key)
            return key
    return f"0_{pad.sdl_guid}"


def cemu_profile(pad, uuid, gamepad):
    """One controllerN.xml, laid out the way Cemu's own save() writes it."""
    ids = CEMU_VPAD if gamepad else CEMU_PRO
    kind = "Wii U GamePad" if gamepad else "Wii U Pro Controller"
    codes = dict(CEMU_SDL, **(CEMU_FACE_MIRRORED if pad.swap_faces
                             else CEMU_FACE_IDENTITY))
    entries = "".join(
        f"\t\t\t<entry>\n\t\t\t\t<mapping>{ids[k]}</mapping>\n"
        f"\t\t\t\t<button>{codes[k]}</button>\n\t\t\t</entry>\n"
        for k in sorted(ids, key=ids.get))
    name = xml_escape(pad.label)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            "<emulated_controller>\n"
            f"\t<type>{kind}</type>\n"
            "\t<controller>\n"
            "\t\t<api>SDLController</api>\n"
            f"\t\t<uuid>{uuid}</uuid>\n"
            f"\t\t<display_name>{name}</display_name>\n"
            # Rumble at full strength. Cemu's own default is 0 — off — and
            # writing nothing left it there, so a pad this tool had just
            # buzzed on the check screen sat silent in the game. It is a
            # strength, not a flag: 1 is as hard as the pad goes.
            "\t\t<rumble>1</rumble>\n"
            "\t\t<axis>\n\t\t\t<deadzone>0.25</deadzone>\n\t\t\t<range>1</range>\n\t\t</axis>\n"
            "\t\t<rotation>\n\t\t\t<deadzone>0.25</deadzone>\n\t\t\t<range>1</range>\n\t\t</rotation>\n"
            "\t\t<trigger>\n\t\t\t<deadzone>0.25</deadzone>\n\t\t\t<range>1</range>\n\t\t</trigger>\n"
            "\t\t<mappings>\n" + entries + "\t\t</mappings>\n"
            "\t</controller>\n"
            "</emulated_controller>\n")


def xml_escape(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def cemu_config_target(app_id=None, exe=None):
    """Cemu's controllerProfiles folder. Portable beside the binary, else its
    flatpak sandbox, else ~/.config — and never one standing in for another,
    for the reason find_config gives."""
    if exe:
        portable = os.path.join(os.path.dirname(os.path.abspath(exe)), "portable")
        if os.path.isdir(portable):
            return os.path.join(portable, "controllerProfiles")
        return os.path.expanduser("~/.config/Cemu/controllerProfiles")
    return os.path.expanduser(
        f"~/.var/app/{app_id or CEMU_APP_ID}/config/Cemu/controllerProfiles")


CEMU_SLOTS = 8      # controller0 .. controller7


# Which games play with P1 as a Pro Controller, by ROM file name. The GamePad
# is the default because many Wii U games will not start without one; but a
# game that offers both — Wind Waker HD — puts its map and items on the
# GamePad's own screen, which does not exist in Game Mode. With a Pro
# Controller the same things are on the TV. Only the game can say which it
# is, so it is set per game on the check screen with + and - together.
CEMU_P1_PRO = os.path.join(STATE_DIR, "cemu-p1-pro.json")


def cemu_p1_pro(rom):
    return bool(rom) and bool(load_json(CEMU_P1_PRO, {}).get(os.path.basename(rom)))


def set_cemu_p1_pro(rom, pro):
    if not rom:
        return
    games = load_json(CEMU_P1_PRO, {})
    if pro:
        games[os.path.basename(rom)] = True
    else:
        games.pop(os.path.basename(rom), None)
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = CEMU_P1_PRO + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(games, fh, indent=2)
    os.replace(tmp, CEMU_P1_PRO)


def write_cemu_config(cfg_dir, pads, p1_pro=False):
    """controller0.xml for P1, controller1-3 for the rest.

    P1 is the GamePad unless this game has been switched to a Pro Controller
    (see CEMU_P1_PRO); everyone else is always a Pro Controller.

    Any other controllerN.xml that names one of the same pads is moved to the
    backups: yesterday's P2 left in place would drive a second player with
    today's P1.
    """
    assigned = sorted((p for p in pads if p.slot), key=lambda p: p.slot)
    if not assigned:
        return ["no controllers assigned"]
    try:
        os.makedirs(cfg_dir, exist_ok=True)
    except OSError:
        return [f"cannot create {cfg_dir}"]
    rows = cemu_devices()
    if rows is None:
        print("cemu: could not enumerate through Cemu's SDL; using ours",
              flush=True)
    used, ours = set(), {}
    for pad in assigned:
        uuid = cemu_uuid(pad, rows, used)
        gamepad = pad.slot == 1 and not p1_pro
        ours[f"controller{pad.slot - 1}.xml"] = cemu_profile(pad, uuid, gamepad)
        print(f"cemu: P{pad.slot} {pad.label} -> {uuid}", flush=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    for n in range(CEMU_SLOTS):
        name = f"controller{n}.xml"
        path = os.path.join(cfg_dir, name)
        if not os.path.isfile(path):
            continue
        backup = os.path.join(BACKUP_DIR, f"cemu-{stamp}-{name}")
        if name in ours:
            shutil.copy2(path, backup)
            continue
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if any(f"<uuid>{u}</uuid>" in text for u in used):
            shutil.move(path, backup)
            print(f"cemu: moved stale {name} to backups", flush=True)

    for name, text in ours.items():
        path = os.path.join(cfg_dir, name)
        tmp = path + ".preflight.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    return []


# ------------------------------------------------------------------ dolphin

DOLPHIN_APP_ID = "org.DolphinEmu.dolphin-emu"

# One GameCube pad, in the SDL backend's vocabulary. Copied from what Dolphin
# itself writes rather than invented — the names are its own ("Button S" is the
# south face button, "Pad N" the d-pad's north). Face buttons are filled in
# from the mapping so the A/B swap works the same way it does for Ryujinx.
DOLPHIN_GC_TEMPLATE = [
    ("Buttons/A", "`Button {a}`"),
    ("Buttons/B", "`Button {b}`"),
    ("Buttons/X", "`Button {x}`"),
    ("Buttons/Y", "`Button {y}`"),
    # Either shoulder, because a GameCube pad has one Z and a modern pad has
    # two of them: whichever hand reaches for it is the right one.
    ("Buttons/Z", "`Shoulder L` | `Shoulder R`"),
    ("Buttons/Start", "Start"),
    ("D-Pad/Up", "`Pad N`"),
    ("D-Pad/Down", "`Pad S`"),
    ("D-Pad/Left", "`Pad W`"),
    ("D-Pad/Right", "`Pad E`"),
    ("Main Stick/Up", "`Left Y+`"),
    ("Main Stick/Down", "`Left Y-`"),
    ("Main Stick/Left", "`Left X-`"),
    ("Main Stick/Right", "`Left X+`"),
    ("Main Stick/Calibration",
     "100.00 141.42 100.00 141.42 100.00 141.42 100.00 141.42"),
    ("C-Stick/Up", "`Right Y+`"),
    ("C-Stick/Down", "`Right Y-`"),
    ("C-Stick/Left", "`Right X-`"),
    ("C-Stick/Right", "`Right X+`"),
    ("C-Stick/Calibration",
     "100.00 141.42 100.00 141.42 100.00 141.42 100.00 141.42"),
    ("Triggers/L", "`Trigger L`"),
    ("Triggers/R", "`Trigger R`"),
    ("Triggers/L-Analog", "`Trigger L`"),
    ("Triggers/R-Analog", "`Trigger R`"),
    ("Rumble/Motor", "Motor"),
]

# Dolphin names face buttons by position: S is the bottom one. Identity is
# Dolphin's own default; mirrored swaps the pairs, exactly as FACE_MIRRORED
# does for Ryujinx.
DOLPHIN_FACE_IDENTITY = {"a": "S", "b": "E", "x": "W", "y": "N"}
DOLPHIN_FACE_MIRRORED = {"a": "E", "b": "S", "x": "N", "y": "W"}

# The same pad through Dolphin's evdev backend, which is what we actually
# write. Its vocabulary is entirely different from the SDL backend's, and the
# axis numbers are positions among the device's absolute axes rather than
# kernel codes — hence the d-pad on 6 and 7. Copied verbatim in shape from a
# working hand-made entry rather than derived, and confirmed against the
# device's own capabilities: SOUTH EAST NORTH WEST TL TR SELECT START MODE
# THUMBL THUMBR, axes X Y Z RX RY RZ HAT0X HAT0Y.
DOLPHIN_GC_EVDEV_TEMPLATE = [
    ("Buttons/A", "{A}"),
    ("Buttons/B", "{B}"),
    ("Buttons/X", "{X}"),
    ("Buttons/Y", "{Y}"),
    # Either shoulder. Dolphin's `|` parses fine here — confirmed in game
    # 2026-09-16, L gives Z on any pad preflight can bind.
    ("Buttons/Z", "TL | TR"),
    ("Buttons/Start", "START"),
    ("D-Pad/Up", "`Axis {hy}-`"),
    ("D-Pad/Down", "`Axis {hy}+`"),
    ("D-Pad/Left", "`Axis {hx}-`"),
    ("D-Pad/Right", "`Axis {hx}+`"),
    ("Main Stick/Up", "`Axis {ly}-`"),
    ("Main Stick/Down", "`Axis {ly}+`"),
    ("Main Stick/Left", "`Axis {lx}-`"),
    ("Main Stick/Right", "`Axis {lx}+`"),
    ("C-Stick/Up", "`Axis {ry}-`"),
    ("C-Stick/Down", "`Axis {ry}+`"),
    ("C-Stick/Left", "`Axis {rx}-`"),
    ("C-Stick/Right", "`Axis {rx}+`"),
    ("Triggers/L", "`Full Axis {lt}+`"),
    ("Triggers/R", "`Full Axis {rt}+`"),
    ("Triggers/L-Analog", "`Full Axis {lt}+`"),
    ("Triggers/R-Analog", "`Full Axis {rt}+`"),
    ("Rumble/Motor", "Strong"),
    ("Options/Always Connected", "True"),
]

DOLPHIN_EVDEV_IDENTITY = {"A": "SOUTH", "B": "EAST", "X": "WEST", "Y": "NORTH"}

# Dolphin's evdev axis numbers are POSITIONS among the axes a device reports,
# not kernel ABS codes — so they shift from pad to pad and cannot be hardcoded.
# A Bluetooth Xbox pad puts its right stick on Z/RZ and its triggers on
# BRAKE/GAS, where Steam's virtual pad uses RX/RY and Z/RZ. Hardcoding the
# latter bound the left trigger to the right stick's X axis.
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0, 1, 2, 3, 4, 5
ABS_BRAKE, ABS_GAS = 9, 10
ABS_HAT0X, ABS_HAT0Y = 16, 17

DEFAULT_AXES = {"lx": 0, "ly": 1, "rx": 3, "ry": 4, "lt": 2, "rt": 5,
                "hx": 6, "hy": 7}


def evdev_abs_codes(pad):
    """The ABS_* codes a pad reports, in order, from its sysfs bitmap."""
    for directory in sysfs_input_dirs(pad):
        try:
            with open(os.path.join(directory, "capabilities", "abs")) as fh:
                words = fh.read().split()
        except OSError:
            continue
        mask = 0
        for i, word in enumerate(reversed(words)):
            try:
                mask |= int(word, 16) << (64 * i)
            except ValueError:
                mask = 0
                break
        codes = [bit for bit in range(64) if mask >> bit & 1]
        if codes:
            return codes
    return []


def evdev_axis_map(pad):
    """Which Dolphin axis number carries each stick and trigger on this pad."""
    codes = evdev_abs_codes(pad)
    if not codes:
        return dict(DEFAULT_AXES)
    pos = {code: i for i, code in enumerate(codes)}
    axes = dict(DEFAULT_AXES)

    def put(key, *preferred):
        for code in preferred:
            if code in pos:
                axes[key] = pos[code]
                return

    put("lx", ABS_X)
    put("ly", ABS_Y)
    # Right stick: RX/RY normally, Z/RZ on Bluetooth Xbox pads.
    if ABS_RX in pos and ABS_RY in pos:
        put("rx", ABS_RX)
        put("ry", ABS_RY)
    else:
        put("rx", ABS_Z)
        put("ry", ABS_RZ)
    # Triggers: BRAKE/GAS when present, otherwise Z/RZ — but never the pair
    # already spent on the right stick.
    if ABS_BRAKE in pos and ABS_GAS in pos:
        put("lt", ABS_BRAKE)
        put("rt", ABS_GAS)
    else:
        put("lt", ABS_Z)
        put("rt", ABS_RZ)
    put("hx", ABS_HAT0X)
    put("hy", ABS_HAT0Y)
    return axes
DOLPHIN_EVDEV_MIRRORED = {"A": "EAST", "B": "SOUTH", "X": "NORTH", "Y": "WEST"}


def sysfs_input_dirs(pad):
    """The sysfs input directories behind a pad's device node.

    SDL hands back /dev/input/eventN for some pads and /dev/hidrawN for
    others — Bluetooth Xbox pads go through HIDAPI and report hidraw — so
    anything reading the kernel's view has to handle both. Getting this wrong
    is silent: the lookup returns nothing and the caller quietly falls back.
    """
    import glob as _g
    path = getattr(pad, "devpath", None) or ""
    base = os.path.basename(path)
    if path.startswith("/dev/input/event"):
        return [f"/sys/class/input/{base}/device"]
    if path.startswith("/dev/hidraw"):
        return sorted(_g.glob(f"/sys/class/hidraw/{base}/device/input/input*"))
    return []


def kernel_name(pad):
    """The name the kernel gives this pad, which is what Dolphin's evdev
    backend calls it. SDL's name is not it: SDL says "Steam Virtual Gamepad"
    where the kernel says "Microsoft X-Box 360 pad 0"."""
    for node in [os.path.join(d, "name") for d in sysfs_input_dirs(pad)]:
        try:
            got = open(node).read().strip()
        except OSError:
            continue
        if got:
            return got
    return pad.name


# Enumerate the way Dolphin does: its own SDL, Steam's ignore list cleared
# (that variable is set for processes Steam launches, and does not cross the
# flatpak sandbox), and the virtual-gamepad hint deliberately NOT set, so we
# see the physical pads Dolphin sees by default.
DOLPHIN_ENUM = r"""
import ctypes, os, sys
sdl = ctypes.CDLL(sys.argv[1])
sdl.SDL_Init(0x00000200 | 0x00002000)
for f in ("SDL_JoystickNameForIndex", "SDL_GameControllerNameForIndex",
          "SDL_JoystickPathForIndex"):
    if hasattr(sdl, f):
        getattr(sdl, f).restype = ctypes.c_char_p


def mac_of(path):
    # A hidraw node keeps no uniq of its own; the address lives on the input
    # device hanging off it. An evdev node has it one level up.
    base = os.path.basename(path)
    if path.startswith("/dev/hidraw"):
        import glob as _g
        nodes = _g.glob("/sys/class/hidraw/%s/device/input/input*/uniq" % base)
    elif path.startswith("/dev/input/event"):
        nodes = ["/sys/class/input/%s/device/uniq" % base]
    else:
        return ""
    for node in nodes:
        try:
            got = open(node).read().strip().lower()
        except OSError:
            continue
        if got:
            return got
    return ""


seen = {}
for i in range(sdl.SDL_NumJoysticks()):
    gc = sdl.SDL_GameControllerNameForIndex(i)
    name = (gc or sdl.SDL_JoystickNameForIndex(i) or b"?").decode(errors="replace")
    path = b""
    if hasattr(sdl, "SDL_JoystickPathForIndex"):
        path = sdl.SDL_JoystickPathForIndex(i) or b""
    n = seen.get(name, 0)
    seen[name] = n + 1
    print(n, name, mac_of(path.decode(errors="replace")), sep="\t")
sdl.SDL_Quit()
"""


def find_dolphin_sdl():
    """The libSDL2 Dolphin's flatpak links against — the KDE runtime's."""
    import glob
    for pattern in (
            "/var/lib/flatpak/runtime/org.kde.Platform/*/*/*/files/lib/*/libSDL2-2.0.so.0",
            "/var/lib/flatpak/runtime/org.freedesktop.Platform/*/*/*/files/lib/*/libSDL2-2.0.so.0"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    return None


def dolphin_real_devices():
    """[(index, name, mac)] as Dolphin will see them, physical pads included."""
    lib = find_dolphin_sdl()
    if not lib:
        return None
    env = {k: v for k, v in os.environ.items()
           if k not in ("SDL_GAMECONTROLLER_IGNORE_DEVICES",
                        "SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT",
                        "SDL_JOYSTICK_HIDAPI_STEAM",
                        "SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD")}
    try:
        out = subprocess.run([sys.executable, "-c", DOLPHIN_ENUM, lib], env=env,
                             capture_output=True, text=True, timeout=20)
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    rows = []
    for line in out.stdout.splitlines():
        bits = line.split("\t")
        if len(bits) == 3 and bits[0].isdigit():
            rows.append((int(bits[0]), bits[1], bits[2]))
    return rows or None


def find_dolphin_config(app_id=None, exe=None):
    """Dolphin's config directory — the one holding GCPadNew.ini."""
    candidates = []
    if exe:
        base = os.path.dirname(os.path.abspath(exe))
        candidates.append(os.path.join(base, "User", "Config"))
    elif app_id == DOLPHIN_APP_ID:
        candidates.append(os.path.expanduser(
            f"~/.var/app/{app_id}/config/dolphin-emu"))
    candidates.append(os.path.expanduser(
        f"~/.var/app/{DOLPHIN_APP_ID}/config/dolphin-emu"))
    candidates.append(os.path.expanduser("~/.config/dolphin-emu"))
    for path in candidates:
        if os.path.isdir(path):
            return path
    return None


def dolphin_config_target(app_id=None, exe=None):
    """Where Dolphin's config folder would be if it does not exist yet."""
    if exe:
        return os.path.join(os.path.dirname(os.path.abspath(exe)),
                            "User", "Config")
    return os.path.expanduser(
        f"~/.var/app/{app_id or DOLPHIN_APP_ID}/config/dolphin-emu")


def dolphin_device_names(pads, rows=None):
    """`evdev/<n>/<kernel name>` per pad.

    Deliberately evdev rather than SDL. Dolphin opens both, but its SDL
    backend's device names are ambiguous under Steam Input — SDL calls every
    virtual pad "Steam Virtual Gamepad" while the kernel gives each a distinct
    name — and SDL 2.32 hides them from processes Steam did not launch, which
    took three separate workarounds and still did not drive a game. evdev has
    neither problem: unique names, no hint, no visibility rules.
    """
    seen = {}
    out = {}
    for pad in sorted(pads, key=lambda p: p.index):
        name = kernel_name(pad)
        n = seen.get(name, 0)
        seen[name] = n + 1
        out[pad.key] = f"evdev/{n}/{name}"
    return out


def read_ini(path):
    """Sections in file order, each a list of (key, value). Deliberately not
    configparser: Dolphin's values contain backticks, '&' and repeated
    spacing that a round trip through configparser would quietly reformat."""
    sections = []
    current = None
    try:
        with open(path) as fh:
            for line in fh:
                line = line.rstrip("\n")
                if line.startswith("[") and line.endswith("]"):
                    current = (line[1:-1], [])
                    sections.append(current)
                elif current is not None and "=" in line:
                    k, _, v = line.partition("=")
                    current[1].append((k.strip(), v.strip()))
    except OSError:
        return None
    return sections


def write_ini(path, sections):
    tmp = path + ".preflight.tmp"
    with open(tmp, "w") as fh:
        for name, rows in sections:
            fh.write(f"[{name}]\n")
            for k, v in rows:
                fh.write(f"{k} = {v}\n")
    os.replace(tmp, path)


def write_dolphin_config(cfg_dir, pads):
    """Write GCPadNew.ini for the assigned pads, and make sure the GameCube
    ports they land in are actually enabled in Dolphin.ini."""
    if not cfg_dir:
        return ["Dolphin's config folder was not found."]
    try:
        os.makedirs(cfg_dir, exist_ok=True)
    except OSError:
        return [f"cannot create {cfg_dir}"]
    assigned = [p for p in pads if p.slot]
    if not assigned:
        return ["no controllers assigned"]

    gc_path = os.path.join(cfg_dir, "GCPadNew.ini")
    sections = read_ini(gc_path) if os.path.isfile(gc_path) else []
    if sections is None:
        return ["cannot read GCPadNew.ini"]

    devices = dolphin_device_names(pads)
    ours = {}
    for pad in assigned:
        face = (DOLPHIN_EVDEV_MIRRORED if pad.swap_faces
                else DOLPHIN_EVDEV_IDENTITY)
        rows = [("Device", devices[pad.key])]
        fields = dict(face, **evdev_axis_map(pad))
        rows += [(k, v.format(**fields)) for k, v in DOLPHIN_GC_EVDEV_TEMPLATE]
        ours[f"GCPad{pad.slot}"] = rows

    # Keep any section we do not own (GBA pads, keyboard entries) untouched,
    # and replace ours in place so the file's order survives.
    out, seen = [], set()
    for name, rows in sections:
        if name in ours:
            out.append((name, ours[name]))
            seen.add(name)
        else:
            out.append((name, rows))
    for name in sorted(ours):
        if name not in seen:
            out.append((name, ours[name]))

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if os.path.isfile(gc_path):
        shutil.copy2(gc_path, os.path.join(BACKUP_DIR, f"GCPadNew.{stamp}.ini"))
    write_ini(gc_path, out)

    problems = update_dolphin_ini(cfg_dir, [p.slot for p in assigned], stamp)
    return problems


def update_dolphin_ini(cfg_dir, slots, stamp):
    """Two fixups in Dolphin.ini, both of which are invisible when missing.

    SIDevice<n> = 6 says 'a standard controller is plugged into this port';
    without it the mappings exist and the port stays empty.

    The SDL hint is subtler. Dolphin's SDL is 2.32, which hides Steam's
    virtual gamepads from any process Steam did not launch — and Dolphin runs
    inside a flatpak, so Steam's environment never reaches it. Under Steam
    Input the virtual pads are the only pads there are, so Dolphin sees
    nothing at all and the game has no controller. Dolphin applies whatever
    is in [SDL_Hints] at startup, which is the way in.
    """
    path = os.path.join(cfg_dir, "Dolphin.ini")
    sections = read_ini(path) if os.path.isfile(path) else []
    if sections is None:
        return ["cannot read Dolphin.ini"]

    wanted = {
        "Core": {f"SIDevice{s - 1}": "6" for s in slots},
        "SDL_Hints": {"SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD": "1"},
    }

    changed = False
    for section, values in wanted.items():
        rows = None
        for name, r in sections:
            if name == section:
                rows = r
                break
        if rows is None:
            rows = []
            sections.append((section, rows))
        for key, value in values.items():
            for i, (k, v) in enumerate(rows):
                if k == key:
                    if v != value:
                        rows[i] = (k, value)
                        changed = True
                    break
            else:
                rows.append((key, value))
                changed = True

    if changed:
        if os.path.isfile(path):
            shutil.copy2(path, os.path.join(BACKUP_DIR, f"Dolphin.{stamp}.ini"))
        write_ini(path, sections)
    return []


# ----------------------------------------------------------------- screens

def swap_icon(ui, cx, cy, size, color):
    """Two arrows trading places — shown on a pad whose A/B and X/Y are
    mirrored, so a non-default mapping is never invisible."""
    span, th, head = size * 0.62, size * 0.17, size * 0.20
    for sign in (-1, 1):                       # -1 top row, +1 bottom row
        y = cy + sign * size * 0.26
        if sign < 0:                           # top arrow points right
            ui.rect(cx - span / 2, y - th / 2, span * 0.72, th, color)
            tip = cx + span / 2
            ui.fill_triangle((tip - head, y - head * 0.8),
                             (tip - head, y + head * 0.8), (tip, y), color)
        else:                                  # bottom arrow points left
            ui.rect(cx - span / 2 + span * 0.28, y - th / 2, span * 0.72, th,
                    color)
            tip = cx - span / 2
            ui.fill_triangle((tip + head, y - head * 0.8),
                             (tip + head, y + head * 0.8), (tip, y), color)


def hold_ring(ui, cx, cy, radius, fraction, color, track):
    """Progress shown as a ring of dots, since SDL cannot draw an arc.
    Reads clearly at TV distance and needs no extra primitives."""
    import math
    dots = 14
    filled = int(fraction * dots + 0.001)
    for i in range(dots):
        ang = -math.pi / 2 + (2 * math.pi * i / dots)
        ui.fill_circle(cx + math.cos(ang) * radius, cy + math.sin(ang) * radius,
                       max(1.5, radius * 0.14), color if i < filled else track)


# A wide strip, because the bays are much wider than they are tall. 3.2 and
# not 2.4: at 2.4 the strip came out 727px inside a 1042px bay, so the lanes
# were shoulder to shoulder with 8px of air while a third of the bay went
# unused — and the GameCube cluster, which is the widest thing drawn, had
# nowhere to grow into.
PAD_ASPECT = 3.2
PAD_ASPECT_N64 = 2.3


class Bay:
    """The strip one pad is drawn in, and the vocabulary for drawing in it.

    Both layouts want the same conversions, so they are worked out once here
    rather than twice over. Sizes are fractions of the strip's HEIGHT, not its
    width — height is what the bay actually limits, so this keeps the controls
    a sane size at any aspect — while positions across are fractions of width.

    `art` names the set to draw from: "" is the Switch set at the top of
    art/, "gc/" the GameCube one.
    """

    def __init__(self, ui, ox, oy, dw, dh, col, bg, dim, art=""):
        self.ui, self.bg, self.art = ui, bg, art
        self.ox, self.oy, self.dw, self.dh = ox, oy, dw, dh
        self.idle = blend(bg, FG, 0.20 if dim else 0.32)
        self.lit = blend(bg, FG, 0.38) if dim else col
        self.well = blend(bg, FG, 0.12)
        self.track = blend(bg, FG, 0.18)
        self.resting = blend(bg, FG, 0.48)

    def X(self, u):
        return self.ox + u * self.dw

    def Y(self, v):
        return self.oy + v * self.dh

    def S(self, u):
        return max(2.0, u * self.dh)

    def glyph(self, name, cx, cy, w, h=None, active=False, color=None,
              clip=None):
        """One piece of button art, centred, pressed or not.

        Idle draws the outline glyph; pressed draws the filled one, whose
        label is knocked out of the shape — so a press reads as the player's
        colour with the letter showing the bay through it.
        """
        h = w if h is None else h
        self.ui.image(art(self.art + name + ("_on" if active else "")),
                      cx - w / 2, cy - h / 2, w, h,
                      color or (self.lit if active else self.idle), clip=clip)

    def face(self, name, cx, cy, w, h=None, pressed=False, wys=None):
        """A face button, in three layers.

        Three layers from one glyph, bottom up: the press fills the whole
        shape, the outline paints its edge in the wysiwyg colour — green when
        the label is telling the truth, amber when it is not — and the label
        goes on last in white so it stays readable either way.

        The press goes UNDERNEATH deliberately. Drawn on top it had to be
        eroded to sit inside the outline, and that either left a dark ring of
        background inside every pressed button or, eroded less, covered half
        the outline's width.

        The fill used to knock the label out of itself, for a negative. It
        reads well on a GameCube's big A and not at all on a Switch's small
        circles, where Kenney's letter is nearly as wide as the room left
        inside the ring and a press came out as two red crescents.

        An empty bay has no answer to give, so its outline stays dim and its
        label is left as part of the glyph.
        """
        h = w if h is None else h
        if pressed:
            self.glyph(name + "_press", cx, cy, w, h, color=self.lit)
        self.glyph(name, cx, cy, w, h,
                   color=None if wys is None else
                   (RING_OK if wys else RING_BAD))
        if wys is not None:
            self.glyph(name + "_letter", cx, cy, w, h, color=FG)


def draw_gamepad(ui, bx, by, bw, bh, col, held, axes, bg, swap=False,
                 dim=False, holds=None, wys=None, layout="switch"):
    """A button map — no controller body.

    Drawing a shape means picking *a* shape, and every real pad is a different
    one; the outline ends up either wrong for most players or so generic it
    says nothing. Only the inputs matter here, so only the inputs are drawn,
    laid out where a hand expects them. Everything is dim until pressed.

    `layout` picks which pad the map describes. It is the EMULATED pad, not
    the one in the player's hands: a GameCube game gets a GameCube map, so
    what is on screen is what the game will answer to.
    """
    # The N64 map is drawn on a taller strip. Its cluster reaches two and a
    # half buttons above A where the GameCube's reaches one, so on the wide
    # strip every glyph had to shrink to fit that climb; height is what the
    # layout actually needs, and the lane the second stick would have used
    # is free on this pad anyway.
    aspect = PAD_ASPECT_N64 if layout == "n64" else PAD_ASPECT
    dw, dh = bw, bw / aspect
    if dh > bh:
        dw, dh = bh * aspect, bh
    g = Bay(ui, bx + (bw - dw) / 2, by + (bh - dh) / 2, dw, dh, col, bg, dim,
            art={"gamecube": "gc/", "n64": "n64/",
                 "playstation": "ps/"}.get(layout, ""))
    controls = {"gamecube": _gamecube_controls,
                "n64": _n64_controls,
                "playstation": _playstation_controls}.get(
                    layout, _switch_controls)
    controls(g, held, axes, swap, holds or {}, wys)


class Layout:
    """Where the controls go. The same on both maps, by design.

    A player who has used one of these screens should recognise the other
    instantly, so the rows, the lanes and the sizes are shared and only the
    glyphs differ. Every number here is a fraction of the strip's HEIGHT —
    height is what the bay actually limits — except positions across, which
    are measured out from the point midway between the two sticks.
    """

    # Four lanes on one centre line, equal air between them. Each is as wide
    # as its content can ever get: a stick at full deflection, the face
    # cluster at its full spread. Computed rather than hand-placed, because
    # every time these were fixed fractions a size change put two of them on
    # top of each other.
    def __init__(self, g):
        S = self.S = g.S
        self.dside = S(0.48)                  # the d-pad
        self.sside = S(0.36)                  # a stick's own glyph
        self.stravel = self.sside * 0.28      # how far it can move
        self.fside = S(0.30)                  # one face button
        self.fspread = S(0.215)               # from the cluster's centre
        # Measured off the art: a face glyph's own circle ends at 0.375 of
        # the box side, 96px of ink in a 128px canvas.
        self.freach = self.fspread + self.fside * 0.375

        lanes = (self.dside,
                 self.sside + 2 * self.stravel,
                 self.sside + 2 * self.stravel,
                 2 * self.freach)
        air = (g.dw - sum(lanes)) / (len(lanes) + 1)
        self.centres, run = [], g.ox + air
        for lane in lanes:
            self.centres.append(run + lane / 2)
            run += lane + air

        self.row_y = g.Y(0.635)
        self.top_y = g.Y(0.085)
        self.top_box = S(0.31)
        self.floor = g.oy + g.dh

        # The top row hangs off the point midway BETWEEN THE STICKS, not the
        # middle of the strip. Those are not the same point — the face
        # cluster's lane is wider than the d-pad's, so the lanes sit left of
        # centre — and a top row centred on the strip was visibly out of line
        # with everything under it. Scaled to whatever fits once it is
        # off-centre, keeping its own spacing.
        self.air = air
        self.mid = (self.centres[1] + self.centres[2]) / 2
        # Where a cluster that spans the last two lanes sits: the N64 map's
        # C buttons need the room, and nothing else is in that lane there.
        self.mid_faces = (self.centres[2] + self.centres[3]) / 2
        reach = min(self.mid - g.ox, g.ox + g.dw - self.mid) - S(0.31) / 2
        self.fit = min(1.0, reach / S(1.02))
        self.dh = g.dh
        self.ox, self.dw = g.ox, g.dw

    # Across the top row, by role rather than by name: the outer pair are the
    # analog triggers on either pad, then the shoulders, then whatever sits
    # beside the middle. A GameCube pad has fewer of them, not different ones.
    TRIGGER = 1.02
    SHOULDER = 0.684
    INNER = 0.228

    def across(self, offset):
        return self.mid + offset * self.fit * self.dh


# One entry in the top row. `buttons` is every button that lights it — a
# GameCube's Z is one button on that pad and two on the controller in your
# hands. `axis` is the SDL axis behind it, if any, and `analog` says whether
# the pad MEANS it: a GameCube's L and R are real analog triggers and fill as
# they go down, while a Switch pad's ZL and ZR are switches wearing a
# trigger's shape. The GameCube is the only Nintendo console that ever had
# analog shoulders, so it is the only map that draws them that way.
Control = collections.namedtuple(
    "Control", "offset name box buttons axis analog dy",
    defaults=((), None, False, 0.0))


def draw_top_row(g, lay, items, holds, held, axes):
    """The top strip, shared by both maps."""
    for c in items:
        cx, cy = lay.across(c.offset), lay.top_y + c.dy
        if c.analog:
            trigger(g, c.name, cx, cy, c.box, c.box,
                    axes.get(c.axis, 0))
            continue
        down = any(b in held for b in c.buttons)
        if c.axis is not None:
            down = down or axes.get(c.axis, 0) > 8000
        g.glyph(c.name, cx, cy, c.box, active=down)
        for btn in c.buttons:
            if holds.get(btn, 0.0) > 0:
                hold_ring(g.ui, cx, cy, c.box * 0.643, holds[btn],
                          g.lit, g.track)


def _switch_controls(g, held, axes, swap, holds, wys):
    S = g.S
    on = held.__contains__
    lay = Layout(g)

    draw_top_row(g, lay, (
        Control(-lay.TRIGGER, "zl", S(0.31), axis=4),
        Control(-lay.SHOULDER, "l", S(0.31), (BTN_LSHOULDER,)),
        Control(lay.SHOULDER, "r", S(0.31), (BTN_RSHOULDER,)),
        Control(lay.TRIGGER, "zr", S(0.31), axis=5),
        Control(-lay.INNER, "minus", S(0.21), (BTN_BACK,)),
        Control(lay.INNER, "plus", S(0.21), (BTN_START,)),
    ), holds, held, axes)

    # The directional art is the same cross with one arm marked, so a pressed
    # direction goes straight over the idle cross — a diagonal shows both arms
    # for free. The d-pad keeps the plain tint: it is not a button with a
    # label to knock out.
    dpad_arms(g, lay.centres[0], lay.row_y, lay.dside, held)

    # Face buttons are drawn in the Switch's arrangement — X top, Y left,
    # A right, B bottom — and each lights by NAME, not by position. Press the
    # button marked A on any pad and the circle marked A lights, whatever
    # corner it physically lives in. Location accuracy is the thing being
    # traded away, deliberately.
    live = face_live(held, swap)
    for letter, name, dx, dy in (("X", "x", 0, -1), ("Y", "y", -1, 0),
                                 ("A", "a", 1, 0), ("B", "b", 0, 1)):
        g.face(name, lay.centres[3] + dx * lay.fspread,
               lay.row_y + dy * lay.fspread, lay.fside,
               pressed=letter in live, wys=wys)

    for lane, btn, ax, ay, name in ((1, BTN_LSTICK, 0, 1, "stick_l"),
                                    (2, BTN_RSTICK, 2, 3, "stick_r")):
        stick(g, lay.centres[lane], lay.row_y, lay.sside, lay.stravel,
              axes, ax, ay, name, on(btn))


# A DualShock in the shared skeleton, which it fits without adjustment: the
# triggers outermost, the shoulders inboard of them, Select and Start in the
# middle, and the four shapes in a diamond.
#
# This map has no swap GESTURE — a shape is a position, north is triangle on
# every pad ever made, so there is nothing for a player to choose. But it
# still has to be compensated, because SDL's letters are not always
# positions: on a Nintendo-lettered pad, SDL's A is the button MARKED A,
# which sits east. Measured on an 8Bitdo SF30 Pro through Steam Input:
# pressing the bottom button lit circle and pressing east lit cross.
#
# So the shapes are drawn and bound against the pad's own hardware layout,
# automatically and invisibly — nintendo_layout(), the same fact the other
# maps use for their default. Nobody is asked, and nothing is toggled.
PS_CAPTION = 1.5

PS_FACES = (("A", "cross", 0, 1), ("B", "circle", 1, 0),
            ("X", "square", -1, 0), ("Y", "triangle", 0, -1))


def _playstation_controls(g, held, axes, swap, holds, wys):
    S = g.S
    on = held.__contains__
    lay = Layout(g)

    draw_top_row(g, lay, (
        Control(-lay.TRIGGER, "l2", S(0.31), axis=4),
        Control(-lay.SHOULDER, "l1", S(0.31), (BTN_LSHOULDER,)),
        Control(lay.SHOULDER, "r1", S(0.31), (BTN_RSHOULDER,)),
        Control(lay.TRIGGER, "r2", S(0.31), axis=5),
        # Bigger than the other maps' middle pair: these two wear their
        # captions, so the box has to carry a word as well as a shape. At
        # the shared size the words were there but unreadable.
        Control(-lay.INNER, "select", S(0.21 * PS_CAPTION), (BTN_BACK,)),
        Control(lay.INNER, "start", S(0.21 * PS_CAPTION), (BTN_START,)),
    ), holds, held, axes)

    dpad_arms(g, lay.centres[0], lay.row_y, lay.dside, held)

    live = face_live(held, swap)       # hardware layout, not a choice
    for letter, name, dx, dy in PS_FACES:
        g.face(name, lay.centres[3] + dx * lay.fspread,
               lay.row_y + dy * lay.fspread, lay.fside,
               pressed=letter in live, wys=wys)

    for lane, btn, ax, ay, name in ((1, BTN_LSTICK, 0, 1, "stick_l"),
                                    (2, BTN_RSTICK, 2, 3, "stick_r")):
        stick(g, lay.centres[lane], lay.row_y, lay.sside, lay.stravel,
              axes, ax, ay, name, on(btn))


# The GameCube face cluster: an offset from A and a size, both in units of
# the shared face box. That is all a map needs to own now — the rows, the
# lanes and everything else come from Layout — and expressing the size as a
# box rather than as ink keeps each glyph at the shape Kenney drew it, since
# the box scales and the ink inside it follows.
#
# Measured off gc.psd, whose four layers are the Kenney glyphs this map
# draws, arranged the way a GameCube arranges them: a big A with B below and
# left, X on its end to the right, Y lying across the top. B really is that
# much smaller than A on the pad. Absolute scale is not read — gc_cluster
# fits the cluster to the lane — so only these ratios matter.
GC_FACES = {
    #        offset from A      (width, height) of its box
    "a": ((+0.00, +0.00), (1.00, 1.00)),
    "b": ((-0.72, +0.36), (0.63, 0.63)),
    # X is 11% taller than it is wide relative to the glyph: the layout file
    # stretches it, and that stretch is what stands it upright. Averaging the
    # two into one number left it leaning into A, which is what the pad's own
    # X does and what the layout was drawn to fix.
    "x": ((+0.69, -0.17), (0.83, 0.93)),
    "y": ((-0.18, -0.68), (0.88, 0.89)),
}

# Everything preflight draws is white so it can be tinted — SDL's colour
# modulation only darkens, so white is the only ink that can become any
# colour. The C stick is the exception: it is yellow on the pad and yellow in
# the pack, and tinting that by a player's colour turns it to mud. It keeps
# its own colour and is dimmed rather than recoloured.
SELF_COLOURED = {"gc/stick_r"} | {
    f"n64/{n}" for n in ("c", "c_up", "c_down", "c_left", "c_right")}

# How much of its canvas a glyph's ink takes up, for the few places that
# measure against ink rather than the box it is drawn in. Keyed the way the
# art is: no prefix for the Switch set, "gc/" for the GameCube one.
INK = {"gc/l": (0.750, 0.688), "gc/r": (0.750, 0.688)}


def gc_cluster(lay, faces=None):
    """(centre, scale) for the face cluster, grown into the room it has.

    A GameCube's four buttons are spread wider and taller than the Switch
    map's diamond, so fitting them inside a diamond-sized lane made every one
    of them small. They get the lane plus most of the air beside it instead,
    and are then held back by whichever runs out first: that width, the gap
    up to the top row, or the bottom of the strip. Computed rather than
    chosen, because the answer changes with every other number here.
    """
    xs, ys = [], []
    for (dx, dy), (sw, sh) in (faces or GC_FACES).values():
        xs += [dx - sw / 2, dx + sw / 2]
        ys += [dy - sh / 2, dy + sh / 2]
    lo_x, hi_x, lo_y, hi_y = min(xs), max(xs), min(ys), max(ys)
    centre = ((lo_x + hi_x) / 2, (lo_y + hi_y) / 2)

    # Measured from the cluster's own centre, since that is what gets put on
    # the row: half its span each way, not its distance from A. A reaches
    # further down than up and Y further up than down, so measuring from A
    # made the cluster look taller than it is and held it back.
    across = (hi_x - lo_x) * lay.fside
    half = (hi_y - lo_y) / 2 * lay.fside
    gap = lay.S(0.04)
    room_up = lay.row_y - (lay.top_y + lay.top_box / 2) - gap
    room_down = lay.floor - lay.row_y
    return centre, min((2 * lay.freach + 1.6 * lay.air) / across,
                       room_up / half, room_down / half)


def _gamecube_controls(g, held, axes, swap, holds, wys):
    """What a GameCube pad has, where the Switch map has its own.

    The differences are the pad's own, not decoration: two analog shoulders
    instead of four, Z alone on the right, one Start, and a face cluster
    built around a big A. They sit in the same places as the Switch map's
    equivalents — the outer pair of the top row are the analog triggers on
    either pad — so the two screens are the same screen with different
    lettering. Everything lights by what Dolphin will bind it to, so Z takes
    either shoulder, and L and R are the triggers.
    """
    S = g.S
    on = held.__contains__
    lay = Layout(g)

    # Z is one button on a GameCube pad and Dolphin is told to take either
    # shoulder for it, so it sits in the right shoulder's place and lights
    # from both. Start takes the middle, between where minus and plus are.
    draw_top_row(g, lay, (
        Control(-lay.TRIGGER, "l", S(0.31), axis=4, analog=True),
        Control(lay.SHOULDER, "z", S(0.31),
                (BTN_LSHOULDER, BTN_RSHOULDER)),
        Control(lay.TRIGGER, "r", S(0.31), axis=5, analog=True),
        Control(0.0, "start_plain", S(0.21), (BTN_START,)),
    ), holds, held, axes)

    # Quit runs through Start as well, because this pad has no second button
    # to give it: Start alone starts, Z with it quits. Z being lit says which,
    # and the ring's colour backs that up — white rather than the obvious red,
    # because P1 IS red and the two rings came out the same on the one pad
    # that does the starting.
    if holds.get(BTN_BACK, 0.0) > 0:
        hold_ring(g.ui, lay.across(0.0), lay.top_y, S(0.21) * 0.643,
                  holds[BTN_BACK], FG, g.track)

    dpad_arms(g, lay.centres[0], lay.row_y, lay.dside, held)

    for lane, btn, ax, ay, name in ((1, BTN_LSTICK, 0, 1, "stick_l"),
                                    (2, BTN_RSTICK, 2, 3, "stick_r")):
        stick(g, lay.centres[lane], lay.row_y, lay.sside, lay.stravel,
              axes, ax, ay, name, on(btn))

    (anchor_x, anchor_y), scale = gc_cluster(lay)
    live = face_live(held, swap)
    for letter, name in (("A", "a"), ("B", "b"), ("X", "x"), ("Y", "y")):
        (dx, dy), (sw, sh) = GC_FACES[name]
        g.face(name,
               lay.centres[3] + (dx - anchor_x) * lay.fside * scale,
               lay.row_y + (dy - anchor_y) * lay.fside * scale,
               lay.fside * sw * scale, lay.fside * sh * scale,
               pressed=letter in live, wys=wys)


# An N64 pad's face controls: A, B beside and above it, and the four C
# buttons in their own cross above both. Read straight off n64.psd — each
# layer's box, divided by A's, with A at the origin. Same units as GC_FACES.
#
# The C buttons are four buttons, not a stick. gopher64 binds them to the
# right stick, which is how they are played, but the map draws what the N64
# has, and a player looking for C-up should find a button marked C-up.
# Ink measures 0.78 of the box a glyph is drawn in (tools/stage-art.py's
# N64_INK), so a size measured off the art has to be divided by that to give
# the box that draws it. Skipping this drew every button a fifth smaller than
# its own spacing assumed, which is what opened the gaps in the C cross.
N64_BOX = 1 / 0.78

N64_FACES = {
    # Measured off n64-3.psd — the ink in its composite, not its layer boxes,
    # which carry margin and made the C buttons come out half size. Units of
    # a face button (57 px there), from B.
    "b":       ((+0.000, +0.000), (1.000, 1.000)),
    "a":       ((+0.877, +0.895), (1.000, 1.000)),
    # The cross is pulled 0.30 nearer than the draft has it, by eye: the
    # draft's own gap was drawn against the pack's arrows, which are smaller
    # than these icons, so at the real button size it read as a hole.
    "c_up":    ((+2.849, -0.237), (0.724, 0.724)),
    "c_left":  ((+2.095, +0.465), (0.724, 0.724)),
    "c_right": ((+3.595, +0.474), (0.724, 0.724)),
    "c_down":  ((+2.849, +1.167), (0.724, 0.724)),
    # The C in the middle of them is a label, not a button: never lit, and
    # not counted when the cluster is fitted, since it sits inside it. The
    # draft leaves it out — it was drawn with the pack's arrows — but the
    # icons have one, and without it the cross reads as a second d-pad.
    "c":       ((+2.845, +0.467), (0.370, 0.370)),
}
N64_FACES = {name: (offset, (sw * N64_BOX, sh * N64_BOX))
             for name, (offset, (sw, sh)) in N64_FACES.items()}

# A nudge right, by eye: the group is measured from B, and leaving it centred
# in the space it is given put more air to its right than its left.
N64_SHIFT = 0.85

# The N64 map is drawn on a taller strip, so a box given as a fraction of
# that strip's height comes out bigger in pixels than the same box on the
# other maps — L and R measured 85 px against the GameCube's 69. The top row
# is meant to be the one part every map shares, so this holds it to the same
# drawn size. Measured, not guessed: 69/85.
N64_TOP = 1.38

# Z is its own size and place. The icon is an upright rounded rectangle where
# every other glyph in the row is a wide pill, so at the row's size it read as
# the biggest thing up there; and with the row this large it sat too close to
# L. Smaller, and a little further out.
N64_Z = 0.80
N64_Z_OUT = 0.12
N64_START_DOWN = 0.12

# Which way each C button reads on the stick gopher64 binds it to.
N64_C_AXES = {"c_up": (3, -1), "c_down": (3, 1),
              "c_left": (2, -1), "c_right": (2, 1)}
N64_C_DEADZONE = 12000


def _n64_controls(g, held, axes, swap, holds, wys):
    """What an N64 pad has, in the places the other maps put their own.

    Three things are the pad's own. There is one analog stick, so the second
    stick lane carries the C buttons — which is not a liberty: gopher64 binds
    C to the right stick, so that IS what the game answers to, and the C
    glyph is yellow exactly as the buttons are. Z is a trigger and sits where
    the other maps' left trigger is. And there are two face buttons, so the
    swap gesture trades A and B and nothing else.
    """
    S = g.S
    on = held.__contains__
    lay = Layout(g)

    # Z on the left where L and ZL live on the other maps, L and R in the
    # shoulder places, Start in the middle, as on the GameCube map. Z is
    # digital on this pad even though gopher64 reads it off a trigger axis.
    draw_top_row(g, lay, (
        Control(-(lay.TRIGGER + N64_Z_OUT), "z",
                S(0.31 * N64_TOP * N64_Z), axis=5),
        Control(-lay.SHOULDER, "l", S(0.31 * N64_TOP), (BTN_LSHOULDER,)),
        Control(lay.SHOULDER, "r", S(0.31 * N64_TOP), (BTN_RSHOULDER,)),
        # Start sits lower than the rest of the row: the hold ring is drawn
        # round it at two thirds of its box again, and at this row size that
        # ring reached past the top of the bay and crossed the card's frame.
        Control(0.0, "start_plain", S(0.21 * N64_TOP), (BTN_START,),
                dy=N64_START_DOWN * S(1.0)),
    ), holds, held, axes)

    # Start alone starts, Z with it quits — the GameCube arrangement, for the
    # same reason: no second button on the pad to give quitting.
    if holds.get(BTN_BACK, 0.0) > 0:
        hold_ring(g.ui, lay.across(0.0), lay.top_y + N64_START_DOWN * S(1.0),
                  S(0.21 * N64_TOP) * 0.643, holds[BTN_BACK], FG, g.track)

    dpad_arms(g, lay.centres[0], lay.row_y, lay.dside, held)

    # One stick, in the first stick lane. The second lane is where the C
    # cluster reaches into: this pad has nothing else to put there, and the
    # cluster is wide enough to want it.
    stick(g, lay.centres[1], lay.row_y, lay.sside, lay.stravel,
          axes, 0, 1, "stick_l", on(BTN_LSTICK))

    (anchor_x, anchor_y), scale, group_x = n64_cluster(lay)
    live = face_live(held, swap)
    for name, (letter, pressed) in N64_CLUSTER.items():
        (dx, dy), (sw, sh) = N64_FACES[name]
        cx = group_x + (dx - anchor_x) * lay.fside * scale
        cy = lay.row_y + (dy - anchor_y) * lay.fside * scale
        w, h = lay.fside * sw * scale, lay.fside * sh * scale
        if letter is not None:            # A and B: lit by label, ringed
            g.face(name, cx, cy, w, h, pressed=letter in live, wys=wys)
            continue
        down = False
        if name in N64_C_AXES:
            axis, sign = N64_C_AXES[name]
            down = axes.get(axis, 0) * sign > N64_C_DEADZONE
        g.glyph(name, cx, cy, w, h, active=down)


# What each glyph in the cluster is: a face button lit by its label, or a
# plain glyph lit by an axis (or, for the C in the middle, never).
N64_CLUSTER = {"c_up": (None, True), "c_left": (None, True),
               "c_right": (None, True), "c_down": (None, True),
               "c": (None, False),
               "b": ("B", True), "a": ("A", True)}


def n64_cluster(lay):
    """(centre, scale, x) for the N64 group: A, B and the C cross together.

    Wide rather than tall — four C buttons beside two face buttons is over
    four buttons across — so what it needs is width, and this pad leaves
    plenty: there is one stick, so everything from the stick's edge to the
    end of the strip is its own. Height still limits it when the bay is
    short, and the C label is left out of the measuring since it sits inside
    the cross.
    """
    xs, ys = [], []
    for name, ((dx, dy), (sw, sh)) in N64_FACES.items():
        if name == "c":
            continue
        xs += [dx - sw / 2, dx + sw / 2]
        ys += [dy - sh / 2, dy + sh / 2]
    lo_x, hi_x, lo_y, hi_y = min(xs), max(xs), min(ys), max(ys)
    centre = ((lo_x + hi_x) / 2, (lo_y + hi_y) / 2)

    gap = lay.S(0.04)
    left = lay.centres[1] + lay.sside / 2 + lay.stravel + lay.air / 2
    right = lay.ox + lay.dw
    across = (hi_x - lo_x) * lay.fside
    tall = (hi_y - lo_y) * lay.fside
    room_up = lay.row_y - (lay.top_y + lay.top_box / 2) - gap
    room_down = lay.floor - lay.row_y
    scale = min((right - left) / across,
                2 * min(room_up, room_down) / tall)
    return centre, scale, (left + right) / 2 + N64_SHIFT * lay.fside * scale


def dpad_arms(g, cx, cy, side, held):
    """The idle cross, plus an arm for every direction being pressed."""
    g.glyph("dpad", cx, cy, side, color=g.idle)
    for btn, name in ((BTN_DPAD_UP, "dpad_up"), (BTN_DPAD_DOWN, "dpad_down"),
                      (BTN_DPAD_LEFT, "dpad_left"),
                      (BTN_DPAD_RIGHT, "dpad_right")):
        if btn in held:
            g.glyph(name, cx, cy, side, color=g.lit)


def face_live(held, swap):
    """Which LABELS are lit, given which SDL buttons are down."""
    mirror = {"A": "B", "B": "A", "X": "Y", "Y": "X"}
    live = set()
    for btn in (BTN_A, BTN_B, BTN_X, BTN_Y):
        if btn in held:
            name = BUTTON_NAMES[btn]
            live.add(mirror[name] if swap else name)
    return live


def stick(g, cx, cy, side, travel, axes, ax, ay, name, clicked):
    """A dim well with a knob that actually moves."""
    # The well shows where centre is, so a deflection reads as movement
    # rather than as a glyph that happens to sit off to one side.
    g.ui.fill_circle(cx, cy, side * 0.50, g.well)
    kx = (axes.get(ax, 0) / 32768.0) * travel
    ky = (axes.get(ay, 0) / 32768.0) * travel
    moved = abs(kx) + abs(ky) > 1.5
    if g.art + name in SELF_COLOURED:
        # Dimmed rather than recoloured, since recolouring is what would
        # ruin it. Movement still reads: the glyph moves.
        tint = (255, 255, 255) if (moved or clicked) else (150, 150, 150)
    else:
        tint = g.lit if (moved or clicked) else g.resting
    g.glyph(name, cx + kx, cy + ky, side, color=tint)


def trigger(g, name, cx, cy, w, h, value):
    """An analog shoulder, filling from the bottom as it is pressed.

    The outline is always there, so the control is findable at rest; the
    filled glyph is drawn over it and clipped, which makes the ink itself the
    gauge. Reading the fill off the shape beats a separate bar next to it —
    there is no room for one, and a player looking at the L on screen is
    already looking in the right place.

    The clip runs from the bottom of the INK rather than the bottom of the
    box. Every glyph in the pack sits in its own margin — L's is a fifth of
    the canvas — so measuring from the box meant the first fifth of the
    trigger's travel filled empty space and the button appeared to ignore a
    slow press entirely.
    """
    g.glyph(name, cx, cy, w, h)
    frac = min(1.0, max(0.0, value / 32767.0))
    if frac <= 0.005:
        return
    ink = h * INK[g.art + name][1]
    floor = cy + ink / 2
    top = floor - ink * frac
    g.glyph(name, cx, cy, w, h, active=True,
            clip=(cx - w / 2, top, w, floor - top))


def draw_frame(ui, title, subtitle=None, emoji=None, alert=None):
    ui.clear(BG)
    x, y = int(ui.w * 0.05), int(ui.h * 0.06)
    _, title_h = ui.text_size(title, "title", True)
    if emoji:
        # Emoji glyphs are square and sit taller than the text they follow, so
        # draw them a size down and centre them on the title's line.
        ew, eh = ui.text_size(emoji, "head", emoji=True)
        if ew:
            ui.text(emoji, x, y + (title_h - eh) / 2, "head", FG, emoji=True)
            x += ew + int(ui.size["title"] * 0.30)
    ui.text(title, x, y, "title", FG, bold=True)
    if alert:
        # Takes the subtitle's place rather than squeezing in beside it: this
        # is the one message that has to be read from the sofa, so it gets a
        # filled band and the whole line to itself.
        bx, by = int(ui.w * 0.05), int(y + title_h + 6)
        bh = int(ui.size["head"] * 1.55)
        bw = int(ui.w * 0.90)
        ui.round_rect(bx, by, bw, bh, int(bh * 0.22), blend(BG, BAD, 0.35))
        ui.frame(bx, by, bw, bh, BAD, max(2, int(ui.h * 0.004)))
        room = bw - int(ui.w * 0.04)
        size = "head" if ui.text_size(alert, "head", True)[0] <= room else "body"
        _, ah = ui.text_size(alert, size, True)
        ui.text(alert, int(ui.w * 0.5), by + (bh - ah) / 2, size, FG,
                bold=True, center=True)
    elif subtitle:
        # Step down a size rather than run off the edge on a long line.
        room = int(ui.w * 0.90)
        size = "body" if ui.text_size(subtitle, "body")[0] <= room else "small"
        ui.text(subtitle, int(ui.w * 0.05), y + title_h + 4, size, DIM)


def draw_hint(ui, hint):
    ui.text(hint, ui.w // 2, int(ui.h * 0.91), "body", DIM, center=True)


# SDL's own axis numbering, for the log: an axis that never appears here is
# an axis the pad is not sending, which is worth knowing when a control seems
# dead.
# A stick jitters around its centre and needs a deadzone, or the screen never
# settles and every frame is a repaint. A trigger does not: it rests at zero
# and stays there, and the first fifth of its travel is exactly what the
# GameCube map's gauge is for. One number for both meant a gentle squeeze was
# stored as nothing at all and drew nothing, which looked like the trigger
# being ignored until something else woke the screen up.
STICK_DEADZONE = 6000
# 2000, not 400: a Steam virtual pad's triggers sit at around 1200 at rest,
# measured in a log, and a smaller number left a permanent sliver of fill on
# a trigger nobody was touching. Still a fifteenth of the old 6000, so a
# light squeeze registers.
AXIS_DEADZONE = {sdlui.AXIS_TRIGGERLEFT: 2000, sdlui.AXIS_TRIGGERRIGHT: 2000}

AXIS_NAMES = {0: "Left X", 1: "Left Y", 2: "Right X", 3: "Right Y",
              4: "Trigger L", 5: "Trigger R"}


# Circles for anything that is round on the pad itself: the face buttons,
# minus and plus, and the two stick presses — L3 and R3 are joysticks, so a
# pill would read as another shoulder button.
# The legend draws the same art the pads do, so a glyph means one thing on
# the whole screen. Everything here is a square image, which also retires the
# old pill-versus-circle bookkeeping.
GLYPH_ART = {
    "+": "plus", "\u2013": "minus",
    "L3": "stick_side_l", "R3": "stick_side_r",
    "L": "l", "R": "r", "ZL": "zl", "ZR": "zr",
    # The GameCube set. A scale comes with the ones whose ink is short and
    # wide — a pill drawn in the same square box as a circle reads as the
    # smaller control, which is backwards for a shoulder button.
    "gc:l": ("gc/l", 1.15), "gc:r": ("gc/r", 1.15),
    "gc:z": ("gc/z", 1.30),
    # Start without its caption: there is no room for lettering this small,
    # and the label beside it already says what holding it does.
    "gc:start": ("gc/start_plain", 1.0),
    # The N64 set. Start keeps its caption on this map, so the legend shows
    # the same button the map does.
    "n64:z": ("n64/z", 1.0), "n64:start": ("n64/start_plain", 1.0),
    # The PlayStation set. Its shoulders and triggers are wide like the
    # GameCube's, so they take the same widening.
    "ps:l2": ("ps/l2", 1.15), "ps:r2": ("ps/r2", 1.15),
    "ps:select": ("ps/select", 1.0), "ps:start": ("ps/start", 1.0),
}


def glyph_art(g):
    """(art name, width scale) for a legend glyph drawn from a picture."""
    entry = GLYPH_ART[g]
    return entry if isinstance(entry, tuple) else (entry, 1.0)
GLYPH_ROUND = {"A", "B", "X", "Y"}


def _glyph_metrics(ui, size):
    """Pill height, per-glyph width, and the spacing that goes with a size."""
    _, th = ui.text_size("Ag", size)
    pill_h = int(th * 1.45)

    def glyph_w(g):
        if g.startswith("sep"):
            return ui.text_size(g[3:], size, True)[0] + pill_h * 0.20
        if g in GLYPH_ART:
            # The art is square, with its own margin.
            return pill_h * 1.18 * glyph_art(g)[1]
        tw, _ = ui.text_size(g, size, True)
        return max(pill_h, tw + pill_h * (0.45 if g in GLYPH_ROUND else 0.85))

    return pill_h, glyph_w, pill_h * 0.26


def _item_glyph_widths(ui, glyphs, size):
    """Width per glyph, with every circle in this entry sharing a diameter.

    "R3" is wider than "L3" in the font, so sizing each circle to its own
    label gave a combo made of two mismatched buttons.
    """
    _pill_h, glyph_w, _spacing = _glyph_metrics(ui, size)
    widths = {g: glyph_w(g) for g in glyphs}
    circles = [widths[g] for g in glyphs if g in GLYPH_ROUND]
    if circles:
        biggest = max(circles)
        for g in glyphs:
            if g in GLYPH_ROUND:
                widths[g] = biggest
    return widths


def _glyph_item_width(ui, item, size, bold=False):
    glyphs, token, _tcol, label = item
    pill_h, _glyph_w, spacing = _glyph_metrics(ui, size)
    gws = _item_glyph_widths(ui, glyphs, size)
    w = sum(gws[g] + spacing for g in glyphs)
    w += ui.text_size(label, size, bold)[0]
    if token:
        w += ui.text_size(token, size, True)[0] + pill_h * 0.26
    return w


def _draw_glyph_item(ui, item, x, y_mid, size, bold=False):
    """One legend entry, vertically centred on y_mid. Returns its width."""
    glyphs, token, tcol, label = item
    pill_h, _glyph_w, spacing = _glyph_metrics(ui, size)
    gws = _item_glyph_widths(ui, glyphs, size)
    y = y_mid - pill_h / 2
    x0 = x
    for g in glyphs:
        gw = gws[g]
        shell = blend(BG, FG, 0.20)
        if g.startswith("sep"):
            # A bare joiner, not a button: no pill, so "L3 + R3" reads as one
            # combo rather than three separate things to press.
            _, sth = ui.text_size(g[3:], size, True)
            ui.text(g[3:], x + gw / 2, y + (pill_h - sth) / 2, size, DIM,
                    bold=True, center=True)
        elif g in GLYPH_ART:
            name, scale = glyph_art(g)
            side = min(gw, pill_h * 1.18 * scale)
            ui.image(art(name), x + (gw - side) / 2,
                     y + (pill_h - side) / 2, side, side, FG)
        else:
            if g in GLYPH_ROUND:
                # Radius follows the width so a two-character label like L3
                # sits inside its circle instead of spilling over the edge.
                ui.fill_circle(x + gw / 2, y + pill_h / 2, max(pill_h, gw) / 2,
                               shell)
            else:
                ui.round_rect(x, y, gw, pill_h, pill_h / 2, shell)
            if len(g) == 1:
                ui.glyph_centered(g, x + gw / 2, y + pill_h / 2, size, FG)
            else:
                gth = ui.text_size(g, size, True)[1]
                ui.text(g, x + gw / 2, y + (pill_h - gth) / 2, size, FG,
                        bold=True, center=True)
        x += gw + spacing
    if token:
        tw, tht = ui.text_size(token, size, True)
        ui.text(token, x, y_mid - tht / 2, size, tcol, bold=True)
        x += tw + pill_h * 0.26
    _, lh = ui.text_size(label, size, bold)
    ui.text(label, x, y_mid - lh / 2, size, FG if bold else DIM, bold=bold)
    return (x + ui.text_size(label, size, bold)[0]) - x0


def glyph_bar(ui, items, hidden=()):
    """The legend along the bottom, drawn with the same shapes as the pads.

    Each entry is (glyphs, token, token_colour, label). The glyphs stay
    neutral — colour on the button itself would imply only that player may
    press it — and only the player token in the text is tinted.

    The first entry is the one that starts the game, so it is flush left where
    the eye lands first, and drawn bold. The last entry quits, so it sits hard
    right, as far from the other controls as the screen allows. Whatever is
    left over spreads through the middle. The size steps down rather than
    letting anything run off the edge.

    An index in `hidden` is measured but not drawn, so the entries beside it
    keep the position they had. Claiming P1 removes that control mid-session,
    and everything else sliding sideways in response is exactly the sort of
    thing that makes someone press the wrong button.
    """
    if not items:
        return

    margin = int(ui.w * 0.05)
    y_mid = int(ui.h * 0.925)

    def fits(size):
        widths = [_glyph_item_width(ui, it, size, bold=(i == 0))
                  for i, it in enumerate(items)]
        _, _, spacing = _glyph_metrics(ui, size)
        gap = spacing * 3.4
        return widths, gap, sum(widths) + gap * (len(items) - 1)

    for size in ("body", "small"):
        widths, gap, total = fits(size)
        if total <= ui.w - margin * 2:
            break

    xs = [0.0] * len(items)
    xs[0] = margin
    if len(items) > 1:
        xs[-1] = ui.w - margin - widths[-1]
    middle = range(1, len(items) - 1)
    if middle:
        left = xs[0] + widths[0] + gap
        right = xs[-1] - gap
        span = sum(widths[i] for i in middle) + gap * (len(list(middle)) - 1)
        x = left + ((right - left) - span) / 2
        for i in middle:
            xs[i] = x
            x += widths[i] + gap

    for i, (item, x) in enumerate(zip(items, xs)):
        if i not in hidden:
            _draw_glyph_item(ui, item, x, y_mid, size, bold=(i == 0))


def draw_pad_grid(ui, pads, cycle, warnings, needed, holds, p1_claimed,
                  alert=None, layout="switch", wiiu=None):
    draw_frame(ui, "Controller check",
               "Controllers must be paired in your OS first \u2014 "
               "test your inputs before the game starts", emoji="\U0001F6A7",
               alert=alert)

    gx, gy = int(ui.w * 0.05), int(ui.h * 0.19)
    gw, gh = int(ui.w * 0.90), int(ui.h * 0.63)
    gap = int(ui.w * 0.015)
    cw, ch = (gw - gap) // 2, (gh - gap) // 2

    for slot in range(1, 5):
        cx = gx + ((slot - 1) % 2) * (cw + gap)
        cy = gy + ((slot - 1) // 2) * (ch + gap)
        pad = next((p for p in pads if p.slot == slot), None)
        buzzing = pad is not None and pad.instance_id == cycle.active
        col = player_color(slot) if pad else DIM

        ui.rect(cx, cy, cw, ch, blend(BG, col, 0.30 if buzzing else 0.10))
        ui.frame(cx, cy, cw, ch,
                 col if pad else blend(BG, col, 0.35),
                 max(3, int(ui.h * (0.007 if buzzing else 0.004))))

        ui.text(f"P{slot}", cx + 18, cy + 12, "head",
                col if pad else blend(BG, DIM, 0.65), bold=True)
        corner, swap_y = cx + cw - int(ch * 0.13), cy + int(ch * 0.13)
        if wiiu:
            # Which Wii U controller this bay becomes, from Kenney's Wii U
            # set: P1 is whichever the game is set to, everyone else a Pro
            # Controller. Drawn on empty bays too, dimmed like their map, so
            # the answer is there before anyone joins.
            kind = wiiu if slot == 1 else "pro"
            bh = ch * 0.15
            bw = bh * (96 / 60 if kind == "gamepad" else 96 / 64)
            ui.image(art("wiiu/" + kind), cx + cw - int(ch * 0.06) - bw,
                     cy + int(ch * 0.13) - bh / 2, bw, bh,
                     color=col if pad else blend(BG, DIM, 0.65))
            # The swap badge stacks under this one rather than beside it, so
            # the corner reads as one column of facts about the pad.
            corner = cx + cw - int(ch * 0.06) - bw / 2
            swap_y = cy + int(ch * 0.13) + bh / 2 + ch * 0.04 + ch * 0.075
        # The swap badge means nothing on a map with no swap gesture: what
        # it would show there is the hardware, not a setting.
        if pad and pad.swap_faces and layout != "playstation":
            # Badged in the corner rather than on the pad itself — there is no
            # room among the buttons, and a non-default mapping deserves to be
            # visible from across the room.
            swap_icon(ui, corner, swap_y, ch * 0.15, col)

        # High in the bay, with the label well under it: at the old spacing
        # the lowest buttons very nearly touched the controller's name.
        top, under = 0.15, 0.13
        if layout == "n64":
            # This map is a tall one — the C cross climbs two and a half
            # buttons above A — so it is given the slack the others leave
            # between the strip and the controller's name.
            top, under = 0.10, 0.11
        # 0.70 of the bay, not 0.62: raising the map left slack between it
        # and the label, and a taller strip makes every glyph bigger on a
        # screen being read from a sofa.
        pw, ph = int(cw * 0.92), int(ch * (0.82 if layout == "n64" else 0.70))
        card_bg = blend(BG, col, 0.30 if buzzing else 0.10)
        draw_gamepad(ui, cx + (cw - pw) / 2, cy + ch * top, pw, ph, col,
                     pad.held if pad else set(), pad.axes if pad else {},
                     card_bg,
                     # One setting for every map, so the screen always draws
                     # exactly what gets written.
                     swap=pad.swap_faces if pad else False,
                     dim=pad is None,
                     holds=holds.get(pad.key) if pad else None,
                     wys=pad_wysiwyg(pad) if pad else None,
                     layout=layout)

        if pad:
            label, lc = pad.label, FG
        else:
            label, lc = "— empty —", blend(BG, DIM, 0.75)
        ui.text(label, cx + cw // 2, cy + ch - int(ch * under), "body", lc,
                center=True)

    wy = int(ui.h * 0.835)
    for w in warnings[:2]:
        ui.text("!  " + w, int(ui.w * 0.05), wy, "small", WARN)
        wy += ui.size["small"] + 6

    p1c, anyone = player_color(1), FG
    # The legend draws the same art the pads do, so a glyph means one thing on
    # the whole screen — which means it changes with the map. Claiming P1 is
    # the exception: the stick presses are preflight's own doing and belong to
    # no emulated pad, so that entry stays as it is in both.
    claim = (["L3", "sep+", "R3"], None, None, "claim P1")
    if layout in ("gamecube", "n64"):
        # This pad has no select button, so quit borrows Start and Z tells
        # the two gestures apart. Either shoulder is Z, so either hand works.
        start_art = "n64:start" if layout == "n64" else "gc:start"
        z_art = "n64:z" if layout == "n64" else "gc:z"
        items = [
            ([start_art], "P1", p1c, "hold to start"),
            claim,
            # The swap is both analog triggers. On the GameCube map those
            # ARE L and R, so their own glyphs say it; an N64 pad has no
            # right trigger to name, so the player's real pad is named
            # instead, as "claim P1" already does.
            (["ZL", "sep+", "ZR"] if layout == "n64"
             else ["gc:l", "sep+", "gc:r"], None, anyone,
             "swap A/B" if layout == "n64" else "swap ABXY"),
            ([z_art, "sep+", start_art], "P1", p1c, "hold to quit"),
        ]
    elif layout == "playstation":
        # The swap entry is here after all. The shapes are positions, but
        # whether SDL's south IS the bottom button depends on how Steam
        # handed the pad over, and that is not readable — so it is offered
        # as a correction: squeeze both triggers until Cross is on the
        # bottom button.
        items = [
            (["ps:start"], "P1", p1c, "hold to start"),
            claim,
            (["ZL", "sep+", "ZR"], None, anyone, "fix my buttons"),
            (["ps:select"], "P1", p1c, "hold to quit"),
        ]
    else:
        items = [
            (["+"], "P1", p1c, "hold to start"),
            claim,
            (["ZL", "sep+", "ZR"], None, anyone, "swap ABXY"),
            (["\u2013"], "P1", p1c, "hold to quit"),
        ]
        if wiiu:
            # Says what P1 IS, not what pressing does: the thing to check
            # before starting is which controller the game will be offered.
            items.insert(3, (["+", "sep+", "\u2013"], "P1", p1c,
                             "is a Wii U Pro Controller" if wiiu == "pro"
                             else "is a Wii U GamePad"))
    glyph_bar(ui, items, hidden={1} if p1_claimed else ())


def message_screen(ui, title, lines, color=BAD):
    draw_frame(ui, title)
    y = int(ui.h * 0.30)
    for line in lines:
        ui.text(line, int(ui.w * 0.05), y, "body", color)
        y += ui.size["body"] + 12
    draw_hint(ui, "B = quit")


# ------------------------------------------------------------------- launch

VIRTUAL_PAD_HINT = "SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD=1"


# Which pad the check screen draws. The map describes the pad the GAME will
# see, not the one in the player's hands, so it follows the emulator: a
# GameCube game gets a GameCube map. Anything not listed gets the Switch one,
# which is also what an unrecognised target falls back to.
BACKEND_LAYOUT = {"dolphin": "gamecube", "wheelwizard": "gamecube",
                  "gopher64": "n64", "duckstation": "playstation"}


# Both triggers, firmly, mirrors the face buttons — on either map. It was the
# two shoulders once, which is wrong on a GameCube pad twice over: the legend
# draws L and R and those ARE the triggers there, and both shoulders together
# are Z, so flipping the mapping as a side effect of pressing Z was a trap.
# The triggers are ZL and ZR on a Switch pad, so one gesture covers both and a
# player who learns it on one screen knows it on the other. Two thresholds, so
# a trigger resting just under the line cannot rattle the mapping back and
# forth.
GC_SWAP_ON = 24000
GC_SWAP_OFF = 8000


def read_axes(sdl, pads, logged):
    """Read every axis straight from SDL, instead of waiting to be told.

    Axis events do arrive, but not always before the first button press: a
    Steam virtual pad logged nothing at all until one, so a trigger squeezed
    before anything else was touched drew nothing and looked ignored. Reading
    the state each frame does not depend on an event ever coming, and it is
    six calls per pad on a screen that repaints only when something changes.

    `logged` remembers which axes have been seen to move, so the log gets one
    line per axis per run — enough to tell whether a control is arriving at
    all, without a line per sample.
    """
    for pad in pads:
        if not pad.handle:
            continue
        for axis in range(6):
            value = sdl.SDL_GameControllerGetAxis(pad.handle, axis)
            dead = AXIS_DEADZONE.get(axis, STICK_DEADZONE)
            pad.axes[axis] = value if abs(value) > dead else 0
            if abs(value) > dead and axis not in logged:
                logged.add(axis)
                print(f"axis: P{pad.slot or '-'} axis={axis} "
                      f"({AXIS_NAMES.get(axis, '?')}) reached {value}",
                      flush=True)


def update_trigger_swap(pads, armed):
    """Flip the face mapping of any pad squeezing both triggers.

    Returns True if anything changed, so the caller can save it. Axes carry
    no press events, so this is judged from the values every frame rather
    than from an SDL button-down.
    """
    changed = False
    for pad in pads:
        key = (pad.key, "trigger-swap")
        low = pad.axes.get(sdlui.AXIS_TRIGGERLEFT, 0)
        high = pad.axes.get(sdlui.AXIS_TRIGGERRIGHT, 0)
        if low > GC_SWAP_ON and high > GC_SWAP_ON:
            if key not in armed:
                armed.add(key)
                pad.swap_faces = not pad.swap_faces
                pad.swap_explicit = True
                changed = True
        elif low < GC_SWAP_OFF or high < GC_SWAP_OFF:
            armed.discard(key)
    return changed


def update_gc_holds(pads, holding, now):
    """Start alone starts the game; a shoulder alongside it quits.

    A GameCube pad has no select button, so the two gestures share Start and
    Z tells them apart. Judged from what is held right NOW, so either order
    works — and, just as importantly, so letting go CLEARS the timer. It did
    not before: the entry outlived the press, and the next Z+Start found a
    timer that had already run down and quit on the first press.

    Authoritative for both gestures on this map, which is why it clears as
    well as arms. The physical Back button still counts as a quit, for a way
    out when a pad's shoulders are being remapped out from under us.
    """
    for pad in pads:
        start = BTN_START in pad.held
        shoulder = BTN_LSHOULDER in pad.held or BTN_RSHOULDER in pad.held
        # Quit is every pad's, as everywhere else; starting is P1's alone.
        wanted = {BTN_BACK: (start and shoulder) or BTN_BACK in pad.held,
                  BTN_START: start and not shoulder and pad.slot == 1}
        for btn, want in wanted.items():
            if want:
                holding.setdefault((pad.key, btn), now)
            else:
                holding.pop((pad.key, btn), None)


def layout_for(backend):
    return BACKEND_LAYOUT.get(backend, "switch")


# What each backend needs in its environment on the far side. All of it is
# about SDL 2.32 and newer, which hide Steam's virtual pads from any process
# Steam did not launch — and a flatpak sandbox strips the environment that
# would say otherwise, so `flatpak run --env=` is the only channel across.
BACKEND_ENV = {
    "dolphin": (VIRTUAL_PAD_HINT,),
    "wheelwizard": (VIRTUAL_PAD_HINT,),
    # Eden needs the pad hint AND the driver pinned: pinned so the ids and
    # ports preflight just wrote are the ones it computes, since the same pad
    # enumerates in a different order with a different guid otherwise.
    #
    # The hint matters for its flatpak in particular. The AppImage links SDL
    # statically and inherits our environment, but the flatpak uses the KDE
    # runtime's SDL 2.32 behind a sandbox — the Dolphin situation exactly, and
    # it cost an evening there.
    "eden": (VIRTUAL_PAD_HINT, EDEN_NO_HIDAPI),
    # Cemu's flatpak links the freedesktop runtime's SDL 2.32: same story.
    "cemu": (VIRTUAL_PAD_HINT,),
    # gopher64 links SDL3 statically, which hides the virtual pads just the
    # same; its own CLI is given the hint the same way (gopher_run).
    "gopher64": (VIRTUAL_PAD_HINT,),
    # An AppImage inherits our environment, but the hint costs nothing and
    # the day it ships as a flatpak this is the line that saves an evening.
    "duckstation": (VIRTUAL_PAD_HINT,),
}


def prepare_command(cmd, backend):
    """Add whatever the backend needs to see the controllers we set up.

    Logged by launch(), because it is not the command the shortcut passed in.
    """
    wanted = BACKEND_ENV.get(backend)
    if not wanted or not cmd:
        return cmd
    if os.path.basename(cmd[0]) == "flatpak" and len(cmd) > 1 and cmd[1] == "run":
        add = [f"--env={entry}" for entry in wanted
               if not any(a.startswith("--env=" + entry.partition("=")[0])
                          for a in cmd)]
        return cmd[:2] + add + cmd[2:]
    # A native or AppImage build inherits our environment directly.
    for entry in wanted:
        key, _, value = entry.partition("=")
        os.environ[key] = value
    return cmd


def launch(cmd, dry_run):
    # Logged because it is not the command the shortcut passed in: fixups get
    # added on the way through, and when a game misbehaves this is the first
    # thing worth knowing.
    print("exec:", " ".join(cmd), flush=True)
    if dry_run:
        return
    # Replacing this process rather than spawning keeps the shell pipeline in
    # preflight.sh alive for as long as the emulator is, which is how Steam
    # knows the shortcut is still running.
    os.execvp(cmd[0], cmd)


def game_expectation(rom):
    if not rom:
        return None
    games = load_json(user_file("games.json"), {})
    base = os.path.basename(rom)
    for k, v in games.items():
        if k == base or k in base:
            return v.get("players")
    return None


# --------------------------------------------------------------------- main

def main():
    flags, positional, cmd = split_command(sys.argv[1:])
    if "--version" in flags:
        print(f"preflight {VERSION}")
        return 0
    dry_run = "--dry-run" in flags

    if cmd:
        # Told exactly what to run. The emulator comes out of the command.
        target = command_target(cmd)
        backend = backend_for(target)
        rom = command_rom(cmd)
    else:
        # No command: the historical form, a bare ROM path, always Ryujinx.
        rom = positional[0] if positional else None
        if rom and not os.path.exists(rom):
            print(f"ROM not found: {rom}", file=sys.stderr)
        target = find_app_id()
        backend = "ryujinx"
        cmd = ["flatpak", "run", target, "-f"] + ([rom] if rom else [])

    exe = command_exe(cmd)
    app_id = None if exe else target
    print(f"target: {target or 'unknown'}; backend: {backend or 'none'}"
          f"{'; exe: ' + exe if exe else ''}", flush=True)

    adopt_user_files()
    apply_theme()

    sdl, ttf = sdlui.load_libraries()
    sdlui.set_preinit_hints(sdl)
    if sdl.SDL_Init(sdlui.SDL_INIT_VIDEO | sdlui.SDL_INIT_JOYSTICK
                    | sdlui.SDL_INIT_GAMECONTROLLER) != 0:
        sys.exit(f"SDL_Init: {sdl.SDL_GetError().decode()}")

    ui = UI(sdl, ttf)
    print(f"window ready: {ui.w}x{ui.h}", flush=True)
    known = load_json(KNOWN_PADS, {})
    if backend in ("dolphin", "wheelwizard"):
        # Wheel Wizard is a native launcher whose child Dolphin uses the
        # normal Dolphin Flatpak config, not a config beside WheelWizard.
        #
        # Writing beside WheelWizard instead is not merely redundant, it
        # breaks the game outright: WheelWizard creates
        # config-dolphin-emu/dolphin-emu as a relative *symlink* to this
        # very config folder on every launch (FileHelper.
        # EnsureRelativeSymlink, from DolphinLaunchHelper.LaunchDolphin),
        # and refuses to start if a real directory is sitting there:
        #   "Should have created a symlink at '.../config-dolphin-emu/
        #    dolphin-emu', but a directory already existed at this path!"
        # Since that symlink resolves here anyway, this path reaches the
        # bundled Dolphin all the same.
        dolphin_app_id = DOLPHIN_APP_ID if backend == "wheelwizard" else app_id
        dolphin_exe = None if backend == "wheelwizard" else exe
        cfg_path = find_dolphin_config(dolphin_app_id, dolphin_exe) or \
            dolphin_config_target(dolphin_app_id, dolphin_exe)
    elif backend == "eden":
        cfg_path = eden_config_target(app_id, exe)
    elif backend == "cemu":
        cfg_path = cemu_config_target(app_id, exe)
    elif backend == "gopher64":
        cfg_path = find_gopher_config(app_id, exe)
    elif backend == "duckstation":
        cfg_path = find_duck_config(exe)
    elif backend == "ryujinx":
        cfg_path = find_config(app_id, exe)
    else:
        cfg_path = None
    needed = game_expectation(rom)
    p1_pro = backend == "cemu" and cemu_p1_pro(rom)

    slots = new_slot_state()
    pads, unmapped = scan_pads(sdl)
    apply_known(pads, known)
    pads = resolve_slots(pads, slots)
    # Only Ryujinx keeps bindings we did not write; Dolphin's are replaced
    # wholesale every time, so there is nothing to inspect for gaps.
    binding_gaps = config_binding_gaps(cfg_path) if backend == "ryujinx" else []
    print(f"{len(pads)} pad(s), {len(unmapped)} unmapped, "
          f"{len(binding_gaps)} binding gap(s); entering loop", flush=True)
    label_pads(pads)
    log_pads(pads, "scan")
    log_layouts(pads)

    state = "roster"
    claimed_p1 = None       # key of the pad that took P1; one claim per session
    last_sig = None         # what was last painted, so we can skip redraws
    result = None
    cycle = RumbleCycle(sdl)
    reals = RealWatcher()
    if reals.open():
        for _info in sorted(reals.fds.values(), key=lambda i: i["path"]):
            print(f"watch: {_info['path']} {_info['name']} "
                  f"[{_info['vendor']:04x}:{_info['product']:04x}] "
                  f"{'hidraw' if _info.get('hid') else 'evdev'}", flush=True)
    if not reals.available:
        print("note: no physical pads visible yet; will look again as pads "
              "wake", flush=True)
    HOLD_MS = 1100
    PRESS_LOG_LIMIT = 60
    holding = {}            # (pad key, button) -> tick the hold began
    armed = set()           # pads whose trigger squeeze has already fired
    # What the pad actually sent, which is not always what is printed on it:
    # a remapping layer between the two shows up here and nowhere else.
    # Capped, because a family testing every button would otherwise fill the
    # log with the one thing it already proved.
    presses_logged = [0]
    axes_logged = set()
    last_press = {}         # pad key -> tick, for the pairing guard above
    pairing_logged = [0]

    def rescan():
        """Rebuild the pad list, preserving what each pad was doing."""
        reals.refresh()          # a pad that just woke has a new evdev node
        was = {p.key: (p.held, p.axes) for p in pads}
        for p in pads:
            p.close()
        fresh, missing = scan_pads(sdl)
        apply_known(fresh, known)
        fresh = resolve_slots(fresh, slots, claimed_p1)
        label_pads(fresh)
        for p in fresh:
            if p.key in was:
                p.held, p.axes = was[p.key]
        return fresh, missing

    while True:
        now = sdl.SDL_GetTicks()
        reals.poll(now)

        if state == "roster":
            if any(not p.attached() for p in pads):
                pads, unmapped = rescan()

            warnings = []
            # Eden only receives input when Steam Input is on, for reasons
            # not yet understood (PLAN §7). Nothing we write helps, so the
            # honest thing is to say so before the game starts rather than
            # leave four people prodding dead controllers.
            alert = None
            if backend == "eden" and pads and steam_input_off(pads):
                alert = "Turn Steam Input ON for this game \u2014 Eden gets no input without it"
            # Say who we are still waiting on, by bay, rather than leaving
            # the screen looking finished while the pads are anonymous. A
            # pad only becomes itself when somebody presses a button on it,
            # so the one thing worth showing is which one to press.
            waiting = [p for p in pads
                       if p.slot and p.real is None
                       and (p.vendor, p.product) == STEAM_VIRTUAL]
            spin = 0
            if alert is None and waiting and reals.available:
                spin = int(now / 400) % 4
                who = ", ".join(f"P{p.slot}" for p in sorted(
                    waiting, key=lambda q: q.slot))
                alert = ("Identifying controllers" + "." * spin
                         + f"  press any button on {who}")
            if backend is None:
                warnings.append(f"No controller-config backend for "
                                f"{target or 'this command'} — the check runs, "
                                f"but no bindings will be written.")
            elif not cfg_path:
                warnings.append(
                    "Eden's qt-config.ini not found — cannot write."
                    if backend == "eden" else
                    "Dolphin's config folder not found — cannot write."
                    if backend in ("dolphin", "wheelwizard")
                    else "Ryujinx Config.json not found — cannot write.")
            if binding_gaps:
                warnings.append(f"Ryujinx's saved controller settings are "
                                f"missing {len(binding_gaps)} binding(s) "
                                f"({binding_gaps[0]}) — will be repaired.")
            # There is deliberately no warning about an unidentified pad.
            # It was added when identification needed a press, and it blinked
            # — appearing and clearing as pads were matched — which is worse
            # than useless on a screen someone is trying to read. Elimination
            # now covers that case, and what it cannot cover is in the log.

            for name in unmapped:
                warnings.append(f"{name}: SDL has no mapping for this pad, "
                                "so it cannot be used.")
            if not pads:
                warnings.append("No controllers detected. Wake one and it "
                                "will appear here.")

            # pair_by_elimination is gone. It fired before anyone had
            # pressed anything and bound the one unidentified pad to the one
            # unaccounted-for device — which is only sound if both counts
            # are right, and they were not: it bound an 8bitdo's pad to an
            # Xbox controller on one run and the other way round on the
            # next, crossing the labels and the face mapping with them.
            # A press on hidraw now identifies a pad properly, so a guess
            # has nothing left to buy.

            read_axes(sdl, pads, axes_logged)
            # No swap gesture on a map that cannot swap: a PlayStation pad's
            # shapes are positions, so the two triggers would flip a setting
            # with nothing to act on — and a pad carries that setting across
            # to the other emulators, where it very much does act.
            # Including the PlayStation screen. The shapes are positions,
            # but Steam hands some pads to SDL by LABEL — an 8Bitdo SF30
            # Pro's printed A arrives as SDL's A although it sits on the
            # east — so nothing preflight reads says where a button
            # physically is. Three rounds of inferring it put the mirror on
            # the wrong pad. The person holding the controller can see the
            # answer in one glance, so let them say it.
            if update_trigger_swap(pads, armed):
                remember(pads, known)
            # Both of these pads quit through Z+Start: neither has a
            # second button to spare for it.
            if layout_for(backend) in ("gamecube", "n64"):
                update_gc_holds(pads, holding, now)

            holds = {}
            for (pkey, btn), started in list(holding.items()):
                frac = min(1.0, (now - started) / HOLD_MS)
                holds.setdefault(pkey, {})[btn] = frac
                if frac >= 1.0:
                    holding.pop((pkey, btn), None)
                    if btn == BTN_BACK:
                        state = "exit"
                    elif btn == BTN_START:
                        state = "commit"

            cycle.update(pads)
            # Repaint only on change. The screen is static most of the time
            # and every circle is scanline-filled by hand, so redrawing at
            # 60 Hz burned real CPU for no visible difference.
            sig = (tuple((p.slot, p.instance_id, frozenset(p.held),
                          tuple(sorted(p.axes.items())), p.swap_faces)
                         for p in pads),
                   cycle.active,
                   tuple((k, tuple(sorted(v.items()))) for k, v in sorted(holds.items())),
                   tuple(warnings), claimed_p1, alert, p1_pro, spin)
            if sig != last_sig:
                last_sig = sig
                draw_pad_grid(ui, pads, cycle, warnings, needed, holds,
                              claimed_p1 is not None, alert,
                              layout=layout_for(backend),
                              wiiu=("pro" if p1_pro else "gamepad")
                              if backend == "cemu" else None)
                ui.present()

        elif state == "error":
            if last_sig != "error":
                last_sig = "error"
                message_screen(ui, "Not launching", result)
                ui.present()

        for kind, payload in ui.poll():
            if kind == "quit":
                state = "exit"
            elif kind == "key" and payload == SDLK_ESCAPE:
                state = "exit"
            elif kind == "devices":
                pads, unmapped = rescan()
                if state != "error":
                    state = "roster"
            elif kind == "release":
                inst, btn = payload
                for p in pads:
                    if p.instance_id == inst:
                        p.held.discard(btn)
                        # The swap gesture disarms on its own, from the axis
                        # values: nothing to clear here for it.
                        holding.pop((p.key, btn), None)
            elif kind == "button":
                inst, btn = payload
                pad = next((p for p in pads if p.instance_id == inst), None)
                if pad is None:
                    continue
                pad.held.add(btn)
                if presses_logged[0] < PRESS_LOG_LIMIT:
                    presses_logged[0] += 1
                    print(f"press: P{pad.slot or '-'} btn={btn} "
                          f"({BUTTON_NAMES.get(btn, '?')})", flush=True)

                # Same press lands on the hidden physical pad too; pairing
                # them is what turns "Steam pad f679" into a real controller.
                #
                # Two guards, both bought the hard way. A Steam Controller
                # never gets one: Steam holds it at hidraw level, so it has
                # no kernel node at all and anything it paired with would be
                # somebody else's pad — it took an 8bitdo's, wore its name,
                # and inherited its button layout with it.
                #
                # And nobody pairs while another pad is also being pressed:
                # a node that fired 200ms ago belongs to whoever pressed it,
                # not to the next pad to ask. Waiting for a quiet press costs
                # a second and cannot mis-pair.
                quiet = all(t is None or now - t > reals.WINDOW_MS
                            for k, t in last_press.items() if k != pad.key)
                # No Steam Controller exception any more. That guard existed
                # because Steam holds it at hidraw level and it has no
                # working kernel node — but hidraw is now the channel
                # pairing reads, so it identifies itself like anything else.
                if (pad.real is None
                        and quiet
                        and (pad.vendor, pad.product) == STEAM_VIRTUAL):
                    taken = {dev_ident(q.real) for q in pads if q.real}
                    hit = reals.claim(now, taken)
                    if hit:
                        bind_real(pad, hit, known, pads)
                        label_pads(pads)
                        # A successful match used to log nothing, which made
                        # a wrong one invisible: the only trace was another
                        # pad reporting "already claimed" against a device
                        # it could not name.
                        print(f"pair: P{pad.slot or '-'} is {hit['name']} "
                              f"[{hit['vendor']:04x}:{hit['product']:04x}] "
                              f"via {'hidraw' if hit.get('hid') else 'evdev'}"
                              f" — Steam calls it '{pad.gc_name or pad.name}'",
                              flush=True)
                    if not hit and pairing_logged[0] < 12:
                        # Say when a press went by without identifying the
                        # pad: silence here reads as "nothing to see", and
                        # what it actually means is that this pad's face
                        # buttons are about to be a guess.
                        pairing_logged[0] += 1
                        # Name the devices that stirred, not just how many.
                        # Zero means the channel is dead for this pad; more
                        # than one means something is chattering on its own
                        # — a drifting stick reports for ever — and that is
                        # the difference between "cannot see it" and "cannot
                        # tell them apart".
                        stirring = ", ".join(sorted(
                            {i["name"] for _, i in reals.recent})) or "nothing"
                        print(f"pair: no device matched P{pad.slot or '-'} "
                              f"{pad.display} (stirring: {stirring}; "
                              f"{len(taken)} already claimed)", flush=True)
                last_press[pad.key] = now

                if state == "error":
                    if btn == BTN_B:
                        state = "exit"
                    continue

                # Claim Player 1. Deliberately one-shot: without the lock a
                # second player could keep taking the slot back, which is
                # exactly the game a sibling will play.
                if (not claimed_p1 and btn in (BTN_LSTICK, BTN_RSTICK)
                        and BTN_LSTICK in pad.held and BTN_RSTICK in pad.held):
                    claimed_p1 = pad.key
                    pads = resolve_slots(pads, slots, claimed_p1)
                    label_pads(pads)
                    continue

                # + and - together: P1's Wii U controller type, on Cemu.
                # Both holds are cancelled whoever does it — the two buttons
                # are also start and quit, and pressing the pair must never
                # set either off. The one still held after the other is let
                # go does not re-arm: only a fresh press does.
                if (backend == "cemu" and btn in (BTN_START, BTN_BACK)
                        and BTN_START in pad.held and BTN_BACK in pad.held):
                    holding.pop((pad.key, BTN_START), None)
                    holding.pop((pad.key, BTN_BACK), None)
                    if pad.slot == 1:
                        p1_pro = not p1_pro
                        set_cemu_p1_pro(rom, p1_pro)
                        print(f"cemu: P1 is now a "
                              f"{'Pro Controller' if p1_pro else 'GamePad'}",
                              flush=True)
                    continue

                # Exit is available to every pad on purpose. Gating it to P1
                # meant that if P1's controller slept, or someone else held
                # it, nobody on the sofa could close the tool at all.
                if btn == BTN_BACK:
                    holding[(pad.key, BTN_BACK)] = now
                    continue

                if pad.slot != 1:
                    continue

                # P1 only from here, and by holding rather than tapping, so
                # everyone can mash buttons to test them without setting
                # anything off.
                if btn == BTN_START:
                    holding[(pad.key, btn)] = now


        if state == "exit":
            reals.close()
            for p in pads:
                p.close()
            ui.close()
            sdl.SDL_Quit()
            return 1

        if state == "commit":
            if backend is None:
                remember(pads, known)   # identities are still worth keeping
                break
            if not cfg_path:
                result, state = ["Config for this emulator was not found."], "error"
                continue
            remember(pads, known)
            log_pads(pads, "writing")
            log_layouts(pads)
            if backend == "eden":
                problems = write_eden_config(cfg_path, pads, sdl)
            elif backend in ("dolphin", "wheelwizard"):
                problems = write_dolphin_config(cfg_path, pads)
            elif backend == "cemu":
                problems = write_cemu_config(cfg_path, pads, p1_pro)
            elif backend == "gopher64":
                problems = write_gopher_config(cfg_path, pads, app_id, exe)
            elif backend == "duckstation":
                problems = write_duck_config(cfg_path, pads, exe)
            else:
                problems = write_config(cfg_path, pads, exe)
            if problems:
                result, state = problems, "error"
                continue
            break

        if state == "exit":
            reals.close()
            for p in pads:
                p.close()
            ui.close()
            sdl.SDL_Quit()
            return 1

        sdl.SDL_Delay(16)

    reals.close()
    for p in pads:
        p.close()
    ui.close()
    sdl.SDL_Quit()
    cmd = prepare_command(cmd, backend)
    launch(cmd, dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
