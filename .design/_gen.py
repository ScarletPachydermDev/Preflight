import json, os

FG, DIM, BG = "#E8E9EE", "#8C8F9B", "#121318"
PC = {1: "#E85D5D", 2: "#569CE8", 3: "#E8BA52", 4: "#6CC782"}
RING_OK, RING_BAD = "#4AE87A", "#E8A23C"     # wysiwyg / ABXY inverted
SHELL = "#34363F"          # blend(BG, FG, 0.20) as preflight draws pill shells
STICK = "#5A5C66"

PACKS = {
    "k":  ("Kenney Input Prompts", "Default fill", "svg"),
    "ko": ("Kenney Input Prompts", "Outline", "svg"),
    "em": ("Switch Button Icons", "Solid Mono", "svg"),
    "ed": ("Switch Button Icons", "Solid Duo", "svg"),
    "x":  ("Xelu Controller Prompts", "Switch set, 100px named", "png"),
    "x2": ("Xelu Controller Prompts", "Switch set, 256px export", "png"),
    "mix": ("Xelu 256 + Kenney shoulders", "ZL L R ZR from Kenney", "png"),
}
# Only the 256px export ships shoulder-shaped L/R and separate left/right
# sticks, so L3 and R3 can be the two different sticks they actually are.
STICKS = {"x2": ("stickl", "stickr"), "mix": ("stickl", "stickr")}

# Per-key source overrides. The mix keeps Xelu's 256px faces, sticks, d-pad
# and plus/minus, and takes the four shoulder buttons from Kenney.
OVERRIDE = {
    "mix": {k: ("x2", "png") for k in
            ["a", "b", "x", "y", "plus", "minus", "stickl", "stickr", "dpad"]}
           | {k: ("k", "svg") for k in ["l", "r", "zl", "zr"]},
}
HEAD = ('<!doctype html>\n<html>\n<head>\n  <meta charset="utf-8">\n'
        '  <script src="./support.js"></script>\n</head>\n<body>\n<x-dc>\n')
HELMET = """<helmet>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans:wght@400;600;700&display=swap">
  <style>
    body { margin: 0; background: %s;
           font-family: "Noto Sans", "DejaVu Sans", system-ui, sans-serif;
           -webkit-font-smoothing: antialiased; }
    a { color: #E8BA52; } a:hover { color: #F0CE7E; }
    .g { display: block; width: auto; }
    .row { display: flex; align-items: center; }
  </style>
</helmet>
""" % BG

TAIL = ('</x-dc>\n<script data-dc-script data-props=\'{"dim":{"editor":"boolean",'
        '"default":false}}\'>\nclass Component extends DCLogic {\n'
        '  renderVals() {\n'
        '    return { shade: this.props.dim ? "brightness(0.4) saturate(0.55)" : "none" };\n'
        '  }\n}\n</script>\n</body>\n</html>\n')


def g(pack, key, h):
    src, ext = OVERRIDE.get(pack, {}).get(key, (pack, PACKS[pack][2]))
    return f'<img class="g" src="{src}_{key}.{ext}" style="height:{h}px" alt="">'


def drawn_pill(label, w=39, h=26, r=13, fs=12):
    return (f'<span style="display:inline-flex;align-items:center;justify-content:center;'
            f'min-width:{w}px;height:{h}px;border-radius:{r}px;background:{SHELL};'
            f'color:{FG};font-size:{fs}px;font-weight:600">{label}</span>')


def drawn_circ(label, d=26, fs=12):
    return (f'<span style="display:inline-flex;align-items:center;justify-content:center;'
            f'width:{d}px;height:{d}px;border-radius:50%;background:{SHELL};'
            f'color:{FG};font-size:{fs}px;font-weight:600">{label}</span>')


def pad(pack, scale=1.0, wys=None):
    """The pad Preflight draws: top strip, then d-pad | sticks | ABXY."""
    sh, ci, fa = int(36*scale), int(33*scale), int(41*scale)
    dp, st = int(70*scale), int(58*scale)
    if pack == "drawn":
        strip = "".join([drawn_pill("ZL"), drawn_pill("L"), drawn_circ("–"),
                         drawn_circ("+"), drawn_pill("R"), drawn_pill("ZR")])
        dpad = (f'<span style="display:block;width:{dp}px;height:{dp}px;background:'
                f'linear-gradient({STICK},{STICK}) center/{dp//3}px {dp}px no-repeat,'
                f'linear-gradient({STICK},{STICK}) center/{dp}px {dp//3}px no-repeat"></span>')
        stick = (f'<span style="display:block;width:{st}px;height:{st}px;'
                 f'border-radius:50%;background:{STICK}"></span>')
        stick_r = stick
        fx, fy, fa_, fb = (drawn_circ("X", fa, int(13*scale)), drawn_circ("Y", fa, int(13*scale)),
                           drawn_circ("A", fa, int(13*scale)), drawn_circ("B", fa, int(13*scale)))
    else:
        strip = "".join(g(pack, k, sh if k in ("zl", "l", "r", "zr") else ci)
                        for k in ["zl", "l", "minus", "plus", "r", "zr"])
        sl, sr = STICKS.get(pack, ("stick", "stick"))
        dpad = g(pack, "dpad", dp)
        stick, stick_r = g(pack, sl, st), g(pack, sr, st)
        fx, fy, fa_, fb = (g(pack, "x", fa), g(pack, "y", fa),
                           g(pack, "a", fa), g(pack, "b", fa))

    # One thick ring per face button, not one around the cluster: the reading
    # is about each label individually.
    col = RING_OK if wys else RING_BAD
    pad_ring = max(4, int(6 * scale))
    d = fa + pad_ring * 2 + int(6 * scale)

    def cell(inner):
        if wys is None:
            return inner
        return (f'<span style="display:inline-flex;align-items:center;'
                f'justify-content:center;width:{d}px;height:{d}px;box-sizing:border-box;'
                f'border:{pad_ring}px solid {col};border-radius:50%">{inner}</span>')

    fx, fy, fa_, fb = cell(fx), cell(fy), cell(fa_), cell(fb)
    off = (d - fa) // 2 if wys is not None else 0
    fw, fh = int(fa*3.3) + 2*off, int(fa*2.7) + 2*off
    return f'''
      <div class="row" style="gap:{int(16*scale)}px;justify-content:center;padding-bottom:{int(12*scale)}px">{strip}</div>
      <div class="row" style="justify-content:space-between;padding:0 {int(18*scale)}px">
        <span>{dpad}</span>
        <span class="row" style="gap:{int(20*scale)}px">{stick}{stick_r}</span>
        <span style="position:relative;display:block;width:{fw}px;height:{fh}px">
          <span style="position:absolute;left:{int(fa*1.15)}px;top:0">{fx}</span>
          <span style="position:absolute;left:0;top:{int(fa*0.9)}px">{fy}</span>
          <span style="position:absolute;left:{int(fa*2.3)}px;top:{int(fa*0.9)}px">{fa_}</span>
          <span style="position:absolute;left:{int(fa*1.15)}px;top:{int(fa*1.75)}px">{fb}</span>
        </span>
      </div>'''


def bay(pack, slot, name, w, h, scale=1.0, wys=None):
    col = PC[slot]
    return f'''
    <div style="width:{w}px;height:{h}px;box-sizing:border-box;
                background:color-mix(in srgb, {col} 10%, {BG});
                border:3px solid {col};border-radius:6px;
                padding:{int(12*scale)}px {int(10*scale)}px {int(8*scale)}px;
                display:flex;flex-direction:column;justify-content:space-between">
      <div style="color:{col};font-weight:700;font-size:{int(19*scale)}px;line-height:1">P{slot}</div>
      <div>{pad(pack, scale, wys)}</div>
      <div style="text-align:center;color:{FG};font-size:{int(14*scale)}px">{name}</div>
    </div>'''


def legend(pack, width, scale=1.0):
    h = int(34*scale)
    fs = int(14.5*scale)
    if pack == "drawn":
        plus, minus = drawn_circ("+", h, fs-1), drawn_circ("–", h, fs-1)
        l3 = drawn_circ("L3", int(h*1.25), fs-1)
        r3 = drawn_circ("R3", int(h*1.25), fs-1)
        lb, rb = drawn_pill("L", int(h*1.5), h, h//2, fs-1), drawn_pill("R", int(h*1.5), h, h//2, fs-1)
    else:
        plus, minus = g(pack, "plus", h), g(pack, "minus", h)
        sl, sr = STICKS.get(pack, ("stick", "stick"))
        l3, r3 = g(pack, sl, h), g(pack, sr, h)
        lb, rb = g(pack, "l", h), g(pack, "r", h)
    p1 = PC[1]
    e = f'font-size:{fs}px;color:{DIM}'
    return f'''
    <div class="row" style="width:{width}px;gap:{int(26*scale)}px;padding-top:{int(14*scale)}px">
      <span class="row" style="gap:{int(7*scale)}px;{e}">{plus}
        <b style="color:{p1}">P1</b><span style="color:{FG};font-weight:600">hold to start</span></span>
      <span class="row" style="gap:{int(7*scale)}px;{e}">{l3}
        <span style="color:{DIM};font-weight:600">+</span>{r3}<span>claim P1</span></span>
      <span class="row" style="gap:{int(7*scale)}px;{e}">{lb}{rb}<span>swap ABXY</span></span>
      <span class="row" style="gap:{int(7*scale)}px;margin-left:auto;{e}">{minus}
        <b style="color:{p1}">P1</b><span>hold to quit</span></span>
    </div>'''


def wrap(inner, w, h, pad_px=24):
    return (HEAD + HELMET +
            f'<div style="width:{w}px;height:{h}px;box-sizing:border-box;background:{BG};'
            f'padding:{pad_px}px;filter: {{{{shade}}}}">' + inner + '</div>\n' + TAIL)


# ---- one comparison artboard per candidate -------------------------------
CARD_W, CARD_H = 760, 500
for i, (pk, (name, detail, _)) in enumerate(PACKS.items(), start=1):
    slot = (i - 1) % 4 + 1
    inner = f'''
      <div style="color:{FG};font-size:17px;font-weight:600;padding-bottom:2px">{name}</div>
      <div style="color:{DIM};font-size:12.5px;padding-bottom:14px">{detail}</div>
      {bay(pk, slot, "Xbox Series X|S Controller", CARD_W - 48, 288)}
      {legend(pk, CARD_W - 48)}'''
    fn = {"k": "KenneyDefault", "ko": "KenneyOutline", "em": "EssentialMono",
          "ed": "EssentialDuo", "x": "Xelu100", "x2": "Xelu256",
          "mix": "Mix"}[pk]
    open(f"{fn}.dc.html", "w").write(wrap(inner, CARD_W, CARD_H))

inner = f'''
  <div style="color:{FG};font-size:17px;font-weight:600;padding-bottom:2px">Drawn primitives</div>
  <div style="color:{DIM};font-size:12.5px;padding-bottom:14px">What Preflight draws today</div>
  {bay("drawn", 1, "Xbox Series X|S Controller", CARD_W - 48, 288)}
  {legend("drawn", CARD_W - 48)}'''
open("Current.dc.html", "w").write(wrap(inner, CARD_W, CARD_H))

# ---- Main: the full check screen in the leading candidate ----------------
LEAD = "ko"
bays = "".join(bay(LEAD, s, n, 560, 262, 0.95, w) for s, n, w in
               [(1, "8BitDo SF30 Pro", True), (2, "Xbox Series X|S Controller", True),
                (3, "Stadia Controller", True), (4, "Steam pad f679", False)])
main_inner = f'''
  <div class="row" style="gap:14px;align-items:flex-start">
    <div style="width:34px;height:26px;background:
      repeating-linear-gradient(115deg,#E8BA52 0 8px,#2C2F3A 8px 16px);border-radius:2px"></div>
    <div>
      <div style="color:{FG};font-size:34px;font-weight:700;line-height:1">Controller check</div>
      <div style="color:{DIM};font-size:15px;padding-top:4px">Controllers must be paired in your OS first
        — test your inputs before the game starts</div>
    </div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;padding-top:20px">{bays}</div>
  {legend(LEAD, 1184)}'''
open("Main.dc.html", "w").write(wrap(main_inner, 1280, 830, 48))

# ---- canvas -------------------------------------------------------------
order = ["KenneyOutline", "Current", "Xelu256", "Mix",
         "EssentialMono", "EssentialDuo", "KenneyDefault", "Xelu100"]
arts = [{"file": "Main.dc.html", "x": 0, "y": 0, "w": 1280, "h": 830}]
for n, fn in enumerate(order):
    arts.append({"file": f"{fn}.dc.html",
                 "x": (n % 3) * (CARD_W + 90),
                 "y": 970 + (n // 3) * (CARD_H + 140),
                 "w": CARD_W, "h": CARD_H})
for a in arts[1:]:
    a["page"] = "page-1" if a["file"] in ("KenneyOutline.dc.html", "Current.dc.html") else "page-2"
canvas = {
    "pages": [{"id": "page-1", "name": "Chosen"},
              {"id": "page-2", "name": "Also considered"}],
    "artboards": arts,
    "annotations": [
        {"id": "lighting", "x": 0, "y": -170, "w": 620, "page": "page-1",
         "text": "CHOSEN: Kenney outline for the buttons, per-direction outline d-pads, and switch_stick_top_l/r for the sticks (which will move with the real axis values). The ring around ABXY is bright green when that pad's printed labels tell the truth and amber when ABXY is inverted \u2014 P4 here shows amber. It follows the OUTCOME, so a Nintendo-layout pad like the SF30 Pro goes green on the mirrored setting, not the default one; layout is read from the kernel device name RealWatcher recovers, since under Steam Input every pad claims to be a Valve pad.\n\nKenney outline, throughout. Its art is pure #FFFFFF, and SDL's colour modulation multiplies — so white takes any colour exactly. These can light in a player's colour and dim when a pad sleeps, which the shaded sets could not. Preflight lights a button on press and dims a sleeping bay by "
                 "recolouring the shape it drew. SDL's colour modulation can only "
                 "DARKEN an image — so a flat single-colour set (Essential Mono, "
                 "Xelu) can still be tinted per player, while a set with baked-in "
                 "fills (Kenney, Essential Duo) would look identical pressed or not.\n\n"
                 "Flip the 'dim' tweak on any artboard to see the asleep state."},
        {"id": "licences", "x": 700, "y": -170, "w": 560, "page": "page-1",
         "text": "Kenney — CC0, SVG vector.\n"
                 "Xelu — CC0, 100px PNG (256px export also in the pack).\n"
                 "Essential — CC BY 4.0, SVG vector: shipping it means crediting "
                 "Gioele Casazza in the README and keeping the licence file."},
    ],
    "launch": {"view": "canvas", "page": "page-1"},
}
json.dump(canvas, open("canvas.json", "w"), indent=2)
print("wrote", len(order) + 1, "artboards + canvas.json")
