#!/usr/bin/env python3
"""
preflight — controller gate for Ryubing (Ryujinx) on SteamOS.

Confirms which physical controller is which player, verifies the face-button
labelling, writes Ryujinx's Config.json, then launches the game.

    preflight.py "/path/to/Game.nsp"     # check, then launch that ROM
    preflight.py                         # check, then open the Ryujinx list
    preflight.py --dry-run "<rom>"       # everything except writing/launching
    preflight.py --version               # print the version and exit

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

# Names Steam gives a virtual pad when it isn't telling us what's behind it.
GENERIC_VIRTUAL = ("x-box 360 pad", "steam virtual gamepad", "xbox 360 controller")


def label_pads(pads):
    """Resolve display names with the whole set in view.

    Under Steam Input every pad is a Steam virtual pad sharing one
    vendor/product, so the vendor tables cannot name them. Steam often does
    put the real device's name on the virtual pad — "Steam Controller",
    "Xbox One controller" — so that is used when it is informative, and a
    short slot tag when it is not.

    The CRC is NOT a durable identity for a physical controller: it tracks the
    virtual pad slot, and Steam moves devices between slots. It is reliable
    within one session, which is all the config write needs.
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
            name = (p.name or "").strip()
            generic = not name or any(g in name.lower() for g in GENERIC_VIRTUAL)
            p.display = f"Steam pad {p.name_crc:04x}" if generic else name
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

        devpath = None
        if hasattr(sdl, "SDL_JoystickPathForIndex"):
            p = sdl.SDL_JoystickPathForIndex(index)
            devpath = p.decode(errors="replace") if p else None
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


class RealWatcher:
    """Correlates presses on the hidden physical pads with the virtual ones.

    Steam's virtual pad carries nothing that points back at the hardware
    driving it. But a button press fires on both within milliseconds, so
    watching the real evdev nodes alongside SDL tells us which is which — and
    hands back the real MAC, which is what makes settings stick across
    sessions.
    """

    WINDOW_MS = 250

    def __init__(self):
        self.fds = {}
        self.recent = []
        self.available = False

    def open(self):
        for info in scan_real_gamepads():
            try:
                self.fds[os.open(info["path"], os.O_RDONLY | os.O_NONBLOCK)] = info
            except OSError:
                pass
        self.available = bool(self.fds)
        return self.available

    def poll(self, now):
        if not self.fds:
            return
        try:
            ready, _, _ = select.select(list(self.fds), [], [], 0)
        except (OSError, ValueError):
            return
        for fd in ready:
            try:
                data = os.read(fd, 24 * 64)
            except OSError:
                continue
            for off in range(0, len(data) - 23, 24):
                _, _, etype, _, value = struct.unpack_from("qqHHi", data, off)
                if etype == 1 and value == 1:       # EV_KEY press
                    self.recent.append((now, self.fds[fd]))
        self.recent = [(t, i) for t, i in self.recent
                       if now - t <= self.WINDOW_MS]

    def claim(self, now, taken):
        """The single unclaimed real device that just fired, if unambiguous.

        Two people pressing at the same instant would make the pairing a
        guess, so that case is skipped rather than risking a wrong label.
        """
        hits = [i for t, i in self.recent
                if now - t <= self.WINDOW_MS and i["path"] not in taken]
        if not hits:
            return None
        paths = {i["path"] for i in hits}
        return hits[-1] if len(paths) == 1 else None

    def close(self):
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds.clear()


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
            p.swap_faces = (bool(rec.get("swap_faces"))
                            if rec.get("schema", 0) >= 3
                            and is_hardware_key(p.store_key) else False)



def remember(pads, known):
    for p in pads:
        if p.slot:
            known[p.store_key] = {
                "schema": 3,
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
                "swap_faces": p.swap_faces if is_hardware_key(p.store_key) else False,
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
        targets = [p for p in sorted(pads, key=lambda q: q.slot)
                   if p.slot and p.attached()]
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
        pad.swap_faces = (bool(rec.get("swap_faces"))
                          if rec.get("schema", 0) >= 3
                          and is_hardware_key(pad.store_key) else False)
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


def find_emulator_sdl():
    """Path to the libSDL2 Ryujinx actually links against.

    This matters more than it looks. SDL changed the bus type it reports for
    Bluetooth pads between 2.30 and 2.32, which lands in the GUID — the same
    Stadia pad is ...-0000-0005-... under the system SDL and ...-0000-0003-...
    under Ryujinx's bundled 2.30. Ids computed with the wrong SDL never match
    and Ryujinx logs "No matching controllers found" while every pad works
    perfectly in this tool.
    """
    import glob
    for root in ("/var/lib/flatpak/app", os.path.expanduser("~/.local/share/flatpak/app")):
        for path in glob.glob(f"{root}/*yu*/*/*/*/files/bin/libSDL2*.so*"):
            return path
    return None


def emulator_gamepads():
    """(index, guid_hex, name) as Ryujinx's own SDL will see them.

    Run in a throwaway subprocess: two libSDL2 builds share a SONAME, so
    loading both in one process gets us whichever landed first — the UI keeps
    the system SDL, this borrows the emulator's.
    """
    lib = find_emulator_sdl()
    if not lib:
        return None
    try:
        out = subprocess.run([sys.executable, "-c", EMU_ENUM, lib],
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


def find_config(app_id):
    for path in (os.path.expanduser(f"~/.var/app/{app_id}/config/Ryujinx/Config.json"),
                 os.path.expanduser(
                     f"~/.var/app/{DEFAULT_APP_ID}/config/Ryujinx/Config.json"),
                 os.path.expanduser("~/.config/Ryujinx/Config.json")):
        if os.path.isfile(path):
            return path
    return None


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
    "motion": {"slot": 0, "alt_slot": 0, "mirror_input": False,
               "dsu_server_host": None, "dsu_server_port": 0,
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


def write_config(cfg_path, pads):
    data = load_json(cfg_path, None)
    if data is None:
        return ["cannot read Config.json"]

    rows = emulator_gamepads()
    if rows is None:
        problems_pre = ["Could not read the emulator's own SDL — ids may not "
                        "match. Is Ryujinx installed as a flatpak?"]
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


PAD_ASPECT = 2.4        # a wide strip; the bays are much wider than they are tall


def draw_gamepad(ui, bx, by, bw, bh, col, held, axes, bg, swap=False,
                 dim=False, holds=None):
    """A button map — no controller body.

    Drawing a shape means picking *a* shape, and every real pad is a different
    one; the outline ends up either wrong for most players or so generic it
    says nothing. Only the inputs matter here, so only the inputs are drawn,
    laid out where a hand expects them. Everything is dim until pressed.

    Sizes are fractions of the box HEIGHT, not width — height is what the bay
    actually limits, so this keeps the controls a sane size at any aspect.
    """
    dw, dh = bw, bw / PAD_ASPECT
    if dh > bh:
        dw, dh = bh * PAD_ASPECT, bh
    ox, oy = bx + (bw - dw) / 2, by + (bh - dh) / 2

    def X(u):
        return ox + u * dw

    def Y(v):
        return oy + v * dh

    def S(u):
        return max(2.0, u * dh)

    idle = blend(bg, FG, 0.20 if dim else 0.32)
    lit = blend(bg, FG, 0.38) if dim else col
    label_col = blend(bg, FG, 0.62)
    on = held.__contains__

    def pad_text(text, cx, cy, color):
        ui.glyph_centered(text, cx, cy, "small", color) if len(text) == 1 else \
            ui.text(text, cx, cy - ui.text_size(text, "small", True)[1] / 2,
                    "small", color, bold=True, center=True)

    # Top strip: triggers and shoulders in the outer corners, minus/plus in
    # the middle. Kept in one row so the columns below stay clear.
    for xf, label, btn, axis in ((0.075, "ZL", None, 4),
                                 (0.215, "L", BTN_LSHOULDER, None),
                                 (0.785, "R", BTN_RSHOULDER, None),
                                 (0.925, "ZR", None, 5)):
        active = on(btn) if btn is not None else axes.get(axis, 0) > 8000
        w, h = S(0.25), S(0.165)
        ui.round_rect(X(xf) - w / 2, Y(0.085) - h / 2, w, h, h / 2,
                      lit if active else idle)
        pad_text(label, X(xf), Y(0.085), bg if active else label_col)

    for xf, btn, label in ((0.405, BTN_BACK, "\u2013"), (0.595, BTN_START, "+")):
        active = on(btn)
        ui.fill_circle(X(xf), Y(0.085), S(0.072), lit if active else idle)
        pad_text(label, X(xf), Y(0.085), bg if active else label_col)
        held_for = (holds or {}).get(btn, 0.0)
        if held_for > 0:
            hold_ring(ui, X(xf), Y(0.085), S(0.135), held_for, lit,
                      blend(bg, FG, 0.18))

    # Left column: d-pad. Right column: face buttons. Sticks sit between them.
    dcx, dcy = X(0.135), Y(0.62)
    # Equal arm and stem make a symmetrical cross; square corners keep the
    # joins seamless, where rounded ones left notches at the centre.
    cell = S(0.150)
    # Snap every edge to the same integers the neighbouring cell uses.
    # Computing each arm independently let rounding open a one-pixel seam,
    # which showed up as a gap on the right arm only.
    x0, x1 = round(dcx - cell / 2), round(dcx + cell / 2)
    y0, y1 = round(dcy - cell / 2), round(dcy + cell / 2)
    span = x1 - x0
    ui.rect(x0, y0, span, y1 - y0, idle)
    for btn, rx, ry, rw, rh in (
            (BTN_DPAD_UP, x0, y0 - span, span, span),
            (BTN_DPAD_DOWN, x0, y1, span, span),
            (BTN_DPAD_LEFT, x0 - span, y0, span, y1 - y0),
            (BTN_DPAD_RIGHT, x1, y0, span, y1 - y0)):
        ui.rect(rx, ry, rw, rh, lit if on(btn) else idle)

    # Face buttons are drawn in the Switch's arrangement — X top, Y left,
    # A right, B bottom — and each lights by NAME, not by position. Press the
    # button marked A on any pad and the circle marked A lights, whatever
    # corner it physically lives in. Location accuracy is the thing being
    # traded away, deliberately.
    mirror = {"A": "B", "B": "A", "X": "Y", "Y": "X"}
    live = set()
    for btn in (BTN_A, BTN_B, BTN_X, BTN_Y):
        if on(btn):
            name = BUTTON_NAMES[btn]
            live.add(mirror[name] if swap else name)

    fcx, fcy, spread, br = X(0.855), Y(0.62), S(0.205), S(0.112)
    for letter, dx, dy in (("X", 0, -1), ("Y", -1, 0), ("A", 1, 0), ("B", 0, 1)):
        cx, cy = fcx + dx * spread, fcy + dy * spread
        active = letter in live
        ui.fill_circle(cx, cy, br, lit if active else idle)
        pad_text(letter, cx, cy, bg if active else label_col)

    # sticks: a dim well with a knob that actually moves
    for xf, btn, ax, ay in ((0.360, BTN_LSTICK, 0, 1), (0.585, BTN_RSTICK, 2, 3)):
        cx, cy = X(xf), Y(0.645)
        well = S(0.150)
        ui.fill_circle(cx, cy, well, idle)
        kx = (axes.get(ax, 0) / 32768.0) * well * 0.45
        ky = (axes.get(ay, 0) / 32768.0) * well * 0.45
        moved = abs(kx) + abs(ky) > 1.5
        ui.fill_circle(cx + kx, cy + ky, S(0.088),
                       lit if (moved or on(btn)) else blend(bg, FG, 0.48))


def draw_frame(ui, title, subtitle=None, emoji=None):
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
    if subtitle:
        # Step down a size rather than run off the edge on a long line.
        room = int(ui.w * 0.90)
        size = "body" if ui.text_size(subtitle, "body")[0] <= room else "small"
        ui.text(subtitle, int(ui.w * 0.05), y + title_h + 4, size, DIM)


def draw_hint(ui, hint):
    ui.text(hint, ui.w // 2, int(ui.h * 0.91), "body", DIM, center=True)


def glyph_bar(ui, items):
    """The legend along the bottom, drawn with the same shapes as the pads.

    Each entry is (glyphs, token, token_colour, label). The glyphs stay
    neutral — colour on the button itself would imply only that player may
    press it — and only the player token in the text is tinted. Steps down a
    size rather than running off the edge, exactly as the subtitle does.
    """
    ROUND = {"+", "\u2013", "A", "B", "X", "Y"}

    def measure(size):
        _, th = ui.text_size("Ag", size)
        pill_h = int(th * 1.45)

        def glyph_w(g):
            if g.startswith("sep"):
                return ui.text_size(g[3:], size, True)[0] + pill_h * 0.20
            tw, _ = ui.text_size(g, size, True)
            return max(pill_h, tw + pill_h * (0.55 if g in ROUND else 0.85))

        spacing = pill_h * 0.26
        gap = pill_h * 0.80

        def label_w(token, label):
            w = ui.text_size(label, size)[0]
            if token:
                w += ui.text_size(token, size, True)[0] + pill_h * 0.26
            return w

        widths = [sum(glyph_w(g) + spacing for g in gl) + label_w(tok, lbl)
                  for gl, tok, _, lbl in items]
        total = sum(widths) + gap * (len(items) - 1)
        return pill_h, glyph_w, spacing, gap, widths, total

    size = "body"
    pill_h, glyph_w, spacing, gap, widths, total = measure(size)
    if total > ui.w * 0.94:
        size = "small"
        pill_h, glyph_w, spacing, gap, widths, total = measure(size)

    x = (ui.w - total) / 2
    y = int(ui.h * 0.90)
    for (glyphs, token, tcol, label), w in zip(items, widths):
        for g in glyphs:
            gw = glyph_w(g)
            shell = blend(BG, FG, 0.20)
            if g.startswith("sep"):
                # A bare joiner, not a button: no pill, so "L3 + R3" reads as
                # one combo rather than three separate things to press.
                _, sth = ui.text_size(g[3:], size, True)
                ui.text(g[3:], x + gw / 2, y + (pill_h - sth) / 2, size, DIM,
                        bold=True, center=True)
            else:
                if g in ROUND:
                    ui.fill_circle(x + gw / 2, y + pill_h / 2, pill_h / 2, shell)
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
            ui.text(token, x, y + (pill_h - tht) / 2, size, tcol, bold=True)
            x += tw + pill_h * 0.26
        _, lh = ui.text_size(label, size)
        ui.text(label, x, y + (pill_h - lh) / 2, size, DIM)
        x += ui.text_size(label, size)[0] + gap


def draw_pad_grid(ui, pads, cycle, warnings, needed, holds, p1_claimed):
    draw_frame(ui, "Controller check",
               "Controllers must be paired in your OS first \u2014 "
               "test your inputs before the game starts", emoji="\U0001F6A7")

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
        if pad and pad.swap_faces:
            # Badged in the corner rather than on the pad itself — there is no
            # room among the buttons, and a non-default mapping deserves to be
            # visible from across the room.
            swap_icon(ui, cx + cw - int(ch * 0.13), cy + int(ch * 0.13),
                      ch * 0.15, col)

        pw, ph = int(cw * 0.92), int(ch * 0.62)
        card_bg = blend(BG, col, 0.30 if buzzing else 0.10)
        draw_gamepad(ui, cx + (cw - pw) / 2, cy + ch * 0.20, pw, ph, col,
                     pad.held if pad else set(), pad.axes if pad else {},
                     card_bg, swap=pad.swap_faces if pad else False,
                     dim=pad is None,
                     holds=holds.get(pad.key) if pad else None)

        if pad:
            label, lc = pad.label, FG
        else:
            label, lc = "— empty —", blend(BG, DIM, 0.75)
        ui.text(label, cx + cw // 2, cy + ch - int(ch * 0.17), "body", lc,
                center=True)

    wy = int(ui.h * 0.835)
    for w in warnings[:2]:
        ui.text("!  " + w, int(ui.w * 0.05), wy, "small", WARN)
        wy += ui.size["small"] + 6

    p1c, anyone = player_color(1), FG
    dimmed = blend(BG, DIM, 0.55)
    glyph_bar(ui, [
        (["+"], "P1", p1c, "hold to start"),
        (["\u2013"], "P1", p1c, "hold to exit"),
        (["L3", "sep+", "R3"], None, None, "claim P1"),
        (["L", "R"], None, anyone, "swap A/B"),
    ] if not p1_claimed else [
        (["+"], "P1", p1c, "hold to start"),
        (["\u2013"], "P1", p1c, "hold to exit"),
        (["L", "R"], None, anyone, "swap A/B"),
    ])


def message_screen(ui, title, lines, color=BAD):
    draw_frame(ui, title)
    y = int(ui.h * 0.30)
    for line in lines:
        ui.text(line, int(ui.w * 0.05), y, "body", color)
        y += ui.size["body"] + 12
    draw_hint(ui, "B = quit")


# ------------------------------------------------------------------- launch

def launch(app_id, rom, dry_run):
    args = ["flatpak", "run", app_id, "-f"]
    if rom:
        args.append(rom)
    if dry_run:
        print("would exec:", " ".join(args))
        return
    os.execvp("flatpak", args)


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
    args = [a for a in sys.argv[1:]]
    if "--version" in args:
        print(f"preflight {VERSION}")
        return 0
    dry_run = "--dry-run" in args
    args = [a for a in args if not a.startswith("--")]
    rom = args[0] if args else None

    if rom and not os.path.exists(rom):
        print(f"ROM not found: {rom}", file=sys.stderr)

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
    app_id = find_app_id()
    cfg_path = find_config(app_id)
    needed = game_expectation(rom)

    slots = new_slot_state()
    pads, unmapped = scan_pads(sdl)
    apply_known(pads, known)
    pads = resolve_slots(pads, slots)
    binding_gaps = config_binding_gaps(cfg_path)
    print(f"{len(pads)} pad(s), {len(unmapped)} unmapped, "
          f"{len(binding_gaps)} binding gap(s); entering loop", flush=True)
    label_pads(pads)

    state = "roster"
    claimed_p1 = None       # key of the pad that took P1; one claim per session
    last_sig = None         # what was last painted, so we can skip redraws
    result = None
    cycle = RumbleCycle(sdl)
    reals = RealWatcher()
    if not reals.open():
        print("note: cannot read /dev/input directly; virtual pads will not "
              "be matched to their hardware", flush=True)
    HOLD_MS = 1100
    holding = {}            # (pad key, button) -> tick the hold began
    combo_armed = set()     # pads whose L+R has already fired this press

    def rescan():
        """Rebuild the pad list, preserving what each pad was doing."""
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
            if not cfg_path:
                warnings.append("Ryujinx Config.json not found — cannot write.")
            if binding_gaps:
                warnings.append(f"Ryujinx's saved controller settings are "
                                f"missing {len(binding_gaps)} binding(s) "
                                f"({binding_gaps[0]}) — will be repaired.")
            for name in unmapped:
                warnings.append(f"{name}: SDL has no mapping for this pad, "
                                "so it cannot be used.")
            if not pads:
                warnings.append("No controllers detected. Wake one and it "
                                "will appear here.")

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
                   tuple(warnings), claimed_p1)
            if sig != last_sig:
                last_sig = sig
                draw_pad_grid(ui, pads, cycle, warnings, needed, holds,
                              claimed_p1 is not None)
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
                        if btn in (BTN_LSHOULDER, BTN_RSHOULDER):
                            combo_armed.discard(p.key)
                        holding.pop((p.key, btn), None)
            elif kind == "axis":
                inst, axis, value = payload
                for p in pads:
                    if p.instance_id == inst:
                        # Deadzone, or the sticks jitter constantly on screen.
                        p.axes[axis] = value if abs(value) > 6000 else 0

            elif kind == "button":
                inst, btn = payload
                pad = next((p for p in pads if p.instance_id == inst), None)
                if pad is None:
                    continue
                pad.held.add(btn)

                # Same press lands on the hidden physical pad too; pairing
                # them is what turns "Steam pad f679" into a real controller.
                if pad.real is None and (pad.vendor, pad.product) == STEAM_VIRTUAL:
                    taken = {q.real["path"] for q in pads if q.real}
                    hit = reals.claim(now, taken)
                    if hit:
                        bind_real(pad, hit, known, pads)

                if state == "error":
                    if btn == BTN_B:
                        state = "exit"
                    continue

                # Any player may mirror their own face buttons with L+R.
                if (btn in (BTN_LSHOULDER, BTN_RSHOULDER)
                        and BTN_LSHOULDER in pad.held
                        and BTN_RSHOULDER in pad.held
                        and pad.key not in combo_armed):
                    combo_armed.add(pad.key)
                    pad.swap_faces = not pad.swap_faces
                    remember(pads, known)

                # Claim Player 1. Deliberately one-shot: without the lock a
                # second player could keep taking the slot back, which is
                # exactly the game a sibling will play.
                if (not claimed_p1 and btn in (BTN_LSTICK, BTN_RSTICK)
                        and BTN_LSTICK in pad.held and BTN_RSTICK in pad.held):
                    claimed_p1 = pad.key
                    pads = resolve_slots(pads, slots, claimed_p1)
                    label_pads(pads)
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
            if not cfg_path:
                result, state = ["Ryujinx Config.json not found."], "error"
                continue
            remember(pads, known)
            problems = write_config(cfg_path, pads)
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
    launch(app_id, rom, dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
