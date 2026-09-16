#!/usr/bin/env python3
"""Stage the GameCube button glyphs into art/gc/ from Zacksly's pack.

The pack is not vendored — only the handful of files preflight draws, under
the names the code asks for. Run it again with the pack unzipped somewhere
to rebuild them:

    ./tools/stage-gc-art.py "~/Downloads/GameCube Button Icons and Controls" \\
                            "~/Downloads/green gc.png"

Two things happen on the way in, and both count as modifications under
CC BY 3.0, so they are stated here and in art/gc/LICENSE-zacksly.txt:

  * the pack ships palette and RGB PNGs; preflight's loader takes RGBA only,
    so everything is converted;
  * A, B, X and Y are built from the layout mock-up rather than straight
    from the pack: the mock-up's blobs are the pack's own shapes, but scaled
    the way the layout wants them and with X already stood on its end. Each
    one gets the pack's letter dropped back in UPRIGHT and centred on the
    blob's own middle — the pack draws the labels off to one side to suit the
    tilt of a real pad, which reads as a mistake once the blobs are drawn
    square. The filled twin is rebuilt the same way, so a press knocks the
    letter out of exactly where the letter is.

Idle glyphs come from Buttons Outline; the `_on` twins come from Buttons
Full Solid, whose letter is knocked out of the silhouette — that is what
makes a press read as the player's colour with the label showing the bay
through it, the same trick the Switch set uses.
"""

import os
import sys

from PIL import Image, ImageChops, ImageFilter

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "art", "gc")
RES = "256w"

# preflight's name -> the pack's file name.
# The mock-up the face buttons come from, and where each blob sits in it.
# Keyed by which corner of the cluster it is, since the file is one picture
# of all four: A is the biggest, then X, Y, B by area.
MOCKUP = "green gc.png"
FACES = {"a": "A", "b": "B", "x": "X", "y": "Y"}
ROTATED = ("x",)                 # stood on its end in the mock-up

OUTLINE = {
    "z": "Right Bumper",          # the Z pill, not a bumper on this pad
    "l": "L Analog", "r": "R Analog",   # GameCube's shoulders are analog
    "start": "Start Pause",
    "dpad": "D-Pad",
    "dpad_up": "D-Pad Up", "dpad_down": "D-Pad Down",
    "dpad_left": "D-Pad Left", "dpad_right": "D-Pad Right",
    "stick_l": "Control Stick", "stick_r": "C Stick",
}
# Only the ones a player can press get a filled twin. The d-pad's pressed
# arm is already part of its directional art, and a stick is not a label.
PRESSED = ("z", "l", "r", "start")


def components(mask):
    """Connected runs of ink in an alpha channel, largest first.

    The pack draws each glyph as a shape plus a separate letter, so this is
    what separates the two: the shape is always the biggest piece.
    """
    w, h = mask.size
    a = mask.load()
    seen = bytearray(w * h)
    out = []
    for y0 in range(h):
        for x0 in range(w):
            if seen[y0 * w + x0] or a[x0, y0] < 128:
                continue
            stack, pts = [(x0, y0)], []
            seen[y0 * w + x0] = 1
            while stack:
                x, y = stack.pop()
                pts.append((x, y))
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = x + dx, y + dy
                    if (0 <= nx < w and 0 <= ny < h
                            and not seen[ny * w + nx] and a[nx, ny] >= 128):
                        seen[ny * w + nx] = 1
                        stack.append((nx, ny))
            out.append(pts)
    return sorted(out, key=len, reverse=True)


def split_letter(im):
    """(shape, letter) — the glyph with its label erased, and the label alone.

    The letter's own component is grown by a couple of pixels before it is
    cut out, because the pack's art is antialiased: the faint edge left by
    an exact cut showed up as a ghost of the old letter once the shape was
    turned. The letter keeps the full canvas, so pasting it back unmoved
    lands it where it started.
    """
    alpha = im.getchannel("A")
    parts = components(alpha)
    if len(parts) < 2:
        return im, None
    core = Image.new("L", im.size, 0)
    for x, y in parts[1]:
        core.putpixel((x, y), 255)
    grown = core.filter(ImageFilter.MaxFilter(5))
    letter = Image.new("RGBA", im.size, (255, 255, 255, 0))
    letter.putalpha(ImageChops.multiply(alpha, grown))
    shape = im.copy()
    shape.putalpha(ImageChops.subtract(alpha, letter.getchannel("A")))
    return shape, letter


def fill_holes(im):
    """(closed, letter) — the silhouette with its knockout filled in, and the
    knockout on its own as a mask.

    A filled glyph wears its label as a hole rather than as ink, so this is
    how the label is recovered from one. Anything transparent the canvas edge
    cannot reach is inside the silhouette, which on this pack means the letter
    and nothing else. The hole is grown before it is filled, for the same
    antialiasing reason as split_letter.
    """
    alpha = im.getchannel("A")
    hole = alpha.point(lambda v: 255 if v < 128 else 0)
    core = Image.new("L", im.size, 0)
    for pts in components(hole)[1:]:
        for x, y in pts:
            core.putpixel((x, y), 255)
    grown = core.filter(ImageFilter.MaxFilter(5))
    out = im.copy()
    out.putalpha(ImageChops.lighter(alpha, grown))
    letter = ImageChops.multiply(ImageChops.invert(alpha), grown)
    return out, letter


def centre_on(shape, letter):
    """Put the letter in the middle of the shape it labels.

    Centred on the shape's bounding box, not its centre of mass: these blobs
    lean, and the eye reads the box.
    """
    sb, lb = shape.getchannel("A").getbbox(), letter.getchannel("A").getbbox()
    patch = letter.crop(lb)
    x = (sb[0] + sb[2] - patch.width) // 2
    y = (sb[1] + sb[3] - patch.height) // 2
    out = Image.new("RGBA", shape.size, (255, 255, 255, 0))
    out.paste(patch, (x, y))
    return out


def start_button(im, inset=1.0):
    """Start's round button alone, centred and scaled like a lettered glyph.

    The caption is a separate piece of ink above the circle, and the circle
    is always the biggest piece, so this is just "keep the largest part".
    """
    parts = components(im.getchannel("A"))
    keep = Image.new("RGBA", im.size, (255, 255, 255, 0))
    for x, y in parts[0]:
        keep.putpixel((x, y), im.getpixel((x, y)))
    box = keep.getchannel("A").getbbox()
    side = im.size[0]
    # 0.70 of the canvas, the share the lettered glyphs' ink takes up. The
    # filled twin comes in further, so a press shows as a disc inside the
    # ring rather than as the same circle very slightly larger.
    want = int(round(side * 0.70 * inset))
    patch = keep.crop(box).resize((want, want), Image.LANCZOS)
    out = Image.new("RGBA", im.size, (255, 255, 255, 0))
    out.paste(patch, ((side - want) // 2, (side - want) // 2))
    return out


def mockup_blobs(path):
    """The mock-up's four blobs, keyed the way FACES is.

    The file is one picture of the whole cluster, drawn in green with faint
    labels of its own. Only the four outlines are wanted: they are the four
    biggest pieces of ink, and the labels are dropped in favour of the pack's
    crisper ones.
    """
    im = Image.open(path).convert("RGBA")
    parts = components(im.getchannel("A"))
    out = {}
    for pts in parts[:len(FACES)]:
        piece = Image.new("RGBA", im.size, (255, 255, 255, 0))
        for x, y in pts:
            piece.putpixel((x, y), im.getpixel((x, y)))
        out[len(pts)] = piece
    # Biggest first is A, then X, Y, B — by area of ink, which is stable
    # across a recolour and does not care where the file puts them.
    order = sorted(out, reverse=True)
    blobs = dict(zip(("a", "x", "y", "b"), (out[n] for n in order)))
    # One line weight for all four, set by whichever was drawn finest.
    finest = min(stroke_erosions(b) for b in blobs.values())
    return {k: thin_to(v, finest) for k, v in blobs.items()}


def interior(shape):
    """The blob's own middle: the centroid of the area its outline encloses.

    Not the bounding box — these shapes lean and bulge, and a letter centred
    on the box of the X blob ends up visibly to one side of the hole it is
    meant to sit in.
    """
    a = shape.getchannel("A")
    w, h = a.size
    ink = a.load()
    # Flood the transparent background inward from the edge; whatever
    # transparency it cannot reach is inside the outline.
    outside = bytearray(w * h)
    stack = [(x, y) for x in range(w) for y in (0, h - 1)]
    stack += [(x, y) for y in range(h) for x in (0, w - 1)]
    stack = [p for p in stack if ink[p] < 128]
    for p in stack:
        outside[p[1] * w + p[0]] = 1
    while stack:
        x, y = stack.pop()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if (0 <= nx < w and 0 <= ny < h and not outside[ny * w + nx]
                    and ink[nx, ny] < 128):
                outside[ny * w + nx] = 1
                stack.append((nx, ny))
    pts = [(x, y) for y in range(h) for x in range(w)
           if ink[x, y] < 128 and not outside[y * w + x]]
    if not pts:
        bb = a.getbbox()
        return (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
    return (sum(p[0] for p in pts) / len(pts),
            sum(p[1] for p in pts) / len(pts))


def stroke_erosions(im):
    """How many erosions a shape survives — half its stroke width, in effect.

    Used to compare one blob's line weight with another's without having to
    find a centreline: a ring twice as thick takes twice as many.
    """
    m = im.getchannel("A").point(lambda v: 255 if v >= 128 else 0)
    n = 0
    while m.getbbox() and n < 60:
        m = m.filter(ImageFilter.MinFilter(3))
        n += 1
    return n


def thin_to(im, target):
    """Erode a blob's outline until it is as fine as `target` erosions.

    The mock-up draws A with a noticeably heavier line than B, X and Y — 17
    pixels against 11 — which is plain to see once the four sit side by side.
    Eroding takes a pixel off the outside and a pixel off the inside at once,
    so the line thins about its own middle and the blob keeps its shape.
    """
    out = im
    for _ in range(max(0, stroke_erosions(im) - target)):
        out = out.filter(ImageFilter.MinFilter(3))
    return out


def tight(im):
    """The image cropped to its ink."""
    return im.crop(im.getchannel("A").getbbox())


def white(im):
    """Ink repainted pure white, alpha kept.

    Everything preflight draws is white so SDL's colour modulation can tint
    it — the mock-up arrives already green, which would only ever tint to a
    darker green.
    """
    out = Image.new("RGBA", im.size, (255, 255, 255, 0))
    out.putalpha(im.getchannel("A"))
    return out


FILL_GAP = 5                    # canvas pixels between a press and its ring


def solid_from(shape, gap):
    """A press, cut from the blob's own outline rather than from other art.

    The pack's filled glyphs are a different shape from the mock-up's blobs —
    the mock-up scaled X and Y unevenly — so stretching one onto the other
    left the fill visibly out of true with the ring around it. Filling the
    blob's own interior and then eroding it cannot go out of true: every
    point of the result is the same distance inside the line that encircles
    it, whatever shape that line happens to be.
    """
    a = shape.getchannel("A").point(lambda v: 255 if v >= 128 else 0)
    w, h = a.size
    ink = a.load()
    outside = bytearray(w * h)
    stack = [(x, y) for x in range(w) for y in (0, h - 1)]
    stack += [(x, y) for y in range(h) for x in (0, w - 1)]
    stack = [q for q in stack if ink[q] < 128]
    for q in stack:
        outside[q[1] * w + q[0]] = 1
    while stack:
        x, y = stack.pop()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if (0 <= nx < w and 0 <= ny < h and not outside[ny * w + nx]
                    and ink[nx, ny] < 128):
                outside[ny * w + nx] = 1
                stack.append((nx, ny))
    full = Image.new("L", (w, h), 0)
    fp = full.load()
    for y in range(h):
        for x in range(w):
            if ink[x, y] >= 128 or not outside[y * w + x]:
                fp[x, y] = 255
    # In from the outer edge by the line's own width plus the gap, so the
    # wysiwyg colour shows all the way round a press.
    for _ in range(stroke_erosions(shape) * 2 + gap):
        full = full.filter(ImageFilter.MinFilter(3))
    out = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    # A touch of blur, or the eroded edge is a staircase.
    out.putalpha(full.filter(ImageFilter.GaussianBlur(0.6)))
    return out


def face(blob, pack, key, canvas=256, fill=0.90):
    """One face button, from the mock-up's blob and the pack's letter.

    The blob is scaled to `fill` of the canvas and centred. Its press is cut
    from the blob itself, so the two are concentric by construction. The
    letter is scaled uniformly, so it is never stretched, and placed on the
    blob's interior centroid — the pack draws its labels off to one side to
    suit the tilt of a real pad, which reads as a mistake once the blobs are
    drawn square.

    Returns (outline, filled).
    """
    src = FACES[key]
    _shape, letter = split_letter(load(pack, "Buttons Outline", src))
    pack_ink = tight(load(pack, "Buttons Outline", src))
    if key in ROTATED:
        pack_ink = pack_ink.rotate(90, Image.BICUBIC, expand=True)

    blob = tight(white(blob))
    scale = fill * canvas / max(blob.size)
    bw = max(1, round(blob.width * scale))
    bh = max(1, round(blob.height * scale))
    out = Image.new("RGBA", (canvas, canvas), (255, 255, 255, 0))
    out.paste(blob.resize((bw, bh), Image.LANCZOS),
              ((canvas - bw) // 2, (canvas - bh) // 2))

    filled = solid_from(out, FILL_GAP)

    # The letter, undistorted, on the blob's own middle.
    mark = tight(letter)
    uniform = min(bw / pack_ink.width, bh / pack_ink.height)
    mark = mark.resize((max(1, round(mark.width * uniform)),
                        max(1, round(mark.height * uniform))), Image.LANCZOS)
    cx, cy = interior(out)
    stamped = Image.new("RGBA", (canvas, canvas), (255, 255, 255, 0))
    stamped.paste(mark, (int(round(cx - mark.width / 2)),
                         int(round(cy - mark.height / 2))))

    out.alpha_composite(stamped)
    # A filled glyph wears its label as a hole, so the letter is subtracted
    # there: that is what makes a press read as a negative of the button.
    filled.putalpha(ImageChops.subtract(filled.getchannel("A"),
                                        stamped.getchannel("A")))
    return out, filled


def load(pack, folder, name):
    return Image.open(os.path.join(pack, folder, "White", RES,
                                   name + ".png")).convert("RGBA")


def main():
    if not 2 <= len(sys.argv) <= 3:
        sys.exit(__doc__)
    pack = os.path.expanduser(sys.argv[1])
    mock = os.path.expanduser(sys.argv[2] if len(sys.argv) > 2
                              else os.path.join("~/Downloads", MOCKUP))
    os.makedirs(OUT, exist_ok=True)

    for name, src in OUTLINE.items():
        load(pack, "Buttons Outline", src).save(os.path.join(OUT, name + ".png"))
    for name in PRESSED:
        load(pack, "Buttons Full Solid", OUTLINE[name]).save(
            os.path.join(OUT, name + "_on.png"))

    # The legend has no room for Start's own START/PAUSE caption, so it gets
    # the button on its own, blown up to sit at the same weight as the
    # lettered glyphs beside it. The map keeps the captioned version.
    for folder, name, inset in (("Buttons Outline", "start_plain", 1.0),
                                ("Buttons Full Solid", "start_plain_on",
                                 0.82)):
        start_button(load(pack, folder, "Start Pause"), inset).save(
            os.path.join(OUT, name + ".png"))

    # The face buttons, from the mock-up's blobs.
    blobs = mockup_blobs(mock)
    for key in FACES:
        outline, filled = face(blobs[key], pack, key)
        outline.save(os.path.join(OUT, key + ".png"))
        filled.save(os.path.join(OUT, key + "_on.png"))

    for src, dst in (("LICENSE.txt", "LICENSE-zacksly.txt"),):
        with open(os.path.join(pack, src), encoding="utf-8", errors="replace") as fh:
            body = fh.read()
        with open(os.path.join(OUT, dst), "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.write("\n\n" + "-" * 90 + "\n\n"
                     "Modified for preflight: converted to RGBA, and X.png "
                     "rotated a quarter turn so it stands on its end as it "
                     "does on a real GameCube pad. See tools/stage-gc-art.py.\n")

    print(f"staged {len(OUTLINE) + len(PRESSED) + 2 * len(FACES) + 3} "
          f"files into {OUT}")


if __name__ == "__main__":
    main()
