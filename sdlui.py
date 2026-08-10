"""
Minimal fullscreen UI toolkit: ctypes bindings for system SDL2 + SDL2_ttf.

No pip packages. SteamOS has no pip and a read-only /usr, so everything here
talks to the libraries already on the system.
"""

import ctypes
import ctypes.util
import os
import sys

# ------------------------------------------------------------------ constants

SDL_INIT_VIDEO = 0x00000020
SDL_INIT_JOYSTICK = 0x00000200
SDL_INIT_GAMECONTROLLER = 0x00002000

SDL_WINDOWPOS_UNDEFINED = 0x1FFF0000
SDL_WINDOW_FULLSCREEN_DESKTOP = 0x00001001
SDL_WINDOW_SHOWN = 0x00000004

SDL_RENDERER_ACCELERATED = 0x00000002
SDL_RENDERER_PRESENTVSYNC = 0x00000004

SDL_QUIT = 0x100
SDL_KEYDOWN = 0x300
SDL_CONTROLLERAXISMOTION = 0x650
SDL_CONTROLLERBUTTONDOWN = 0x651
SDL_CONTROLLERBUTTONUP = 0x652
SDL_CONTROLLERDEVICEADDED = 0x653
SDL_CONTROLLERDEVICEREMOVED = 0x654

SDLK_ESCAPE = 27
SDLK_RETURN = 13

# SDL_GameControllerButton
BTN_A, BTN_B, BTN_X, BTN_Y = 0, 1, 2, 3
BTN_BACK, BTN_GUIDE, BTN_START = 4, 5, 6
BTN_LSTICK, BTN_RSTICK, BTN_LSHOULDER, BTN_RSHOULDER = 7, 8, 9, 10
BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT = 11, 12, 13, 14

BUTTON_NAMES = {
    BTN_A: "A", BTN_B: "B", BTN_X: "X", BTN_Y: "Y",
    BTN_BACK: "Back", BTN_GUIDE: "Guide", BTN_START: "Start",
    BTN_LSTICK: "L-Stick", BTN_RSTICK: "R-Stick",
    BTN_LSHOULDER: "L", BTN_RSHOULDER: "R",
    BTN_DPAD_UP: "D-Up", BTN_DPAD_DOWN: "D-Down",
    BTN_DPAD_LEFT: "D-Left", BTN_DPAD_RIGHT: "D-Right",
}

# What the Switch receives when SDL reports a given physical button. Ryujinx
# maps SDL's positional names onto Nintendo's layout, which is mirrored: the
# bottom face button is Nintendo B, the right one is Nintendo A.
SWITCH_EQUIVALENT = {
    "A": "B", "B": "A", "X": "Y", "Y": "X",
    "L": "L", "R": "R", "Back": "Minus", "Start": "Plus",
    "L-Stick": "L-Stick", "R-Stick": "R-Stick",
    "D-Up": "D-Up", "D-Down": "D-Down",
    "D-Left": "D-Left", "D-Right": "D-Right", "Guide": "Home",
}

FONT_CANDIDATES = [
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
]
# Colour-emoji font, kept separate: the UI face has no emoji coverage, so a
# glyph like the roadworks sign has to come from here or it renders as tofu.
FONT_EMOJI_CANDIDATES = [
    "/usr/share/fonts/twemoji/twemoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
]

FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]


class SDL_Rect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_int),
                ("w", ctypes.c_int), ("h", ctypes.c_int)]


class SDL_Color(ctypes.Structure):
    _fields_ = [("r", ctypes.c_uint8), ("g", ctypes.c_uint8),
                ("b", ctypes.c_uint8), ("a", ctypes.c_uint8)]


class SDL_JoystickGUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_uint8 * 16)]


def load_png(path):
    """Decode an 8-bit RGB/RGBA PNG to raw RGBA bytes.

    SteamOS ships neither SDL2_image nor Pillow, and its /usr is read-only, so
    this is the whole image pipeline. Only what our own build output uses is
    supported: 8 bits per channel, no interlacing.
    """
    import zlib
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path}: not a PNG")

    pos, idat, w = 8, [], None
    while pos < len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            w = int.from_bytes(body[0:4], "big")
            h = int.from_bytes(body[4:8], "big")
            depth, color, _, _, interlace = body[8:13]
            if depth != 8 or color not in (2, 6) or interlace:
                raise ValueError(f"{path}: need 8-bit RGB/RGBA, uninterlaced")
            bpp = 4 if color == 6 else 3
        elif ctype == b"IDAT":
            idat.append(body)
        elif ctype == b"IEND":
            break
        pos += 12 + length
    if w is None:
        raise ValueError(f"{path}: no IHDR")

    raw = zlib.decompress(b"".join(idat))
    stride = w * bpp
    out = bytearray(h * stride)
    prev = bytearray(stride)
    src = 0
    for y in range(h):
        ftype = raw[src]
        src += 1
        line = bytearray(raw[src:src + stride])
        src += stride
        if ftype == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                c = prev[i - bpp] if i >= bpp else 0
                b = prev[i]
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        elif ftype != 0:
            raise ValueError(f"{path}: bad filter {ftype}")
        out[y * stride:(y + 1) * stride] = line
        prev = line

    if bpp == 3:                      # expand to RGBA
        rgba = bytearray(w * h * 4)
        for i in range(w * h):
            rgba[i * 4:i * 4 + 3] = out[i * 3:i * 3 + 3]
            rgba[i * 4 + 3] = 255
        out = rgba
    return w, h, bytes(out)


def _load(names):
    for n in names:
        try:
            return ctypes.CDLL(n)
        except OSError:
            continue
    return None


def load_libraries():
    sdl = _load(["libSDL2-2.0.so.0", "libSDL2.so.0", "libSDL2.so"])
    if sdl is None:
        found = ctypes.util.find_library("SDL2")
        sdl = ctypes.CDLL(found) if found else None
    if sdl is None:
        raise RuntimeError("libSDL2 not found")

    ttf = _load(["libSDL2_ttf-2.0.so.0", "libSDL2_ttf.so.0", "libSDL2_ttf.so"])
    if ttf is None:
        raise RuntimeError("libSDL2_ttf not found")

    _bind(sdl, ttf)
    return sdl, ttf


def set_preinit_hints(sdl):
    """Must run BEFORE SDL_Init — these are read during subsystem startup.

    Modern SDL can drive a physical Steam Controller itself, clearing the
    controller's lizard mode to read it as a gamepad. We must never do that:
    if we take the device over and then exit, Steam does not reliably reclaim
    it and the pad goes dead until it is power-cycled. Steam owns that
    controller; all we ever want is the virtual pad it publishes for us.
    """
    for name, value in ((b"SDL_JOYSTICK_HIDAPI_STEAM", b"0"),
                        (b"SDL_JOYSTICK_HIDAPI_STEAMDECK", b"0"),
                        (b"SDL_JOYSTICK_HIDAPI_STEAM_HORI", b"0")):
        sdl.SDL_SetHint(name, value)


def _bind(sdl, ttf):
    ci, cp, vp, cu8 = ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint8

    sdl.SDL_Init.argtypes, sdl.SDL_Init.restype = [ctypes.c_uint32], ci
    sdl.SDL_Quit.restype = None
    sdl.SDL_GetError.restype = cp
    sdl.SDL_Delay.argtypes = [ctypes.c_uint32]
    sdl.SDL_SetHint.argtypes, sdl.SDL_SetHint.restype = [cp, cp], ci

    sdl.SDL_CreateWindow.argtypes = [cp, ci, ci, ci, ci, ctypes.c_uint32]
    sdl.SDL_CreateWindow.restype = vp
    sdl.SDL_DestroyWindow.argtypes = [vp]
    sdl.SDL_CreateRenderer.argtypes = [vp, ci, ctypes.c_uint32]
    sdl.SDL_CreateRenderer.restype = vp
    sdl.SDL_DestroyRenderer.argtypes = [vp]
    sdl.SDL_GetRendererOutputSize.argtypes = [vp, ctypes.POINTER(ci),
                                              ctypes.POINTER(ci)]
    sdl.SDL_SetRenderDrawColor.argtypes = [vp, cu8, cu8, cu8, cu8]
    sdl.SDL_SetRenderDrawBlendMode.argtypes = [vp, ci]
    sdl.SDL_RenderClear.argtypes = [vp]
    sdl.SDL_RenderFillRect.argtypes = [vp, ctypes.POINTER(SDL_Rect)]
    sdl.SDL_RenderDrawRect.argtypes = [vp, ctypes.POINTER(SDL_Rect)]
    sdl.SDL_RenderPresent.argtypes = [vp]
    sdl.SDL_RenderCopy.argtypes = [vp, vp, ctypes.POINTER(SDL_Rect),
                                   ctypes.POINTER(SDL_Rect)]
    sdl.SDL_CreateTextureFromSurface.argtypes = [vp, vp]
    sdl.SDL_CreateTextureFromSurface.restype = vp
    sdl.SDL_QueryTexture.argtypes = [vp, ctypes.POINTER(ctypes.c_uint32),
                                     ctypes.POINTER(ci), ctypes.POINTER(ci),
                                     ctypes.POINTER(ci)]
    sdl.SDL_DestroyTexture.argtypes = [vp]
    sdl.SDL_FreeSurface.argtypes = [vp]
    sdl.SDL_PollEvent.argtypes, sdl.SDL_PollEvent.restype = [vp], ci
    sdl.SDL_ShowCursor.argtypes, sdl.SDL_ShowCursor.restype = [ci], ci

    sdl.SDL_NumJoysticks.restype = ci
    sdl.SDL_IsGameController.argtypes, sdl.SDL_IsGameController.restype = [ci], ci
    sdl.SDL_JoystickNameForIndex.argtypes = [ci]
    sdl.SDL_JoystickNameForIndex.restype = cp
    sdl.SDL_JoystickGetDeviceGUID.argtypes = [ci]
    sdl.SDL_JoystickGetDeviceGUID.restype = SDL_JoystickGUID
    sdl.SDL_JoystickGetGUIDString.argtypes = [SDL_JoystickGUID, cp, ci]
    sdl.SDL_GameControllerOpen.argtypes, sdl.SDL_GameControllerOpen.restype = [ci], vp
    sdl.SDL_GameControllerClose.argtypes = [vp]
    sdl.SDL_GameControllerGetJoystick.argtypes = [vp]
    sdl.SDL_GameControllerGetJoystick.restype = vp
    sdl.SDL_JoystickInstanceID.argtypes, sdl.SDL_JoystickInstanceID.restype = [vp], ci
    sdl.SDL_JoystickCurrentPowerLevel.argtypes = [vp]
    sdl.SDL_JoystickCurrentPowerLevel.restype = ci

    sdl.SDL_CreateRGBSurfaceFrom.argtypes = [vp, ci, ci, ci, ci,
                                             ctypes.c_uint32, ctypes.c_uint32,
                                             ctypes.c_uint32, ctypes.c_uint32]
    sdl.SDL_CreateRGBSurfaceFrom.restype = vp
    sdl.SDL_RenderSetClipRect.argtypes = [vp, ctypes.POINTER(SDL_Rect)]
    sdl.SDL_SetTextureColorMod.argtypes = [vp, cu8, cu8, cu8]
    sdl.SDL_SetTextureAlphaMod.argtypes = [vp, cu8]
    sdl.SDL_SetTextureBlendMode.argtypes = [vp, ci]

    sdl.SDL_GetTicks.restype = ctypes.c_uint32
    sdl.SDL_GetTicks.argtypes = []

    for name, restype in (("SDL_JoystickPathForIndex", cp),
                          ("SDL_GameControllerGetSerial", cp)):
        try:
            fn = getattr(sdl, name)
            fn.restype = restype
            fn.argtypes = [ci] if "Index" in name else [vp]
        except AttributeError:
            pass

    # Rumble arrived in SDL 2.0.9 and the capability query in 2.0.18, so both
    # are treated as optional rather than assumed present.
    try:
        sdl.SDL_GameControllerRumble.argtypes = [vp, ctypes.c_uint16,
                                                 ctypes.c_uint16,
                                                 ctypes.c_uint32]
        sdl.SDL_GameControllerRumble.restype = ci
    except AttributeError:
        pass
    try:
        sdl.SDL_GameControllerHasRumble.argtypes = [vp]
        sdl.SDL_GameControllerHasRumble.restype = ci
    except AttributeError:
        pass

    # The authoritative "is this pad still there?" query. A Bluetooth pad that
    # sleeps can linger in the joystick list, and rumble still reports success
    # on it, so this is the only reliable liveness signal.
    sdl.SDL_GameControllerGetAttached.argtypes = [vp]
    sdl.SDL_GameControllerGetAttached.restype = ci

    ttf.TTF_Init.restype = ci
    ttf.TTF_OpenFont.argtypes, ttf.TTF_OpenFont.restype = [cp, ci], vp
    ttf.TTF_CloseFont.argtypes = [vp]
    ttf.TTF_RenderUTF8_Blended.argtypes = [vp, cp, SDL_Color]
    ttf.TTF_RenderUTF8_Blended.restype = vp
    ttf.TTF_FontAscent.argtypes, ttf.TTF_FontAscent.restype = [vp], ci
    ttf.TTF_GlyphMetrics.argtypes = [vp, ctypes.c_uint16] + [ctypes.POINTER(ci)] * 5
    ttf.TTF_GlyphMetrics.restype = ci
    ttf.TTF_Quit.restype = None


class UI:
    """A fullscreen window with cached text rendering."""

    def __init__(self, sdl, ttf, title="preflight"):
        self.sdl, self.ttf = sdl, ttf
        sdl.SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")
        sdl.SDL_SetHint(b"SDL_HINT_VIDEO_MINIMIZE_ON_FOCUS_LOSS", b"0")

        self.window = sdl.SDL_CreateWindow(
            title.encode(), SDL_WINDOWPOS_UNDEFINED, SDL_WINDOWPOS_UNDEFINED,
            1280, 800, SDL_WINDOW_FULLSCREEN_DESKTOP | SDL_WINDOW_SHOWN)
        if not self.window:
            raise RuntimeError(f"CreateWindow: {sdl.SDL_GetError().decode()}")

        self.renderer = sdl.SDL_CreateRenderer(
            self.window, -1, SDL_RENDERER_ACCELERATED | SDL_RENDERER_PRESENTVSYNC)
        if not self.renderer:
            self.renderer = sdl.SDL_CreateRenderer(self.window, -1, 0)
        if not self.renderer:
            raise RuntimeError(f"CreateRenderer: {sdl.SDL_GetError().decode()}")

        sdl.SDL_ShowCursor(0)

        w, h = ctypes.c_int(), ctypes.c_int()
        sdl.SDL_GetRendererOutputSize(self.renderer, ctypes.byref(w),
                                      ctypes.byref(h))
        self.w, self.h = w.value or 1280, h.value or 800

        if ttf.TTF_Init() != 0:
            raise RuntimeError("TTF_Init failed")

        # Scale type to the display so it stays legible from a sofa.
        unit = max(self.h / 800.0, 0.75)
        self._fonts, self._cache, self._images = {}, {}, {}
        self.size = {
            "title": int(40 * unit), "head": int(30 * unit),
            "body": int(23 * unit), "small": int(18 * unit),
            "huge": int(64 * unit),
        }

    # ------------------------------------------------------------- resources

    def _font(self, px, bold=False, emoji=False):
        key = (px, bold, emoji)
        if key in self._fonts:
            return self._fonts[key]
        candidates = (FONT_EMOJI_CANDIDATES if emoji
                      else (FONT_BOLD_CANDIDATES if bold else []) + FONT_CANDIDATES)
        for path in candidates:
            if os.path.exists(path):
                f = self.ttf.TTF_OpenFont(path.encode(), px)
                if f:
                    self._fonts[key] = f
                    return f
        if emoji:
            return None                       # caller falls back to no glyph
        raise RuntimeError("no usable TTF font found")

    def _texture(self, text, px, color, bold, emoji=False):
        key = (text, px, color, bold, emoji)
        if key in self._cache:
            return self._cache[key]
        font = self._font(px, bold, emoji)
        if font is None:
            return None
        surf = self.ttf.TTF_RenderUTF8_Blended(
            font, text.encode("utf-8"),
            SDL_Color(color[0], color[1], color[2], 255))
        if not surf:
            return None
        tex = self.sdl.SDL_CreateTextureFromSurface(self.renderer, surf)
        self.sdl.SDL_FreeSurface(surf)
        tw, th = ctypes.c_int(), ctypes.c_int()
        self.sdl.SDL_QueryTexture(tex, None, None, ctypes.byref(tw),
                                  ctypes.byref(th))
        entry = (tex, tw.value, th.value)
        # Bounded cache: the UI redraws a small, mostly-static set of strings.
        if len(self._cache) > 400:
            for k, (t, _, _) in list(self._cache.items())[:200]:
                self.sdl.SDL_DestroyTexture(t)
                del self._cache[k]
        self._cache[key] = entry
        return entry

    # ---------------------------------------------------------------- drawing

    def clear(self, color):
        self.sdl.SDL_SetRenderDrawColor(self.renderer, color[0], color[1],
                                        color[2], 255)
        self.sdl.SDL_RenderClear(self.renderer)

    def rect(self, x, y, w, h, color, fill=True):
        self.sdl.SDL_SetRenderDrawColor(self.renderer, color[0], color[1],
                                        color[2], 255)
        r = SDL_Rect(int(x), int(y), int(w), int(h))
        if fill:
            self.sdl.SDL_RenderFillRect(self.renderer, ctypes.byref(r))
        else:
            self.sdl.SDL_RenderDrawRect(self.renderer, ctypes.byref(r))

    def _image(self, path):
        if path in self._images:
            return self._images[path]
        try:
            w, h, rgba = load_png(path)
        except (OSError, ValueError) as exc:
            print(f"image: {exc}", file=sys.stderr)
            self._images[path] = None
            return None
        buf = ctypes.create_string_buffer(rgba, len(rgba))
        # RGBA byte order in memory -> these masks on little-endian.
        surf = self.sdl.SDL_CreateRGBSurfaceFrom(
            buf, w, h, 32, w * 4,
            0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000)
        tex = self.sdl.SDL_CreateTextureFromSurface(self.renderer, surf)
        self.sdl.SDL_FreeSurface(surf)
        self.sdl.SDL_SetTextureBlendMode(tex, 1)   # SDL_BLENDMODE_BLEND
        self._images[path] = (tex, w, h, buf)      # keep buf alive
        return self._images[path]

    def image(self, path, x, y, w, h, color=(255, 255, 255), alpha=255,
              additive=False):
        """Draw a PNG, tinted by `color` and faded by `alpha`.

        `additive` adds the image to what is underneath instead of blending
        over it. Colour modulation can only ever darken, so art that is already
        dark — like Steam's near-black controller bodies — stays invisible when
        composited normally. Adding it makes the light line work glow and
        leaves the dark fill almost untouched, which is exactly the read we
        want on a dark background.
        """
        got = self._image(path)
        if not got:
            return
        tex = got[0]
        self.sdl.SDL_SetTextureBlendMode(tex, 2 if additive else 1)
        self.sdl.SDL_SetTextureColorMod(tex, color[0], color[1], color[2])
        self.sdl.SDL_SetTextureAlphaMod(tex, alpha)
        dst = SDL_Rect(int(x), int(y), int(w), int(h))
        self.sdl.SDL_RenderCopy(self.renderer, tex, None, ctypes.byref(dst))

    def stripe_bg(self, x, y, w, h, c1, c2, period=26):
        """Diagonal hazard stripes, built once as a small tile and repeated.

        Drawing the diagonals directly would mean thousands of one-pixel spans
        every frame; a tile plus a clip rect is a couple of hundred blits.
        """
        key = ("stripes", c1, c2, period)
        if key not in self._images:
            n = period * 2
            buf = bytearray(n * n * 4)
            for yy in range(n):
                for xx in range(n):
                    c = c1 if ((xx + yy) // period) % 2 == 0 else c2
                    i = (yy * n + xx) * 4
                    buf[i:i + 3] = bytes(c)
                    buf[i + 3] = 255
            cbuf = ctypes.create_string_buffer(bytes(buf), len(buf))
            surf = self.sdl.SDL_CreateRGBSurfaceFrom(
                cbuf, n, n, 32, n * 4,
                0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000)
            tex = self.sdl.SDL_CreateTextureFromSurface(self.renderer, surf)
            self.sdl.SDL_FreeSurface(surf)
            self._images[key] = (tex, n, n, cbuf)

        tex, n, _, _ = self._images[key]
        clip = SDL_Rect(int(x), int(y), int(w), int(h))
        self.sdl.SDL_RenderSetClipRect(self.renderer, ctypes.byref(clip))
        for ty in range(int(y), int(y + h), n):
            for tx in range(int(x), int(x + w), n):
                dst = SDL_Rect(tx, ty, n, n)
                self.sdl.SDL_RenderCopy(self.renderer, tex, None,
                                        ctypes.byref(dst))
        self.sdl.SDL_RenderSetClipRect(self.renderer, None)

    def fill_circle(self, cx, cy, r, color):
        """SDL2 has no circle primitive, so scan-fill it as horizontal spans.
        Radii here are small (a few dozen pixels) so the cost is trivial."""
        self.sdl.SDL_SetRenderDrawColor(self.renderer, color[0], color[1],
                                        color[2], 255)
        cx, cy, r = int(cx), int(cy), int(r)
        for dy in range(-r, r + 1):
            dx = int((r * r - dy * dy) ** 0.5)
            span = SDL_Rect(cx - dx, cy + dy, 2 * dx + 1, 1)
            self.sdl.SDL_RenderFillRect(self.renderer, ctypes.byref(span))

    def fill_triangle(self, p1, p2, p3, color):
        """Scanline-filled triangle — SDL2 has no polygon primitive."""
        self.sdl.SDL_SetRenderDrawColor(self.renderer, color[0], color[1],
                                        color[2], 255)
        pts = sorted([p1, p2, p3], key=lambda p: p[1])
        (x1, y1), (x2, y2), (x3, y3) = pts
        if y3 == y1:
            return
        for y in range(int(y1), int(y3) + 1):
            xa = x1 + (x3 - x1) * (y - y1) / (y3 - y1)
            if y < y2 and y2 != y1:
                xb = x1 + (x2 - x1) * (y - y1) / (y2 - y1)
            elif y3 != y2:
                xb = x2 + (x3 - x2) * (y - y2) / (y3 - y2)
            else:
                xb = x2
            lo, hi = sorted((xa, xb))
            span = SDL_Rect(int(lo), int(y), max(1, int(hi - lo)), 1)
            self.sdl.SDL_RenderFillRect(self.renderer, ctypes.byref(span))

    def round_rect(self, x, y, w, h, r, color):
        x, y, w, h, r = int(x), int(y), int(w), int(h), int(r)
        r = max(0, min(r, w // 2, h // 2))
        self.rect(x + r, y, w - 2 * r, h, color)
        self.rect(x, y + r, w, h - 2 * r, color)
        for cx, cy in ((x + r, y + r), (x + w - r - 1, y + r),
                       (x + r, y + h - r - 1), (x + w - r - 1, y + h - r - 1)):
            self.fill_circle(cx, cy, r, color)

    def frame(self, x, y, w, h, color, thickness):
        """An outline drawn as four filled bars — SDL's DrawRect is 1px only."""
        t = max(1, int(thickness))
        x, y, w, h = int(x), int(y), int(w), int(h)
        self.rect(x, y, w, t, color)
        self.rect(x, y + h - t, w, t, color)
        self.rect(x, y, t, h, color)
        self.rect(x + w - t, y, t, h, color)

    def text(self, s, x, y, size="body", color=(230, 230, 235), bold=False,
             center=False, right=False, emoji=False):
        px = self.size.get(size, size if isinstance(size, int) else 23)
        got = self._texture(str(s), px, tuple(color), bold, emoji)
        if not got:
            return 0
        tex, tw, th = got
        dx = x - tw // 2 if center else (x - tw if right else x)
        dst = SDL_Rect(int(dx), int(y), tw, th)
        self.sdl.SDL_RenderCopy(self.renderer, tex, None, ctypes.byref(dst))
        return th

    def glyph_centered(self, ch, cx, cy, size="body", color=(230, 230, 235),
                       bold=True):
        """Draw one character centred on its INK, not on its text box.

        A rendered surface spans the font's full ascent and descent, so
        centring by surface height leaves marks like + and \u2013 sitting high
        inside a circle. Glyph metrics give the real extents.
        """
        px = self.size.get(size, size if isinstance(size, int) else 23)
        font = self._font(px, bold)
        minx, maxx, miny, maxy, adv = (ctypes.c_int() for _ in range(5))
        ok = self.ttf.TTF_GlyphMetrics(font, ord(ch[0]),
                                       ctypes.byref(minx), ctypes.byref(maxx),
                                       ctypes.byref(miny), ctypes.byref(maxy),
                                       ctypes.byref(adv)) == 0
        got = self._texture(str(ch), px, tuple(color), bold, False)
        if not got:
            return
        tex, tw, th = got
        if ok:
            ascent = self.ttf.TTF_FontAscent(font)
            top = cy - (ascent - (miny.value + maxy.value) / 2)
        else:
            top = cy - th / 2
        dst = SDL_Rect(int(cx - tw / 2), int(top), tw, th)
        self.sdl.SDL_RenderCopy(self.renderer, tex, None, ctypes.byref(dst))

    def text_size(self, s, size="body", bold=False, emoji=False):
        px = self.size.get(size, size if isinstance(size, int) else 23)
        got = self._texture(str(s), px, (255, 255, 255), bold, emoji)
        return (got[1], got[2]) if got else (0, 0)

    def present(self):
        self.sdl.SDL_RenderPresent(self.renderer)

    # ----------------------------------------------------------------- events

    def poll(self):
        """Yield (kind, payload) tuples for the events we care about."""
        buf = ctypes.create_string_buffer(128)
        import struct
        while self.sdl.SDL_PollEvent(buf):
            etype = struct.unpack_from("I", buf, 0)[0]
            if etype == SDL_QUIT:
                yield ("quit", None)
            elif etype == SDL_KEYDOWN:
                yield ("key", struct.unpack_from("i", buf, 20)[0])
            elif etype == SDL_CONTROLLERBUTTONDOWN:
                yield ("button", (struct.unpack_from("i", buf, 8)[0],
                                  struct.unpack_from("B", buf, 12)[0]))
            elif etype == SDL_CONTROLLERBUTTONUP:
                yield ("release", (struct.unpack_from("i", buf, 8)[0],
                                   struct.unpack_from("B", buf, 12)[0]))
            elif etype == SDL_CONTROLLERAXISMOTION:
                # which(i)@8, axis(B)@12, then 3 pad bytes, value(h)@16
                yield ("axis", (struct.unpack_from("i", buf, 8)[0],
                                struct.unpack_from("B", buf, 12)[0],
                                struct.unpack_from("h", buf, 16)[0]))
            elif etype in (SDL_CONTROLLERDEVICEADDED, SDL_CONTROLLERDEVICEREMOVED):
                yield ("devices", etype == SDL_CONTROLLERDEVICEADDED)

    def close(self):
        for tex, _, _ in self._cache.values():
            self.sdl.SDL_DestroyTexture(tex)
        for got in self._images.values():
            if got:
                self.sdl.SDL_DestroyTexture(got[0])
        for f in self._fonts.values():
            self.ttf.TTF_CloseFont(f)
        self.ttf.TTF_Quit()
        if self.renderer:
            self.sdl.SDL_DestroyRenderer(self.renderer)
        if self.window:
            self.sdl.SDL_DestroyWindow(self.window)
