#!/usr/bin/env python3
"""
preflight — Phase 0 diagnostics.

Prints everything we need to know before building the real tool:
what SDL sees, what the kernel sees, what Ryujinx would write, and
whether Steam Input is interfering.

Zero dependencies. Stdlib + ctypes against the system libSDL2.

Usage:
    python3 phase0.py                 # full report
    python3 phase0.py --watch         # live button test (Ctrl-C to stop)
    python3 phase0.py --json out.json # machine-readable dump

Run it TWICE: once with Steam Input enabled for the shortcut, once with it
disabled, and diff the two reports. That comparison is the whole point.
"""

import ctypes
import ctypes.util
import glob
import json
import os
import select
import shutil
import struct
import subprocess
import sys
import time

# ---------------------------------------------------------------- SDL bindings

SDL_INIT_JOYSTICK = 0x00000200
SDL_INIT_GAMECONTROLLER = 0x00002000

SDL_CONTROLLERBUTTONDOWN = 0x651

SDL_SENSOR_ACCEL = 1
SDL_SENSOR_GYRO = 2

POWER_LEVELS = {
    -1: "unknown", 0: "empty", 1: "low", 2: "medium", 3: "full", 4: "wired",
}

CONTROLLER_TYPES = {
    0: "unknown", 1: "Xbox 360", 2: "Xbox One", 3: "PS3", 4: "PS4",
    5: "Switch Pro", 6: "virtual", 7: "PS5", 8: "Amazon Luna",
    9: "Google Stadia", 10: "NVIDIA Shield", 11: "Joy-Con (L)",
    12: "Joy-Con (R)", 13: "Joy-Con pair",
}

BUS_TYPES = {
    0x03: "USB", 0x05: "Bluetooth", 0x06: "virtual", 0x18: "I2C", 0x19: "host",
}


# Kept as names so they can be used inside f-string expressions — a literal
# backslash escape there is a syntax error before Python 3.12.
YEL, RESET = "\033[33m", "\033[0m"
NONE_MAC = YEL + "none" + RESET
VIRTUAL_TAG = "   " + YEL + "<< VIRTUAL (uinput)" + RESET


class SDL_JoystickGUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_uint8 * 16)]


class SDL_version(ctypes.Structure):
    _fields_ = [("major", ctypes.c_uint8),
                ("minor", ctypes.c_uint8),
                ("patch", ctypes.c_uint8)]


def load_sdl():
    for name in ("libSDL2-2.0.so.0", "libSDL2.so.0", "libSDL2.so"):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    found = ctypes.util.find_library("SDL2")
    if found:
        return ctypes.CDLL(found)
    sys.exit("Could not load libSDL2. On SteamOS it should already be present; "
             "elsewhere install your distro's SDL2 runtime package.")


def bind(sdl):
    """Declare the signatures we use. Optional symbols degrade to None."""
    def opt(name, restype, argtypes):
        try:
            fn = getattr(sdl, name)
        except AttributeError:
            return None
        fn.restype, fn.argtypes = restype, argtypes
        return fn

    cp, vp, ci = ctypes.c_char_p, ctypes.c_void_p, ctypes.c_int

    sdl.SDL_Init.restype, sdl.SDL_Init.argtypes = ci, [ctypes.c_uint32]
    sdl.SDL_Quit.restype, sdl.SDL_Quit.argtypes = None, []
    sdl.SDL_GetError.restype, sdl.SDL_GetError.argtypes = cp, []
    sdl.SDL_GetVersion.restype = None
    sdl.SDL_GetVersion.argtypes = [ctypes.POINTER(SDL_version)]
    sdl.SDL_SetHint.restype, sdl.SDL_SetHint.argtypes = ci, [cp, cp]
    sdl.SDL_NumJoysticks.restype, sdl.SDL_NumJoysticks.argtypes = ci, []
    sdl.SDL_JoystickNameForIndex.restype = cp
    sdl.SDL_JoystickNameForIndex.argtypes = [ci]
    sdl.SDL_JoystickGetDeviceGUID.restype = SDL_JoystickGUID
    sdl.SDL_JoystickGetDeviceGUID.argtypes = [ci]
    sdl.SDL_JoystickGetGUIDString.restype = None
    sdl.SDL_JoystickGetGUIDString.argtypes = [SDL_JoystickGUID, cp, ci]
    sdl.SDL_IsGameController.restype, sdl.SDL_IsGameController.argtypes = ci, [ci]
    sdl.SDL_GameControllerOpen.restype = vp
    sdl.SDL_GameControllerOpen.argtypes = [ci]
    sdl.SDL_GameControllerClose.restype = None
    sdl.SDL_GameControllerClose.argtypes = [vp]
    sdl.SDL_GameControllerGetJoystick.restype = vp
    sdl.SDL_GameControllerGetJoystick.argtypes = [vp]
    sdl.SDL_GameControllerMapping.restype = cp
    sdl.SDL_GameControllerMapping.argtypes = [vp]
    sdl.SDL_JoystickInstanceID.restype, sdl.SDL_JoystickInstanceID.argtypes = ci, [vp]
    sdl.SDL_JoystickCurrentPowerLevel.restype = ci
    sdl.SDL_JoystickCurrentPowerLevel.argtypes = [vp]
    sdl.SDL_PollEvent.restype, sdl.SDL_PollEvent.argtypes = ci, [vp]
    sdl.SDL_GameControllerGetStringForButton.restype = cp
    sdl.SDL_GameControllerGetStringForButton.argtypes = [ci]
    sdl.SDL_GameControllerFromInstanceID.restype = vp
    sdl.SDL_GameControllerFromInstanceID.argtypes = [ci]

    return {
        "path_for_index": opt("SDL_JoystickPathForIndex", cp, [ci]),
        "get_serial": opt("SDL_GameControllerGetSerial", cp, [vp]),
        "has_sensor": opt("SDL_GameControllerHasSensor", ci, [vp, ci]),
        "get_type": opt("SDL_GameControllerGetType", ci, [ci]),
    }


def guid_hex(sdl, index):
    guid = sdl.SDL_JoystickGetDeviceGUID(index)
    buf = ctypes.create_string_buffer(33)
    sdl.SDL_JoystickGetGUIDString(guid, buf, 33)
    return buf.value.decode()


def sdl_guid_to_ryujinx(hexstr):
    """SDL's 32-hex-char GUID -> the dashed form Ryujinx writes in Config.json.

    Ryujinx feeds the raw SDL bytes to .NET's Guid(byte[]), which reads the
    first three fields little-endian and the last eight bytes as-is. So SDL
    '030000005e0400001. ..' becomes '00000003-045e-0000-130b-...'.
    Verified against real ids in section 4 of this report.
    """
    try:
        b = bytes.fromhex(hexstr)
    except ValueError:
        return "?"
    if len(b) != 16:
        return "?"
    d1 = int.from_bytes(b[0:4], "little")
    d2 = int.from_bytes(b[4:6], "little")
    d3 = int.from_bytes(b[6:8], "little")
    return f"{d1:08x}-{d2:04x}-{d3:04x}-{b[8]:02x}{b[9]:02x}-{b[10:16].hex()}"


def guid_fields(hexstr):
    """Decode SDL's GUID layout. Works for every backend, including hidapi
    devices that have no evdev node.

      [0:2] bus  [2:4] CRC16 of the device NAME  [4:6] vendor
      [8:10] product  [12:14] version

    The name CRC matters: it changes when the reported name changes, so the
    same physical pad can have different GUIDs in different launch contexts.
    """
    try:
        b = bytes.fromhex(hexstr)
    except ValueError:
        return {}
    if len(b) != 16:
        return {}
    le = lambda s, e: int.from_bytes(b[s:e], "little")
    return {
        "bus_id": le(0, 2),
        "name_crc": le(2, 4),
        "vendor": le(4, 6),
        "product": le(8, 10),
        "guid_version": le(12, 14),
    }


def is_steam_virtual(fields):
    """Valve 0x28de / 0x11ff is the Steam Virtual Gamepad."""
    return fields.get("vendor") == 0x28DE and fields.get("product") == 0x11FF


def stable_id(pad):
    """Best available per-device identity, in order of trustworthiness.

    SDL's serial is the Bluetooth MAC for hidapi-backed pads, and it is
    available where evdev 'uniq' is not (hidraw devices have no evdev node).
    """
    for key in ("serial", "uniq", "phys"):
        val = pad.get(key)
        if val and val not in ("0", "?"):
            return val, key
    return None, None


# --------------------------------------------------------------- sysfs helpers

def sysfs_read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def sysfs_for_event(devpath):
    """Enrich a /dev/input/eventN path with what the kernel knows."""
    if not devpath or not devpath.startswith("/dev/input/event"):
        return {}
    node = os.path.basename(devpath)
    base = f"/sys/class/input/{node}"
    dev = f"{base}/device"
    bustype = sysfs_read(f"{dev}/id/bustype")
    info = {
        "kernel_name": sysfs_read(f"{dev}/name"),
        "uniq": sysfs_read(f"{dev}/uniq") or None,
        "phys": sysfs_read(f"{dev}/phys") or None,
        "vendor": sysfs_read(f"{dev}/id/vendor"),
        "product": sysfs_read(f"{dev}/id/product"),
        "bustype": bustype,
        "bus": BUS_TYPES.get(int(bustype, 16), bustype) if bustype else None,
        "virtual": os.path.realpath(base).startswith("/sys/devices/virtual/"),
    }
    return info


def has_gamepad_key(node):
    """True if this evdev node advertises BTN_SOUTH (0x130) — i.e. it's a pad."""
    caps = sysfs_read(f"/sys/class/input/{node}/device/capabilities/key")
    if not caps:
        return False
    words = caps.split()[::-1]          # sysfs prints most-significant group first
    bit = 0x130
    idx, off = bit // 64, bit % 64
    if idx >= len(words):
        return False
    try:
        return bool(int(words[idx], 16) >> off & 1)
    except ValueError:
        return False


def scan_gamepad_nodes():
    nodes = []
    for path in sorted(glob.glob("/sys/class/input/event*"),
                       key=lambda p: int(os.path.basename(p)[5:])):
        node = os.path.basename(path)
        if has_gamepad_key(node):
            nodes.append(f"/dev/input/{node}")
    return nodes


# -------------------------------------------------------------------- sections

def section(title):
    print(f"\n\033[1m{title}\033[0m")
    print("─" * max(len(title), 40))


def report_environment():
    section("1. Environment — is Steam Input in the way?")

    interesting = [
        "SDL_GAMECONTROLLER_IGNORE_DEVICES",
        "SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT",
        "SDL_JOYSTICK_HIDAPI",
        "SDL_GAMECONTROLLERCONFIG",
        "STEAM_COMPAT_CLIENT_INSTALL_PATH",
        "SteamAppId",
        "SteamGameId",
        "STEAM_RUNTIME",
        "XDG_SESSION_TYPE",
    ]
    found = {k: os.environ.get(k) for k in interesting if os.environ.get(k)}
    if found:
        for k, v in found.items():
            print(f"  {k} = {v}")
    else:
        print("  (none of the Steam/SDL override vars are set)")

    ignore = os.environ.get("SDL_GAMECONTROLLER_IGNORE_DEVICES")
    print()
    if ignore:
        print("  \033[33m>> SDL_GAMECONTROLLER_IGNORE_DEVICES is set.\033[0m")
        print("     Steam Input is active and hiding physical pads from SDL.")
        print("     Every controller below is probably a Steam virtual pad.")
    elif found.get("SteamAppId") or found.get("STEAM_COMPAT_CLIENT_INSTALL_PATH"):
        print("  Launched from Steam, but no device ignore-list is set —")
        print("  Steam Input looks DISABLED for this shortcut. Good.")
    else:
        print("  Not launched from Steam. This is the raw-hardware baseline.")
        print("  Re-run from a Steam shortcut to see what Ryujinx will see.")
    return found


def report_controllers(sdl, ext):
    section("2. Controllers — SDL's view, cross-checked against the kernel")

    ver = SDL_version()
    sdl.SDL_GetVersion(ctypes.byref(ver))
    print(f"  SDL runtime: {ver.major}.{ver.minor}.{ver.patch}")
    if (ver.major, ver.minor) < (2, 24):
        print("  \033[33m>> Older than 2.24: SDL_JoystickPathForIndex unavailable,")
        print("     falling back to a sysfs scan for evdev nodes.\033[0m")
    print(f"  SDL_JoystickPathForIndex: "
          f"{'available' if ext['path_for_index'] else 'MISSING'}")

    n = sdl.SDL_NumJoysticks()
    print(f"  Joysticks detected: {n}")

    fallback_nodes = scan_gamepad_nodes()
    if not ext["path_for_index"]:
        print(f"  sysfs gamepad nodes (in order): {', '.join(fallback_nodes) or 'none'}")

    pads = []
    for i in range(n):
        name = sdl.SDL_JoystickNameForIndex(i)
        name = name.decode(errors="replace") if name else "?"
        ghex = guid_hex(sdl, i)
        rid = sdl_guid_to_ryujinx(ghex)

        devpath = None
        if ext["path_for_index"]:
            p = ext["path_for_index"](i)
            devpath = p.decode(errors="replace") if p else None
        elif i < len(fallback_nodes):
            devpath = fallback_nodes[i]

        pad = {
            "sdl_index": i,
            "name": name,
            "sdl_guid": ghex,
            "ryujinx_id": f"{i}-{rid}",
            "is_gamecontroller": bool(sdl.SDL_IsGameController(i)),
            "evdev": devpath,
            "evdev_source": "SDL" if ext["path_for_index"] else "sysfs guess",
        }
        pad.update(sysfs_for_event(devpath))
        fields = guid_fields(ghex)
        pad.update(fields)
        pad["steam_virtual"] = is_steam_virtual(fields)

        if ext["get_type"]:
            pad["sdl_type"] = CONTROLLER_TYPES.get(ext["get_type"](i), "?")

        gc = sdl.SDL_GameControllerOpen(i) if pad["is_gamecontroller"] else None
        if gc:
            js = sdl.SDL_GameControllerGetJoystick(gc)
            pad["instance_id"] = sdl.SDL_JoystickInstanceID(js)
            pad["battery"] = POWER_LEVELS.get(
                sdl.SDL_JoystickCurrentPowerLevel(js), "?")
            m = sdl.SDL_GameControllerMapping(gc)
            pad["mapping"] = m.decode(errors="replace") if m else None
            if ext["get_serial"]:
                s = ext["get_serial"](gc)
                pad["serial"] = s.decode(errors="replace") if s else None
            if ext["has_sensor"]:
                pad["gyro"] = bool(ext["has_sensor"](gc, SDL_SENSOR_GYRO))
                pad["accel"] = bool(ext["has_sensor"](gc, SDL_SENSOR_ACCEL))
            sdl.SDL_GameControllerClose(gc)

        pads.append(pad)

    for pad in pads:
        print(f"\n  [{pad['sdl_index']}] \033[1m{pad['name']}\033[0m")
        print(f"      Ryujinx id : {pad['ryujinx_id']}")
        print(f"      SDL GUID   : {pad['sdl_guid']}")
        print(f"      evdev      : {pad.get('evdev') or '?'}  ({pad['evdev_source']})")
        ident, source = stable_id(pad)
        print(f"      identity   : {ident or NONE_MAC}"
              f"{'  (from SDL ' + source + ')' if ident else ''}")
        print(f"      vendor/prod: {pad.get('vendor', 0):#06x} / "
              f"{pad.get('product', 0):#06x}   name-crc {pad.get('name_crc', 0):#06x}")
        print(f"      bus        : {pad.get('bus') or '?'}"
              f"{VIRTUAL_TAG if pad.get('steam_virtual') else ''}")
        print(f"      SDL type   : {pad.get('sdl_type', '?')}")
        print(f"      battery    : {pad.get('battery', '?')}")
        print(f"      gyro/accel : {pad.get('gyro', '?')} / {pad.get('accel', '?')}")
        if pad.get("serial"):
            print(f"      serial     : {pad['serial']}")
        if pad.get("mapping"):
            print(f"      mapping    : {pad['mapping'][:160]}")

    return pads


def report_identity(pads):
    section("3. Identity verdict — can we tell these pads apart across reboots?")

    if not pads:
        print("  No controllers detected — nothing to judge.")
        print("  Turn the pads on and re-run, or check Steam Input isn't hiding them.")
        return

    guids = {}
    for p in pads:
        guids.setdefault(p["sdl_guid"], []).append(p["sdl_index"])
    dupes = {g: idx for g, idx in guids.items() if len(idx) > 1}

    if dupes:
        for g, idx in dupes.items():
            print(f"  \033[33m>> GUID {g} shared by SDL indices {idx}.\033[0m")
            print("     These are indistinguishable to Ryujinx except by index.")
    else:
        print("  All GUIDs are distinct — no model collisions. "
              "Only the index prefix can drift.")

    print()
    anon = []
    for p in pads:
        ident, source = stable_id(p)
        if ident:
            print(f"  [{p['sdl_index']}] {p['name']}: {ident} (via {source})")
        else:
            anon.append(p)
    for p in anon:
        why = ("Steam virtual pad — Steam does not pass the real serial through"
               if p.get("steam_virtual") else "no serial, uniq or phys exposed")
        print(f"  \033[33m>> [{p['sdl_index']}] {p['name']}: no stable id ({why}).\033[0m")
        print(f"     Falls back to name-crc {p.get('name_crc', 0):#06x}, which is "
              f"stable as long as the reported NAME does not change.")

    virt = [p for p in pads if p.get("steam_virtual")]
    print()
    if not virt:
        print("  No Steam virtual pads — every device is raw hardware.")
    elif len(virt) == len(pads):
        print(f"  \033[33m>> ALL {len(pads)} pads are Steam virtual. Steam Input is "
              f"intercepting everything.\033[0m")
    else:
        print(f"  \033[33m>> MIXED: {len(virt)} of {len(pads)} pads are Steam virtual, "
              f"{len(pads) - len(virt)} are raw.\033[0m")
    if virt:
        crcs = [p.get("name_crc") for p in virt]
        if len(set(crcs)) == len(crcs):
            print("     Their GUIDs still differ (name CRC), so they remain")
            print("     distinguishable to Ryujinx — but only while the names hold.")
        else:
            print("     \033[31m Two virtual pads share a name CRC — indistinguishable.\033[0m")


def find_ryujinx():
    section("4. Ryujinx — install, config, and whether our id format is right")

    app_id, cfg = None, None
    if shutil.which("flatpak"):
        try:
            out = subprocess.run(
                ["flatpak", "list", "--app", "--columns=application"],
                capture_output=True, text=True, timeout=15).stdout
            for line in out.splitlines():
                if "ryu" in line.lower():
                    app_id = line.strip()
                    break
        except (subprocess.SubprocessError, OSError) as exc:
            print(f"  flatpak query failed: {exc}")
    print(f"  Flatpak app id: {app_id or 'not found'}")

    candidates = []
    if app_id:
        candidates.append(os.path.expanduser(
            f"~/.var/app/{app_id}/config/Ryujinx/Config.json"))
    candidates += [
        os.path.expanduser(
            "~/.var/app/io.github.ryubing.Ryujinx/config/Ryujinx/Config.json"),
        os.path.expanduser("~/.config/Ryujinx/Config.json"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            cfg = path
            break

    print(f"  Config.json  : {cfg or 'NOT FOUND — checked ' + ', '.join(candidates)}")

    if app_id and shutil.which("flatpak"):
        try:
            perms = subprocess.run(
                ["flatpak", "info", "--show-permissions", app_id],
                capture_output=True, text=True, timeout=15).stdout
            fs = [l for l in perms.splitlines()
                  if l.startswith("filesystems=") or l.startswith("devices=")]
            for line in fs:
                print(f"  {line}")
        except (subprocess.SubprocessError, OSError):
            pass

    return app_id, cfg


def report_config(cfg, pads):
    if not cfg:
        return None
    try:
        with open(cfg) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  \033[31mCould not parse Config.json: {exc}\033[0m")
        return None

    print(f"  config version: {data.get('version', '?')}")
    entries = data.get("input_config") or []
    print(f"  input_config entries: {len(entries)}")

    live_ids = {p["ryujinx_id"] for p in pads}
    live_guids = {p["ryujinx_id"].split("-", 1)[1] for p in pads}

    face_keys = set()
    for e in entries:
        eid = e.get("id", "?")
        guid_part = eid.split("-", 1)[1] if "-" in eid else eid
        if eid in live_ids:
            status = "\033[32mMATCHES a connected pad\033[0m"
        elif guid_part in live_guids:
            status = "\033[33mGUID present but INDEX DRIFTED\033[0m"
        else:
            status = "not connected right now"
        print(f"\n    {e.get('player_index','?'):<8} {e.get('controller_type','?'):<16}"
              f" {e.get('backend','?')}")
        print(f"    id: {eid}")
        print(f"    -> {status}")
        for k in e:
            if k.lower().startswith("button_") or "joycon" in k.lower():
                face_keys.add(k)

    dupes = [e.get("id") for e in entries]
    for eid in set(dupes):
        if dupes.count(eid) > 1:
            print(f"\n  \033[31m>> DUPLICATE id in config: {eid} "
                  f"({dupes.count(eid)}x)\033[0m")

    if not any(e.get("player_index") == "Player1" for e in entries):
        print("\n  \033[31m>> No Player1 entry. In Ryujinx this means NO pad works.\033[0m")

    if face_keys:
        print(f"\n  button/stick keys present in entries "
              f"(this is the schema we must edit):")
        for k in sorted(face_keys):
            print(f"    {k}")

    matched = [e for e in entries if e.get("id") in live_ids]
    print(f"\n  id-format check: {len(matched)}/{len(entries)} config entries exactly "
          f"match a computed id.")
    if matched:
        print("  \033[32mOur SDL-GUID -> Ryujinx-id conversion is confirmed correct.\033[0m")
    else:
        print("  \033[33mNo exact match. Either no configured pad is connected, or the\033[0m")
        print("  \033[33mid format differs — compare the strings above by hand.\033[0m")

    return data


# ------------------------------------------------------------------ watch mode

def watch(sdl, ext, pads):
    section("5. Live button test — press buttons, Ctrl-C to stop")
    print("  Shows the SDL button name each press produces, plus which evdev")
    print("  node fired. That correlation is what pins SDL index -> MAC.\n")

    handles = []
    for p in pads:
        if p["is_gamecontroller"]:
            gc = sdl.SDL_GameControllerOpen(p["sdl_index"])
            if gc:
                handles.append(gc)

    fds = {}
    for p in pads:
        path = p.get("evdev")
        if not path:
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            fds[fd] = p
        except OSError as exc:
            print(f"  (cannot read {path}: {exc}; evdev correlation disabled "
                  f"for {p['name']})")

    ev_struct = struct.Struct("qqHHi")   # input_event on 64-bit Linux
    EV_KEY = 1
    buf = ctypes.create_string_buffer(128)   # SDL_Event is 56 bytes; 128 is safe
    recent_evdev = {}

    try:
        while True:
            if fds:
                ready, _, _ = select.select(list(fds), [], [], 0.01)
                for fd in ready:
                    try:
                        raw = os.read(fd, ev_struct.size * 16)
                    except OSError:
                        continue
                    for off in range(0, len(raw) - ev_struct.size + 1,
                                     ev_struct.size):
                        _, _, etype, code, value = ev_struct.unpack_from(raw, off)
                        if etype == EV_KEY and value == 1:
                            recent_evdev[time.time()] = (fds[fd], code)
            else:
                time.sleep(0.01)

            while sdl.SDL_PollEvent(ctypes.byref(buf)):
                etype = struct.unpack_from("I", buf, 0)[0]
                if etype != SDL_CONTROLLERBUTTONDOWN:
                    continue
                which = struct.unpack_from("i", buf, 8)[0]
                button = struct.unpack_from("B", buf, 12)[0]
                bname = sdl.SDL_GameControllerGetStringForButton(button)
                bname = bname.decode() if bname else str(button)

                pad = next((p for p in pads if p.get("instance_id") == which), None)
                label = pad["name"] if pad else f"instance {which}"
                idx = pad["sdl_index"] if pad else "?"

                now = time.time()
                near = [(p, c) for t, (p, c) in recent_evdev.items()
                        if now - t < 0.15]
                extra = ""
                if near:
                    p2, code = near[-1]
                    extra = (f"   evdev {p2.get('evdev')} "
                             f"(mac {p2.get('uniq') or '-'}) code {code}")
                recent_evdev = {t: v for t, v in recent_evdev.items()
                                if now - t < 0.5}

                print(f"  [{idx}] {label:<28} SDL button: \033[1m{bname}\033[0m{extra}")
    except KeyboardInterrupt:
        print("\n  stopped.")
    finally:
        for gc in handles:
            sdl.SDL_GameControllerClose(gc)
        for fd in fds:
            os.close(fd)


# ------------------------------------------------------------------------ main

def main():
    args = sys.argv[1:]
    json_out = None
    if "--json" in args:
        i = args.index("--json")
        json_out = args[i + 1] if i + 1 < len(args) else "phase0.json"

    sdl = load_sdl()
    ext = bind(sdl)
    sdl.SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")
    # Never take over a physical Steam Controller. SDL is able to clear its
    # lizard mode and drive it directly, but if we do that and exit, Steam
    # does not reliably reclaim the device and the pad goes dead until it is
    # power-cycled. Steam owns that controller; we only read its virtual pad.
    for hint in (b"SDL_JOYSTICK_HIDAPI_STEAM", b"SDL_JOYSTICK_HIDAPI_STEAMDECK",
                 b"SDL_JOYSTICK_HIDAPI_STEAM_HORI"):
        sdl.SDL_SetHint(hint, b"0")
    if sdl.SDL_Init(SDL_INIT_JOYSTICK | SDL_INIT_GAMECONTROLLER) != 0:
        sys.exit(f"SDL_Init failed: {sdl.SDL_GetError().decode()}")

    print("\033[1mpreflight — Phase 0 diagnostics\033[0m")
    print(f"host: {os.uname().nodename}   {time.strftime('%Y-%m-%d %H:%M:%S')}")

    env = report_environment()
    pads = report_controllers(sdl, ext)
    report_identity(pads)
    app_id, cfg = find_ryujinx()
    cfg_data = report_config(cfg, pads) if cfg else None

    if json_out:
        with open(json_out, "w") as fh:
            json.dump({"env": env, "pads": pads, "flatpak_app_id": app_id,
                       "config_path": cfg,
                       "input_config": (cfg_data or {}).get("input_config"),
                       "when": time.strftime("%Y-%m-%dT%H:%M:%S")}, fh, indent=2)
        print(f"\n  wrote {json_out}")

    if "--watch" in args:
        watch(sdl, ext, pads)

    sdl.SDL_Quit()


if __name__ == "__main__":
    main()
