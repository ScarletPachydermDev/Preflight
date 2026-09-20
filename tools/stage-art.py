#!/usr/bin/env python3
"""Stage the button glyphs preflight draws, for both maps.

Everything comes from one pack — Kenney's Input Prompts, CC0 — so the two
maps are the same hand at the same weight, and the only thing that differs
between them is which console's buttons are drawn. Run it again with the pack
unzipped somewhere to rebuild:

    ./tools/stage-art.py "~/Claude/selfsteam assets/inputs/kenney_input-prompts_1.5"

Only the files preflight draws are staged, under the names the code asks for.
Three things are derived rather than copied, and each earns its keep:

  * `<n>_letter` — a face button's label on its own, so the outline round it
    can turn green or amber while the label stays white.
  * `<n>_press` — the filled twin with its label hole closed, drawn UNDER
    the outline so the colour runs uniformly up to a full-width edge.
  * `dpad_<direction>` — the pack draws a pressed arm in red on a white
    cross; the arm alone is kept, so a press lights that arm rather than the
    whole d-pad.

A face button's line is also thinned or thickened on the way in, so that every
outline on screen comes out the SAME WEIGHT however big its button is. A line
scales with the box it is drawn in, and the GameCube map draws A half again as
large as anything in the top row and B smaller than it — so one 6px line in
the file became 6.5px on A and 3.5px on B. The correction is read out of
preflight's own layout rather than guessed, so re-run this after changing it.

Everything is repainted white on the way in, because preflight tints what it
draws and SDL's colour modulation can only darken. The C stick is the one
exception: it keeps Kenney's yellow, and preflight knows not to tint it.
"""

import os
import shutil
import sys

from PIL import Image, ImageChops, ImageFilter

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWITCH_DIR = os.path.join(HERE, "art")
GC_DIR = os.path.join(SWITCH_DIR, "gc")
GC_PACK = os.path.join("Nintendo Gamecube", "Double")

# A face button, or anything else with a label inside an outline: staged as
# the outline, its label, and a press that fits within the outline.
GC_LABELLED = {
    "a": "button_a", "b": "button_b",
    # Tilted, because a GameCube's X and Y sit at an angle beside A.
    "x": "button_x_tilted", "y": "button_y_tilted",
}
# Outline plus a filled twin at the same size, which is all a control needs
# when nothing recolours its outline.
GC_PAIRS = {
    "z": "button_z",
    "l": "trigger_l", "r": "trigger_r",     # analog on this pad
}
# Taken as they are.
GC_PLAIN = {
    "dpad": "dpad",
    "stick_l": "stick_grip_top",
}
# Turned a few degrees on the way in, clockwise, about the ink's own centre.
# Kenney's tilted X leans its top toward A; this stands it up a little
# further. The BLOB turns and the label does not — a tilted letter reads as a
# mistake, which is the whole reason the pack's untilted glyphs were passed
# over in the first place.
GC_TILT = {"x": -8}

# Kept in the pack's own colour — see the note about the C stick above.
GC_COLOURED = {"stick_r": "stick_c_color_top"}
GC_ARMS = ("dpad_up", "dpad_down", "dpad_left", "dpad_right")

SWITCH_FACES = ("a", "b", "x", "y")

N64_DIR = os.path.join(SWITCH_DIR, "n64")
# Drawn by the user to match Kenney's hand, since the pack has no N64 set.
# Colour is dropped on the way in like everything else — the map tints what
# it draws — except the C buttons, which are yellow on the pad and stay so.
N64_ICONS = os.path.join("..", "selfsteam assets", "n64 kenney'd icons",
                         "png", "512")
N64_SIDE = 128                     # the canvas every other glyph here uses
N64_INK = 0.78                     # how much of it the ink fills


def components(mask):
    """Connected runs of ink in an alpha channel, largest first.

    Kenney draws a glyph as a shape plus a separate label, so this is what
    separates the two: the shape is always the biggest piece.
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


def white(im):
    """Ink repainted pure white, alpha kept.

    Everything preflight draws is white, because SDL's colour modulation can
    only darken: white is the only ink that can become any colour.
    """
    out = Image.new("RGBA", im.size, (255, 255, 255, 0))
    out.putalpha(im.getchannel("A"))
    return out


def split_letter(im):
    """(shape, letter) — the glyph with its label erased, and the label alone.

    The label's own component is grown by a couple of pixels before it is cut
    out, because the art is antialiased: the faint edge left by an exact cut
    showed up as a ghost of the label once the shape was recoloured.
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
    """The silhouette with its knocked-out label filled in.

    A filled glyph wears its label as a hole. The label is drawn back on top
    in white now, so the hole is closed first — left in, it showed as a dark
    label-shaped halo around the white one.
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
    return out


def stroke_erosions(im):
    """How many erosions a shape survives — half its stroke width, in effect.

    Measures the THICKEST part of the ink, since that is the last to go.
    """
    m = im.getchannel("A").point(lambda v: 255 if v >= 128 else 0)
    n = 0
    while m.getbbox() and n < 60:
        m = m.filter(ImageFilter.MinFilter(3))
        n += 1
    return n


def turn(im, degrees):
    """Rotate a glyph about its ink's own centre, so it does not wander.

    Rotating about the canvas centre would shift a glyph whose ink is off
    centre; these all have margin to spare, so a few degrees never clips.
    """
    if not degrees:
        return im
    bb = im.getchannel("A").getbbox()
    centre = ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)
    return im.rotate(degrees, Image.BICUBIC, center=centre)


def press_from(filled):
    """A press: the filled glyph, with its label hole closed.

    Nothing is eroded. The press is drawn UNDER the outline, so the outline
    paints its own edge at full width and the colour runs uniformly up to it.
    Eroding the fill to sit inside the outline instead — which is what this
    did first — could only trade a dark ring inside every pressed button for
    an outline drawn half its proper weight.
    """
    return fill_holes(filled)


def start_button(im, inset=1.0):
    """Start's round button alone, centred and scaled like a lettered glyph.

    Kenney draws START above the button. There is no room for that lettering
    at the size the map draws it, and the label beside it in the legend
    already says what holding it does — so the caption goes and the button is
    blown up to sit at the same weight as the glyphs around it.
    """
    parts = components(im.getchannel("A"))
    keep = Image.new("RGBA", im.size, (255, 255, 255, 0))
    for x, y in parts[0]:
        keep.putpixel((x, y), im.getpixel((x, y)))
    box = keep.getchannel("A").getbbox()
    side = im.size[0]
    want = int(round(side * 0.70 * inset))
    patch = keep.crop(box).resize((want, want), Image.LANCZOS)
    out = Image.new("RGBA", im.size, (255, 255, 255, 0))
    out.paste(patch, ((side - want) // 2, (side - want) // 2))
    return out


def arm_only(im):
    """The pressed arm of a d-pad, on its own and in white.

    The pack draws it in red on a white cross. Keeping the whole cross would
    light the whole d-pad when one direction is pressed; keeping the arm lets
    a press show where it happened, and a diagonal show both arms for free.
    """
    px = im.load()
    w, h = im.size
    out = Image.new("RGBA", im.size, (255, 255, 255, 0))
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a > 40 and r > 150 and g < 120:
                out.putpixel((x, y), (255, 255, 255, a))
    return out


def load(pack, name):
    return Image.open(os.path.join(pack, GC_PACK,
                                   "gamecube_" + name + ".png")).convert("RGBA")


def layout_weights():
    """{name: how much thicker than the reference its line will be drawn}.

    Read from preflight's own Layout, at whatever size, because the ratio
    between two boxes does not depend on the window: the lanes and the
    cluster's fit are all fractions of the strip's height.
    """
    import importlib.util
    if HERE not in sys.path:
        sys.path.insert(0, HERE)      # preflight imports sdlui beside it
    spec = importlib.util.spec_from_file_location(
        "pf", os.path.join(HERE, "preflight.py"))
    pf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pf)

    class Bay:
        dw = 1000.0
        dh = 1000.0 / pf.PAD_ASPECT
        ox = oy = 0.0

        def S(self, u):
            return max(2.0, u * self.dh)

        def Y(self, v):
            return self.oy + v * self.dh

    lay = pf.Layout(Bay())
    _centre, fit = pf.gc_cluster(lay)
    # The top row is the reference: it is the most of what a player looks at,
    # and both maps draw it at one size.
    # A glyph stretched unevenly has a line that is not one width either, so
    # the mean of the two is what gets matched.
    return {name: (lay.fside * (sw + sh) / 2 * fit) / lay.top_box
            for name, (_offset, (sw, sh)) in pf.GC_FACES.items()}


def match_weight(im, factor, fine=4):
    """Thin or thicken a glyph's line so it is drawn at the reference weight.

    A line drawn `factor` times too thick needs to be that much thinner in
    the file. Erosion takes a pixel off each side of it per pass, so the work
    is done on a 4x copy: a pass there is a quarter of a pixel here, and at
    whole-pixel granularity A overshot from 6px to 4px when it wanted 4.7.
    """
    have = stroke_erosions(im) * 2
    if not have:
        return im
    delta = (have - have / factor) / 2          # per side, in real pixels
    passes = int(round(delta * fine))
    if not passes:
        return im
    big = im.resize((im.width * fine, im.height * fine), Image.LANCZOS)
    for _ in range(abs(passes)):
        big = big.filter(ImageFilter.MinFilter(3) if passes > 0
                         else ImageFilter.MaxFilter(3))
    return big.resize(im.size, Image.LANCZOS)


def gamecube(pack):
    """The GameCube set, from the pack."""
    os.makedirs(GC_DIR, exist_ok=True)
    count = 0
    weights = layout_weights()
    for key, src in GC_LABELLED.items():
        outline = load(pack, src + "_outline")
        filled = load(pack, src)
        _shape, letter = split_letter(outline)
        if letter is None:
            sys.exit(f"{src}: expected a label inside the outline")
        # The label is left alone: it is lettering, not a line, and reads as
        # part of the button's size rather than as a weight.
        ring, _ = split_letter(outline)
        ring = match_weight(ring, weights.get(key, 1.0))
        matched = turn(ring, GC_TILT.get(key, 0))
        matched.alpha_composite(letter)
        white(matched).save(os.path.join(GC_DIR, key + ".png"))
        white(letter).save(os.path.join(GC_DIR, key + "_letter.png"))
        turn(press_from(filled), GC_TILT.get(key, 0)).save(
            os.path.join(GC_DIR, key + "_press.png"))
        count += 3
    for key, src in GC_PAIRS.items():
        white(load(pack, src + "_outline")).save(
            os.path.join(GC_DIR, key + ".png"))
        white(load(pack, src)).save(os.path.join(GC_DIR, key + "_on.png"))
        count += 2
    for key, src in GC_PLAIN.items():
        white(load(pack, src)).save(os.path.join(GC_DIR, key + ".png"))
        count += 1
    for key, src in GC_COLOURED.items():
        load(pack, src).save(os.path.join(GC_DIR, key + ".png"))
        count += 1
    for key in GC_ARMS:
        arm_only(load(pack, key)).save(os.path.join(GC_DIR, key + ".png"))
        count += 1
    # The outline for the idle button, the filled one for a press: taking
    # both from the outline left a press looking like a smaller ring.
    for name, src, inset in (("start_plain", "button_start_outline", 1.0),
                             ("start_plain_on", "button_start", 0.82)):
        start_button(white(load(pack, src)), inset).save(
            os.path.join(GC_DIR, name + ".png"))
        count += 1
    return count


def switch_faces():
    """The Switch set's two derived layers, from the art already committed.

    Nothing is copied here: `a.png` and the rest are staged already, and what
    they lack are the layers that let an outline be recoloured while its
    label stays white. Derived from committed art, so running this twice is
    harmless.
    """
    for key in SWITCH_FACES:
        outline = Image.open(os.path.join(SWITCH_DIR, key + ".png")).convert("RGBA")
        filled = Image.open(os.path.join(SWITCH_DIR, key + "_on.png")).convert("RGBA")
        _ring, letter = split_letter(outline)
        if letter is None:
            sys.exit(f"art/{key}.png: expected a label inside the outline")
        letter.save(os.path.join(SWITCH_DIR, key + "_letter.png"))
        press_from(filled).save(os.path.join(SWITCH_DIR, key + "_press.png"))
    return 2 * len(SWITCH_FACES)


def fit(im, side=N64_SIDE, ink=N64_INK, square=True):
    """One glyph, its ink scaled to the same fraction of the same canvas.

    The layout file gives each N64 button a SIZE, and a size only means
    something if every glyph wears its margin the same way. Kenney's own
    canvases do not: a shoulder pill and a face circle are drawn with
    different air around them. `square=False` keeps a wide glyph's
    proportions, for the shoulders.
    """
    box = im.getchannel("A").getbbox()
    cut = im.crop(box)
    if square:
        scale = side * ink / max(cut.size)
    else:
        scale = side * ink / cut.size[0]
    size = (max(1, int(round(cut.size[0] * scale))),
            max(1, int(round(cut.size[1] * scale))))
    patch = cut.resize(size, Image.LANCZOS)
    out = Image.new("RGBA", (side, side), (255, 255, 255, 0))
    out.paste(patch, ((side - size[0]) // 2, (side - size[1]) // 2))
    return out


def fit_like(im, model, side=N64_SIDE, ink=N64_INK, square=True):
    """Place one glyph using ANOTHER's framing.

    A label is not a glyph in its own right: it belongs at the size and place
    its ring puts it. Fitting it on its own blew every letter up to fill the
    canvas, which put A's serif outside A's circle.
    """
    box = model.getchannel("A").getbbox()
    scale = side * ink / (max(box[2] - box[0], box[3] - box[1]) if square
                          else box[2] - box[0])
    size = (max(1, int(round(im.size[0] * scale))),
            max(1, int(round(im.size[1] * scale))))
    patch = im.resize(size, Image.LANCZOS)
    out = Image.new("RGBA", (side, side), (255, 255, 255, 0))
    # Where the model's ink lands, so everything drawn with it lands with it.
    cx = (box[0] + box[2]) / 2 * scale
    cy = (box[1] + box[3]) / 2 * scale
    out.paste(patch, (int(round(side / 2 - cx)), int(round(side / 2 - cy))))
    return out


def solid(shape, letter=None, colour=None):
    """The shape filled in, with any label left as a hole.

    This is what Kenney's filled twin looks like, and the pack has one for
    every glyph it draws. These are outlines only, so the twin is derived:
    close the ring, then punch the label back out.

    The fill takes its colour from `colour`, because the pixels being filled
    were transparent and carry whatever RGB the canvas was made with — which
    is how the first yellow C button came out with a white middle.
    """
    disc = fill_holes(shape)
    if colour is not None:
        tint = Image.new("RGBA", disc.size, colour)
        tint.putalpha(disc.getchannel("A"))
        disc = tint
    if letter is not None:
        disc.putalpha(ImageChops.subtract(disc.getchannel("A"),
                                          letter.getchannel("A")))
    return disc


def ink_colour(im):
    """The colour of a glyph's own ink, as the fill should be."""
    px = im.load()
    for y in range(im.size[1]):
        for x in range(im.size[0]):
            r, g, b, a = px[x, y]
            if a > 200:
                return (r, g, b, 255)
    return (255, 255, 255, 255)


def n64_source(name):
    here = os.path.join(HERE, N64_ICONS, f"n64-{name}.png")
    return Image.open(here).convert("RGBA")


def n64_part(im, part, keep_colour=False):
    """One piece of a multi-button glyph, on a canvas of its own.

    The C cluster arrives as a single picture of four buttons and a C. Each
    button has to light on its own, so each is cut out here — the same reason
    the d-pad's arms are cut out of its cross.
    """
    out = Image.new("RGBA", im.size, (255, 255, 255, 0))
    for x, y in part:
        px = im.getpixel((x, y))
        out.putpixel((x, y), px if keep_colour else (255, 255, 255, px[3]))
    return out


def n64():
    """The N64 set, from the user's own icons plus art already staged."""
    os.makedirs(N64_DIR, exist_ok=True)
    count = 0

    # Face buttons: the ring alone, the label alone, and a solid press. Same
    # three layers as every other map's faces.
    for key in ("a", "b"):
        ring, letter = split_letter(white(n64_source(key)))
        if letter is None:
            sys.exit(f"n64-{key}.png: expected a label inside the ring")
        flat = ring.copy()
        flat.alpha_composite(letter)
        fit_like(flat, flat).save(os.path.join(N64_DIR, key + ".png"))
        fit_like(letter, flat).save(os.path.join(N64_DIR, key + "_letter.png"))
        fit_like(press_from(solid(ring, colour=ink_colour(ring))),
                 flat).save(os.path.join(N64_DIR, key + "_press.png"))
        count += 3

    # Z and the shoulders: one picture each, plus its filled twin. The
    # shoulders keep their proportions — they are wide, and squaring them
    # would turn a pill into a circle.
    for key, square in (("z", True), ("l", False), ("r", False)):
        shape, letter = split_letter(white(n64_source(key)))
        flat = shape.copy()
        if letter is not None:
            flat.alpha_composite(letter)
        fit_like(flat, flat, square=square).save(
            os.path.join(N64_DIR, key + ".png"))
        fit_like(solid(shape, letter, colour=ink_colour(shape)), flat,
                 square=square).save(
            os.path.join(N64_DIR, key + "_on.png"))
        count += 2

    # Start: the ring without its caption, exactly as the GameCube map's is.
    # Start keeps its caption here, unlike the GameCube map's: the icon was
    # drawn with START inside the ring rather than above it, so it fits.
    start = white(n64_source("start"))
    # Both pieces in the SOURCE's own coordinates: start_button rescales what
    # it returns, so filling that and subtracting a caption measured here put
    # the two on different grids and the fill never landed.
    pieces = components(start.getchannel("A"))
    ring = n64_part(start, pieces[0])
    caption = Image.new("RGBA", start.size, (255, 255, 255, 0))
    for part in pieces[1:]:
        caption.alpha_composite(n64_part(start, part))
    fit_like(start, start).save(os.path.join(N64_DIR, "start_plain.png"))
    filled = solid(ring, colour=ink_colour(ring))
    filled.putalpha(ImageChops.subtract(filled.getchannel("A"),
                                        caption.getchannel("A")))
    fit_like(filled, start).save(
        os.path.join(N64_DIR, "start_plain_on.png"))
    count += 2

    # The C cluster: four buttons and a C, cut apart. Yellow is kept — these
    # are the yellow buttons, and tinting them a player colour loses the one
    # thing that says what they are.
    cluster = n64_source("c-pad")
    parts = components(cluster.getchannel("A"))
    rings = sorted(parts[:4], key=lambda p: (min(y for _x, y in p),
                                             min(x for x, _y in p)))
    arrows = sorted(parts[5:9], key=lambda p: (min(y for _x, y in p),
                                               min(x for x, _y in p)))
    order = ("c_up", "c_left", "c_right", "c_down")
    for key, ring_pts, arrow_pts in zip(order, rings, arrows):
        ring = n64_part(cluster, ring_pts, keep_colour=True)
        arrow = n64_part(cluster, arrow_pts, keep_colour=True)
        flat = ring.copy()
        flat.alpha_composite(arrow)
        fit_like(flat, flat).save(os.path.join(N64_DIR, key + ".png"))
        fit_like(solid(ring, arrow, colour=ink_colour(ring)), flat).save(os.path.join(N64_DIR, key + "_on.png"))
        count += 2
    # The C in the middle is a label, not a button: it never lights.
    fit(n64_part(cluster, parts[4], keep_colour=True)).save(
        os.path.join(N64_DIR, "c.png"))
    count += 1

    # The d-pad is the Switch one, as asked, and the stick the GameCube's:
    # both are already staged, so they are copied rather than rebuilt.
    for name in ("dpad", "dpad_up", "dpad_down", "dpad_left", "dpad_right"):
        shutil.copyfile(os.path.join(SWITCH_DIR, name + ".png"),
                        os.path.join(N64_DIR, name + ".png"))
        count += 1
    shutil.copyfile(os.path.join(GC_DIR, "stick_l.png"),
                    os.path.join(N64_DIR, "stick_l.png"))
    return count + 1


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    pack = os.path.expanduser(sys.argv[1])
    licence = os.path.join(pack, "License.txt")
    if not os.path.isdir(os.path.join(pack, GC_PACK)):
        sys.exit(f"no {GC_PACK} in {pack}")
    staged = gamecube(pack) + n64()
    derived = switch_faces()
    if os.path.isfile(licence):
        shutil.copyfile(licence, os.path.join(SWITCH_DIR, "LICENSE-kenney.txt"))
    print(f"staged {staged} files into {GC_DIR}, "
          f"derived {derived} in {SWITCH_DIR}")


if __name__ == "__main__":
    main()
