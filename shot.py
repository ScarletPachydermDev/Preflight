#!/usr/bin/env python3
"""Render a preflight screen to a PNG without putting it on the TV.

Launching the real thing on a live Game Mode session has frozen it before, so
this is how a layout change gets checked: render offscreen, write a file, look
at it. It caught a badly distorted gamepad once that compiled perfectly.

    ./shot.py out.png                      # the roster, with whatever pads are awake
    ./shot.py out.png --pads 4             # four synthetic pads, no hardware needed
    ./shot.py out.png --alert "text"       # with the alert band
    ./shot.py out.png --pads 2 --swap 2    # pad 2 showing the mirrored badge

Zero dependencies: SDL's offscreen video driver, SDL_RenderReadPixels, and a
PNG written by hand out of zlib — the same reason sdlui carries its own PNG
decoder, since SteamOS has neither SDL2_image nor Pillow.
"""

import argparse
import ctypes
import os
import struct
import sys
import zlib

os.environ.setdefault("SDL_VIDEODRIVER", "offscreen")
# Otherwise a modern SDL hides Steam's virtual pads from us, and a machine in
# Game Mode appears to have no controllers at all.
os.environ.setdefault("SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD", "1")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sdlui                                                    # noqa: E402
import preflight as pf                                          # noqa: E402

SDL_PIXELFORMAT_ARGB8888 = 0x16362004


class FakePad:
    """Enough of a Pad for the drawing code, for a machine with nothing awake."""

    def __init__(self, slot, swap=False):
        self.slot = slot
        self.key = f"fake{slot}"
        self.label = f"Example pad {slot}"
        self.held = set()
        self.axes = {}
        self.swap_faces = swap
        self.instance_id = -slot
        self.index = slot - 1
        self.name = self.gc_name = self.label
        self.sdl_guid = "03000000de280000ff11000001000000"
        self.vendor, self.product = 0x28de, 0x11ff
        self.mac = None
        self.battery = None

    def attached(self):
        return True


def write_png(path, width, height, argb):
    rows = bytearray()
    pixels = memoryview(argb).cast("B")
    for y in range(height):
        rows.append(0)                       # filter: none
        line = pixels[y * width * 4:(y + 1) * width * 4]
        for x in range(width):
            b, g, r = line[x * 4], line[x * 4 + 1], line[x * 4 + 2]
            rows += bytes((r, g, b))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n"
                 + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                                              8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
                 + chunk(b"IEND", b""))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", nargs="?", default="roster.png")
    ap.add_argument("--pads", type=int, default=0,
                    help="render this many synthetic pads instead of real ones")
    ap.add_argument("--swap", type=int, action="append", default=[],
                    help="slot number to show with its A/B mirrored")
    ap.add_argument("--alert", help="text for the alert band")
    ap.add_argument("--size", default="2560x1440",
                    help="surface to render at; defaults to a 1440p TV, "
                         "because the offscreen desktop is 1024x768 and 4:3")
    args = ap.parse_args()

    # Checking a layout at the wrong aspect ratio is barely checking it: the
    # offscreen driver's desktop is 1024x768, the living-room target is 16:9.
    if "x" in args.size:
        os.environ["PREFLIGHT_WINDOW"] = args.size
    sdl, ttf = sdlui.load_libraries()
    sdlui.set_preinit_hints(sdl)
    if sdl.SDL_Init(sdlui.SDL_INIT_VIDEO | sdlui.SDL_INIT_JOYSTICK
                    | sdlui.SDL_INIT_GAMECONTROLLER) != 0:
        sys.exit(f"SDL_Init: {sdl.SDL_GetError().decode()}")
    pf.apply_theme()
    ui = pf.UI(sdl, ttf)

    if args.pads:
        pads = [FakePad(n, swap=n in args.swap) for n in range(1, args.pads + 1)]
    else:
        pads, _ = pf.scan_pads(sdl)
        pf.apply_known(pads, pf.load_json(pf.KNOWN_PADS, {}))
        pads = pf.resolve_slots(pads, pf.new_slot_state())
        pf.label_pads(pads)
        for slot in args.swap:
            for pad in pads:
                if pad.slot == slot:
                    pad.swap_faces = True

    pf.draw_pad_grid(ui, pads, pf.RumbleCycle(sdl), [], None, {}, False,
                     args.alert)

    buf = (ctypes.c_uint8 * (ui.w * ui.h * 4))()
    sdl.SDL_RenderReadPixels.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_uint32, ctypes.c_void_p,
                                         ctypes.c_int]
    if sdl.SDL_RenderReadPixels(ui.renderer, None, SDL_PIXELFORMAT_ARGB8888,
                                buf, ui.w * 4) != 0:
        sys.exit(f"SDL_RenderReadPixels: {sdl.SDL_GetError().decode()}")
    write_png(args.out, ui.w, ui.h, buf)
    print(f"{args.out}  {ui.w}x{ui.h}  {len(pads)} pad(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
