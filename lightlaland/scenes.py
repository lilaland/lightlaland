"""Scenes: a scene is data (a "spec"), and one renderer turns any spec into what each light
does this frame, from musical time (or plain seconds, for ambient scenes).

A spec has a palette of two colour ranges (primary and secondary: circles on the colour disc,
so colours are drawn from anywhere inside them rather than a few exact ones), a movement,
three light layers (the beams, the LED strip with its two rows, and the white strobes) and
effects (lightning, random strobe bursts, fills). SCHEMA lists every field with its default
and limits; the scene maker in the web UI is built from it, and normalize() makes any incoming
spec safe to render. The built-in scenes are PRESETS. Scenes saved from the maker live in the
config ("scenes") and override the preset with the same name. Each scene belongs to a set
(everyday, Halloween, the Halloween window); auto mode and the group pads only pick from the
set chosen on the Show or Perform screen. Tags (beams, washers, strobes, disco ball) sort them.

The bars are mounted horizontally, so movement is tilt: the head turns about the bar's long
axis (see geometry.py). Beam safety is enforced by the engine, not here: a scene can ask
for anything and the beams still go dark where they'd point below level.

Fixture 0 is A (left), 1 is B (right). Index 0 of beams/strip/whites is the first end of the bar.
"""
import colorsys
import copy
import functools
import math
import random
import re

from . import geometry
from .fixture import N_BEAMS, N_STRIP, N_WHITE, Frame
from .geometry import DEG_PER_STEP

GROUPS = ("groove", "calm", "peak", "ambient")
SETS = [["", "Everyday"], ["halloween", "Halloween"], ["window", "Window"]]   # window: bars on the windowsill
SET_NAMES = {k for k, _ in SETS}


# --- small colour/shape helpers ------------------------------------------------------------
def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def rgb_hex(c):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v * 255)))) for v in c)


def rgbw(rgb, level=1.0):
    """RGB -> RGBW for the beam LEDs: whites and pastels also drive the W die."""
    r, g, b = rgb
    return (r * level, g * level, b * level, min(r, g, b) * level)


def scale3(rgb, level):
    return (rgb[0] * level, rgb[1] * level, rgb[2] * level)


def mix(c1, c2, u):
    return tuple(a + (b - a) * u for a, b in zip(c1, c2))


def tri(x):
    """0 -> 1 -> 0 over one unit."""
    x %= 1.0
    return 1.0 - abs(2.0 * x - 1.0)


def ease(x):
    x = min(1.0, max(0.0, x))
    return 0.5 - 0.5 * math.cos(math.pi * x)


def pingpong(x):
    x %= 2.0
    return x if x < 1.0 else 2.0 - x


def flash(phase, decay=6.0):
    return math.exp(-decay * phase)


def h01(*xs):
    """A repeatable pseudo-random number in 0..1 from some numbers (no state, so every frame
    of every scene agrees about where the raindrops and lightning are)."""
    v = 0.0
    for k, x in enumerate(xs):   # the weights must differ mod 2 pi, or (a, b) and (a + 1, b - 1) look alike
        v += x * (12.9898 + 78.233 * k)
    s = math.sin(v + 78.233) * 43758.5453
    return s - math.floor(s)


def noise1(x, seed):
    """Smooth random wandering in 0..1: random values at whole x, eased between."""
    k = math.floor(x)
    a, b = h01(seed, k), h01(seed, k + 1)
    return a + (b - a) * ease(x - k)


@functools.lru_cache(maxsize=64)
def sparkle_set(step, i, n, count):
    """Which segments sparkle during one step (worked out once, not once per segment)."""
    return frozenset(random.Random(step * 7919 + i * 104729 + n).sample(range(n), min(count, n)))


@functools.lru_cache(maxsize=64)
def scatter_round(lseed, rnd, n):
    """One round of a scatter: every segment once, in random order, never starting on the one
    the last round ended on, so no segment lights twice in a row. (Swapping the first two
    doesn't move the last, so the previous round's last needs no fixing of its own.)"""
    def shuffled(r):
        return random.Random(lseed * 7919 + r * 104729 + n).sample(range(n), n)
    order = shuffled(rnd)
    if n > 2 and order[0] == shuffled(rnd - 1)[-1]:
        order[0], order[1] = order[1], order[0]
    return tuple(order)


# --- colour ranges -----------------------------------------------------------------------------
# A range is a circle on the colour disc: hue h (degrees round it), saturation s (white in the
# middle, 1 at the edge) and radius r. Lights look best at full brightness, so there's no value.
def crange(h, s=1.0, r=0.12):
    h, s, r = float(h), float(s), float(r)
    if not all(map(math.isfinite, (h, s, r))):
        raise ValueError("not a number")
    return {"h": round(h % 360.0, 1), "s": round(min(1.0, max(0.0, s)), 3), "r": round(min(0.6, max(0.0, r)), 3)}


def to_range(raw, fallback=None):
    """A colour range from the API or a saved spec; an #rrggbb colour (older palettes) becomes
    a small range around it. Anything else is an error, or the fallback if there is one."""
    try:
        if isinstance(raw, str) and HEX_RE.fullmatch(raw):
            h, s, _ = colorsys.rgb_to_hsv(*hex_rgb(raw))
            return crange(h * 360.0, s, 0.1)
        if isinstance(raw, dict):
            return crange(raw["h"], raw["s"], raw["r"])
    except (KeyError, TypeError, ValueError):
        pass
    if fallback is None:
        raise ValueError("a colour range is {h: 0-360, s: 0-1, r: 0-0.6}")
    return dict(fallback)


def range_rgb(rng, u, v):
    """A colour inside a range: u goes round the circle, v from its middle (0) out to its edge (1)."""
    a, d, h = 2.0 * math.pi * u, rng["r"] * math.sqrt(min(1.0, max(0.0, v))), math.radians(rng["h"])
    x, y = rng["s"] * math.cos(h) + d * math.cos(a), rng["s"] * math.sin(h) + d * math.sin(a)
    return colorsys.hsv_to_rgb((math.atan2(y, x) / (2.0 * math.pi)) % 1.0, min(1.0, math.hypot(x, y)), 1.0)


def pal(primary, secondary):
    return {"primary": primary, "secondary": secondary}


# --- the spec ------------------------------------------------------------------------------
TARGETS = [["ceiling", "Ceiling, mid-room"], ["up", "Straight up"], ["out", "Level, across the room"],
           ["corner", "Far wall's top edge"], ["balls", "Ball cluster"], ["crowd", "Dance floor"]]
FACES = [["beams", "The beams"], ["leds", "The LED side"],
         ["auto", "Beams if it's safe, else the LED side"]]
PULSES = [["none", "Steady"], ["beat", "Every beat"], ["8th", "8ths"], ["16th", "16ths"],
          ["offbeat", "Off-beats"], ["2beat", "Every other beat"], ["one", "The 1 of each bar"],
          ["breathe", "Slow breathe"], ["heartbeat", "Heartbeat (lub-dub every 2 beats)"]]
PATTERNS = [["off", "Off"], ["solid", "All on"], ["chase", "Chase"], ["bounce", "Bounce"],
            ["midout", "Rings from the middle"], ["fill", "Fill from one end"], ["wave", "Wave"],
            ["shimmer", "Shimmer"], ["sparkle", "Sparkle"], ["twinkle", "Twinkle"], ["rain", "Rain drops"],
            ["flicker", "Fire flicker"], ["alternate", "Alternate segments"],
            ["wander", "Ghost (a glow drifting about)"], ["eyes", "Eyes (pairs that open and blink)"],
            ["glitch", "Glitch (a dying bulb)"], ["step", "One at a time, down the line"],
            ["pingpong", "One at a time, back and forth"], ["scatter", "One at a time, at random"],
            ["comet", "Comet (a head with a fading tail)"]]
ONE_AT_A_TIME = ("step", "pingpong", "scatter")
COLOURS = [["own", "A from the primary range, B from the secondary"], ["a", "Primary range"],
           ["b", "Secondary range"], ["ab", "Primary to secondary along the bar"],
           ["spread", "Both ranges along the bar"], ["scroll", "Both ranges, scrolling along"],
           ["random", "Random colours from both ranges"], ["step", "A new colour every step"],
           ["rainbow", "Rainbow"], ["white", "White"]]
B_MODES = [["same", "Same as A"], ["mirror", "Mirrored"], ["offset", "Half a cycle later"]]
ROWS = [["same", "Same as the first"], ["split", "From the other range"], ["mirror", "Runs the other way"],
        ["offset", "Half a cycle later"], ["alt", "Takes turns, beat by beat"]]
TAGS = [["beams", "Beams"], ["washers", "Washers"], ["strobes", "Strobes"], ["discoball", "Disco ball"]]


def _choice(key, default, options, label, hint=""):
    return {"key": key, "kind": "choice", "default": default, "options": options, "label": label, "hint": hint}


def _num(key, default, lo, hi, step, label, hint=""):
    return {"key": key, "kind": "num", "default": default, "min": lo, "max": hi, "step": step,
            "label": label, "hint": hint}


def _bool(key, default, label, hint=""):
    return {"key": key, "kind": "bool", "default": default, "label": label, "hint": hint}


def _layer(colour=True, default_pattern="off", rows=False):
    fields = [_choice("pattern", default_pattern, PATTERNS, "Pattern")]
    if colour:
        fields.append(_choice("color", "own", COLOURS, "Colour"))
    if rows:
        fields.append(_choice("rows", "same", ROWS, "Second row", "The LED side has two rows of colour along the bar"))
    fields += [
        _num("level", 1.0, 0, 1, 0.05, "Brightness"),
        _num("floor", 0.0, 0, 1, 0.05, "Never below", "Brightness of the parts the pattern leaves dark"),
        _choice("pulse", "none", PULSES, "Pulse"),
        _num("depth", 1.0, 0, 1, 0.05, "Pulse depth", "0 = steady, 1 = full flashes"),
        _num("decay", 6, 1, 16, 0.5, "Flash length", "Higher = shorter flashes"),
        _num("rate", 2, 0.25, 32, 0.25, "Cycle", "Beats (or seconds on the seconds clock) for one chase, wave, drop..."),
        _num("width", 0.25, 0.05, 1, 0.01, "Band width", "Share of the bar a band, wave or ghost covers"),
        _num("count", 2, 1, 8, 1, "How many at once", "Sparkles, or pairs of eyes"),
        _num("density", 0.3, 0.02, 1, 0.02, "How many",
             "Rain and twinkle: share of segments at a time. Glitch: how often it cuts out"),
        _bool("reverse", False, "Run from the other end"),
        _bool("flip_bars", False, "Reverse every other bar"),
        _choice("b", "same", B_MODES, "Light B"),
    ]
    return fields


SCHEMA = {
    "general": [
        {"key": "label", "kind": "text", "default": "New scene", "label": "Name"},
        _choice("group", "groove", [["groove", "Groove"], ["calm", "Break (calm)"], ["peak", "Drop (peak)"],
                                    ["ambient", "Ambient (no music needed)"]], "Used for"),
        _choice("set", "", SETS, "Set", "Auto mode and the group pads only pick from the set chosen on Show or Perform"),
        {"key": "tags", "kind": "tags", "default": None, "options": TAGS, "label": "Tags",
         "hint": "For sorting the scene lists. Worked out from the scene until you change them"},
        {"key": "blurb", "kind": "text", "default": "", "label": "Description"},
        _choice("clock", "beats", [["beats", "The music's beats"], ["seconds", "Seconds (ambient)"]], "Runs on"),
        _bool("own_colors", False, "Keeps its own colours", "The live palette doesn't change this scene"),
        _num("swap", 0, 0, 16, 1, "Swap the two ranges every (bars)", "0 = never"),
    ],
    "move": [
        _choice("mode", "aim", [["aim", "Hold on a target"], ["sweep", "Tilt back and forth"], ["spin", "Spin"],
                                ["hop", "Hop across the balls"]], "Movement",
                "Spinning always turns the beams off: the software can't tell where they point"),
        _choice("target", "ceiling", TARGETS, "Target", "Where it aims, or the middle of a tilt"),
        _choice("face", "beams", FACES, "Which side points at it"),
        _num("lift", 0, -30, 60, 1, "Aim above it by (degrees)", "Negative aims below; beams never go below a light's limit"),
        _num("speed", 1.0, 0.1, 1, 0.05, "Motor speed"),
        _num("jitter", 0, 0, 8, 1, "Jitter (steps)", "Shake around the target; 1 step = 2.1 degrees"),
        _choice("jitter_rate", "16th", [["16th", "16ths"], ["8th", "8ths"], ["beat", "Beats"]], "Jitter rate"),
        _num("sweep", 0, 0, 90, 5, "Tilt width (degrees each side)", "0 = the width set in Setup"),
        _num("period", 2, 0.5, 16, 0.5, "Tilt across in (bars)", "On the seconds clock a bar is 4 seconds"),
        _num("spin", 0.35, 0.05, 1, 0.05, "Spin speed"),
        _num("hops", 3, 2, 6, 1, "Spots across the cluster"),
        _num("hop_every", 4, 0.5, 16, 0.5, "Hop every (beats)"),
        _num("converge", 0, 0, 40, 1, "Drop in from (degrees)", "Every other 2 bars, both curtains start this far above the target and drop onto it"),
        _choice("pair", "same", [["same", "Both the same"], ["mirror", "Mirrored"], ["trade", "Take turns"]],
                "The two lights"),
        _num("trade_bars", 2, 1, 16, 1, "Swap turns every (bars)"),
        _choice("rest", "balls", TARGETS, "Resting light aims at", "Take turns: the other light jitters here"),
    ],
    "beams": _layer(default_pattern="solid"),
    "strip": _layer(rows=True),
    "white": _layer(colour=False),
    "fx": [
        _num("lightning", 0, 0, 20, 0.5, "Lightning (strikes a minute)", "0 = none. Strikes flash the whites"),
        _num("lightning_level", 1.0, 0.1, 1, 0.05, "Lightning brightness"),
        _bool("lightning_beams", False, "Lightning also flashes the beams"),
        _num("strobe", 0, 0, 30, 0.5, "Strobe bursts (a minute)",
             "0 = none. Now and then the whites strobe for a moment. Strobe freq on Show or Perform scales these and lightning"),
        _num("strobe_len", 1, 0.25, 4, 0.25, "Burst length", "Beats (or seconds on the seconds clock)"),
        _bool("strobe_beams", False, "Bursts also strobe the beams"),
        _num("fill", 0, 0, 16, 1, "Fill every (bars)",
             "0 = never. At the end of every N bars the beams race down the line into the next bar"),
        _num("fill_len", 1, 0.5, 4, 0.5, "Fill length (beats)"),
    ],
}
DEFAULT_PALETTE = pal(crange(345, 0.9, 0.12), crange(190, 1.0, 0.12))
HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")
LEGACY_TARGETS = {"back": ("crowd", "leds"),   # specs saved for vertically mounted bars
                  "cross": ("balls", None)}       # the curtains can only meet in the ball corner


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return s[:32] or "scene"


def _norm_fields(fields, given):
    out = {}
    given = given if isinstance(given, dict) else {}
    for f in fields:
        v = given.get(f["key"], f["default"])
        kind = f["kind"]
        try:
            if kind == "choice":
                v = v if v in [o[0] for o in f["options"]] else f["default"]
            elif kind == "num":
                v = min(f["max"], max(f["min"], float(v)))
                if float(f["step"]).is_integer():
                    v = int(round(v))
            elif kind == "bool":
                v = bool(v)
            elif kind == "tags":   # None = work them out from the scene
                v = [k for k, _ in f["options"] if k in v] if isinstance(v, list) else None
            else:
                v = str(v)[:80]
        except (TypeError, ValueError):
            v = f["default"]
        out[f["key"]] = v
    return out


def _palette(raw):
    """Two colour ranges. Older specs had five #rrggbb colours (or {"a", "b", "c"}); their first
    two become the middles of the ranges."""
    if isinstance(raw, list):
        raw = {"primary": raw[0] if raw else None, "secondary": raw[1] if len(raw) > 1 else None}
    elif isinstance(raw, dict) and "primary" not in raw:
        raw = {"primary": raw.get("a"), "secondary": raw.get("b")}
    raw = raw if isinstance(raw, dict) else {}
    return {k: to_range(raw.get(k), DEFAULT_PALETTE[k]) for k in ("primary", "secondary")}


def auto_tags(sp):
    """Tags worked out from what a normalized spec does."""
    def on(part, least):
        return sp[part]["pattern"] != "off" and sp[part]["level"] >= least
    m, fx, tags = sp["move"], sp["fx"], []
    if on("beams", 0.25):
        tags.append("beams")
    if on("strip", 0.25):
        tags.append("washers")
    if on("white", 0.4) or fx["strobe"] >= 3 or fx["lightning"] >= 3:
        tags.append("strobes")
    aims = ({m["target"]} if m["mode"] in ("aim", "sweep") else set()) | ({m["rest"]} if m["pair"] == "trade" else set())
    if m["mode"] == "hop" or (m["face"] != "leds" and "balls" in aims):
        tags.append("discoball")
    return tags


def normalize(spec):
    """Any dict -> a complete, safe spec (unknown keys dropped, numbers clamped)."""
    spec = spec if isinstance(spec, dict) else {}
    out = _norm_fields(SCHEMA["general"], spec)
    out["label"] = out["label"].strip() or "Untitled"
    out["name"] = slug(spec.get("name") or out["label"])
    out["palette"] = _palette(spec.get("palette"))
    move = dict(spec.get("move") or {}) if isinstance(spec.get("move"), dict) else {}
    for key, face_key in (("target", "face"), ("rest", None)):
        if move.get(key) in LEGACY_TARGETS:
            move[key], face = LEGACY_TARGETS[move[key]]
            if face_key and face:
                move[face_key] = face
    out["move"] = _norm_fields(SCHEMA["move"], move)
    for part in ("beams", "strip", "white", "fx"):
        given = spec.get(part)
        if isinstance(given, dict) and given.get("color") == "c":   # colour 3 is gone: the secondary range
            given = dict(given, color="b")
        out[part] = _norm_fields(SCHEMA[part], given)
    if out["tags"] is None:
        out["tags"] = auto_tags(out)
    return out


# --- aiming -------------------------------------------------------------------------------------
def target_elevation(fx, room, points, name):
    """Beam elevation (degrees) that tilts a fixture's curtain onto a target: a saved aim from
    the Mount tab if there is one, else worked out from the room."""
    if name in fx["targets"]:
        return geometry.elevation(fx, fx["targets"][name])
    if name == "balls" and fx.get("ball_span"):
        return geometry.elevation(fx, sum(fx["ball_span"]) / 2)
    p = points.get(name)
    if p is not None:
        return geometry.point_elevation(fx, p["x"], p["y"], p.get("z", fx["z"]))[0]
    if name == "crowd":   # the middle of the dance floor, about chest height
        return geometry.point_elevation(fx, room["w"] / 2, room["d"] / 2, 1.2)[0]
    return geometry.named_elevation(fx, room, name)


# --- context: time, colours and aiming helpers for one frame ----------------------------------
class Ctx:
    def __init__(self, cfg, info, colours, prev, speed=1.0, secs=0.0, windows=None):
        self.fixtures = cfg["fixtures"]
        self.n = len(self.fixtures)
        self.room = cfg["room"]
        self.points = {p["id"]: p for p in cfg["points"]}
        self.sweep = float(cfg["show"]["sweep_width"])
        self.flash = float(cfg["show"]["strobe_freq"])   # scales strobe bursts and lightning (0 = none)
        self.ranges = [geometry.safe_range(fx, cfg["safety"]) for fx in self.fixtures]   # (low, high)
        self.windows = windows or [[] for _ in range(self.n)]   # copies of the safe arc per fixture
        self.info = info
        self.colours = colours          # [primary range, secondary range]
        self.prev = prev
        self.speed = speed
        rb = (info["beat"] - info["anchor"]) * speed  # beats since the phrase started
        self.rb = rb
        self.secs = secs * speed
        self.ph = rb % 1.0                    # phase within the beat
        self.bib = int(math.floor(rb)) % 4    # beat in bar
        self.bar = int(math.floor(rb / 4.0))
        self.bar_ph = (rb % 4.0) / 4.0        # phase within the bar

    def pick(self, which, u, v):
        """A colour from the primary (0) or secondary (1) range; see range_rgb."""
        return range_rgb(self.colours[which % 2], u, v)

    def here(self, i):
        return self.prev[i].pos if self.prev else self.fixtures[i]["up"]

    def target_elevation(self, i, name):
        """Beam elevation (degrees) that tilts fixture i's curtain onto a target."""
        return target_elevation(self.fixtures[i], self.room, self.points, name)

    def facing(self, i, name, face):
        """(beam elevation, side) that points `face` (beams / leds / auto) at a target."""
        e = self.target_elevation(i, name)
        if face == "auto":
            low, high = self.ranges[i]
            face = "beams" if geometry.in_arc(e, low, high) else "leds"
        return (geometry.wrap180(e + 180.0), "leds") if face == "leds" else (e, "beams")

    def place(self, i, elev, face="beams"):
        """CH1 for a beam elevation. When the beams are what's being aimed, stay inside the
        safe range (a scissor or jitter reaching below level stops at level instead)."""
        if face == "beams":   # a little inside the edges, so DMX rounding can't step over them
            low, high = self.ranges[i]
            elev = geometry.clamp_to_arc(elev, low + 0.6, high - 0.6)
        return geometry.place(self.fixtures[i], elev, self.windows[i], self.here(i))

    def cluster_span(self, i):
        """The lowest and highest beam elevations that still land on the ball cluster: the
        range measured on the Mount tab if there is one, else worked out from the plan."""
        p, fx = self.points.get("balls"), self.fixtures[i]
        if fx.get("ball_span"):
            es = sorted(geometry.elevation(fx, c) for c in fx["ball_span"])
            return es[0], es[1]
        if p is None:
            e = self.target_elevation(i, "ceiling")
            return e, e
        es = [geometry.point_elevation(fx, p["x"] + dx, p["y"] + dy, p.get("z", fx["z"]))[0]
              for dx in (-p["w"] / 2, p["w"] / 2) for dy in (-p["h"] / 2, p["h"] / 2)]
        lo, hi = min(es), max(es)
        pad = (hi - lo) * 0.1   # stay inside the edges
        return lo + pad, hi - pad


# --- rendering a spec --------------------------------------------------------------------------
PULSE_PERIOD = {"beat": 1.0, "8th": 0.5, "16th": 0.25, "offbeat": 1.0, "2beat": 2.0, "one": 4.0, "heartbeat": 2.0}
JITTER_DIV = {"16th": 4, "8th": 2, "beat": 1}
STORM_WHITE = (0.8, 0.86, 1.0)


def envelope(layer, t, is_b):
    kind = layer["pulse"]
    if kind == "none":
        return 1.0
    period = PULSE_PERIOD.get(kind, 8.0)
    shift = period / 2 if is_b and layer["b"] == "offset" else 0.0
    if kind == "breathe":
        return 0.5 - 0.5 * math.cos(2 * math.pi * (t + shift) / 8.0)
    if kind == "heartbeat":   # lub, a softer dub, then a rest
        ph = (t + shift) % period
        return max(flash(ph, layer["decay"]), 0.7 * flash(ph - 0.35, layer["decay"]) if ph >= 0.35 else 0.0)
    start = 0.5 if kind == "offbeat" else 0.0
    # Sub-beat pulses decay over their own length; slower ones over beats.
    ph = ((t - start + shift) % period) / min(period, 1.0)
    return flash(ph, layer["decay"])


def drop(seed, t, rate, density, decay, smooth):
    """One segment's raindrops/twinkles: random starts, then a fade (rain) or a swell (twinkle)."""
    v = (t + h01(seed) * rate) / rate
    slot = math.floor(v)
    if h01(seed, slot) >= density:
        return 0.0
    frac = v - slot
    return math.sin(math.pi * frac) ** 2 if smooth else flash(frac, decay)


def steps(layer, n):
    """Steps in one cycle of a one-at-a-time pattern (there and back doesn't repeat the ends)."""
    return 2 * n - 2 if layer["pattern"] == "pingpong" and n > 2 else n


def one_lit(layer, cyc, n, lseed):
    """The segment a one-at-a-time pattern lights at this point (cyc counts whole cycles)."""
    k = math.floor(cyc * steps(layer, n))
    if layer["pattern"] == "step":
        return k % n
    if layer["pattern"] == "pingpong":
        k %= steps(layer, n)
        return k if k < n else 2 * n - 2 - k
    return scatter_round(lseed, k // n, n)[k % n]   # scatter: somewhere new each step


def pattern_value(layer, x, s, u, t, c, i, n, seed, cyc):
    pat, w, rate = layer["pattern"], layer["width"], layer["rate"]
    sx = int(round(x * (n - 1)))   # the segment, counted the way the pattern runs
    if pat == "solid":
        return 1.0
    if pat in ONE_AT_A_TIME:
        return 1.0 if sx == one_lit(layer, cyc, n, seed - s) else 0.0
    if pat == "comet":   # a bright head with a tail fading out behind it
        return max(0.0, 1.0 - ((u - x) % 1.0) / w)
    if pat == "chase":
        d = abs(x - u)
        return max(0.0, 1 - min(d, 1 - d) / w)
    if pat == "bounce":
        return max(0.0, 1 - abs(x - tri(u)) / w)
    if pat == "midout":
        d = ease(min(1.0, u * 1.2))
        if layer["flip_bars"] and c.bar % 2:
            d = 1 - d
        return max(0.0, 1 - abs(abs(x - 0.5) * 2 - d) / w)
    if pat == "fill":
        return 1.0 if x > 1 - u else 0.0
    if pat == "wave":
        return 0.5 + 0.5 * math.cos(2 * math.pi * (x / max(w, 0.05) - u))
    if pat == "shimmer":   # every segment drifts at its own pace
        period = rate * (0.6 + 0.8 * h01(seed, 1))
        return (0.5 + 0.5 * math.sin(2 * math.pi * (t / period + h01(seed, 2)))) ** 1.6
    if pat == "sparkle":
        return 1.0 if s in sparkle_set(int(math.floor(t / rate)), i, n, layer["count"]) else 0.0
    if pat == "rain":
        return drop(seed, t, rate, layer["density"], layer["decay"], False)
    if pat == "twinkle":
        return drop(seed, t, rate, layer["density"], layer["decay"], True)
    if pat == "flicker":
        k = 2 * math.pi * t / max(rate, 0.25)
        v = (0.55 + 0.25 * math.sin(k * 1.3 + s * 1.7 + i * 3.1) + 0.2 * math.sin(k * 3.7 + s * 4.3)
             + 0.12 * math.sin(k * 7.9 + s * 2.1 + i))
        return max(0.0, min(1.0, v))
    if pat == "alternate":
        k = int(math.floor(t / rate)) + (1 if i % 2 and layer["b"] == "offset" else 0)
        return 1.0 if (s + k) % 2 == 0 else 0.0
    if pat == "wander":   # a ghost: a soft glow drifting to a new spot every cycle, fading in and out
        own = 7.0 if i % 2 and layer["b"] == "offset" else 0.0   # B's own path, else it follows A
        centre = min(1.0, max(0.0, 0.5 + 1.6 * (noise1(t / rate + own, 11) - 0.5)))
        return max(0.0, 1 - abs(x - centre) / w) * (0.3 + 0.7 * noise1(t / (rate * 1.7) + own, 13))
    if pat == "eyes":   # pairs of eyes open somewhere, blink once, close, and open somewhere else
        lseed, v = seed - s, 0.0
        for k in range(layer["count"]):
            cyc = t / rate + h01(lseed, k, 1)
            slot, frac = math.floor(cyc), cyc % 1.0
            left = int(h01(lseed, k, slot, 2) * max(1, n - 2))
            if sx != left and sx != left + 2:
                continue
            blink = abs(frac - (0.3 + 0.4 * h01(lseed, k, slot, 3))) < 0.035
            v = max(v, 0.0 if blink else min(1.0, frac / 0.12, (1 - frac) / 0.12))
        return v
    if pat == "glitch":   # a dying bulb: on, but the whole bar or single segments cut out and stutter
        k = t / rate * 8.0
        slot = math.floor(k)
        for sd, salt, chance in ((seed - s, 5, layer["density"]), (seed, 6, layer["density"] * 0.4)):
            if h01(sd, slot, salt) < chance:
                return 1.0 if (k * 3.0) % 1.0 < 0.5 * h01(sd, slot, salt + 2) else 0.08
        return 1.0
    return 0.0


def layer_colour(layer, x, u, t, c, i, seed, n, cyc, other=0):
    """One segment's colour: from the colour ranges (or a rainbow, or white). `other` swaps the
    primary and secondary ranges (the strip's second row, split)."""
    kind = layer.get("color", "white")
    if kind == "white":
        return (1.0, 1.0, 1.0)
    if kind == "rainbow":
        return colorsys.hsv_to_rgb((x * 0.6 + u) % 1.0, 1.0, 1.0)

    def pick(which, a, b):
        return c.pick(which ^ other, a, b)
    # Each segment has its own spot in a range and wanders slowly, so the colours stay alive.
    wu, wv = h01(seed, 3) + 0.25 * noise1(t / 8.0, seed), noise1(t / 6.0, seed + 0.5)
    if kind == "own":
        return pick(i % 2, wu, wv)
    if kind in ("a", "b"):
        return pick(kind == "b", wu, wv)
    if kind == "ab":
        return mix(pick(0, wu, wv), pick(1, wu, wv), x)
    if kind in ("spread", "scroll"):   # primary, secondary, primary... along the bar, round each range
        p = (x + (u if kind == "scroll" else 0.0)) % 1.0
        return mix(pick(0, p, 0.8), pick(1, p, 0.8), 0.5 - 0.5 * math.cos(2 * math.pi * p))
    if kind == "random":   # each segment a random colour from either range, changing at its own moments
        slot = math.floor(t / (layer["rate"] * 4) + h01(seed, 5))
        return pick(int(h01(seed, slot, 7) * 2), h01(seed, slot, 8), h01(seed, slot, 9))
    if kind == "step":   # a new colour every step of the pattern, the ranges taking turns
        k = math.floor(cyc * steps(layer, n))
        return pick(k % 2, h01(k, i, 21), h01(k, i, 22))
    return (1.0, 1.0, 1.0)


def render_layer(layer, c, i, n, t, salt, row2=""):
    """One light layer of fixture i -> list of (colour rgb, level) for its n segments. row2 says
    how the strip's second row differs: "split" (the other range), "mirror" or "offset"."""
    if layer["pattern"] == "off" or layer["level"] <= 0:
        return [((0.0, 0.0, 0.0), 0.0)] * n
    if row2 == "offset":
        t += layer["rate"] / 2
    is_b = i % 2 == 1
    cyc = t / layer["rate"] + (0.5 if is_b and layer["b"] == "offset" else 0.0)   # cycles so far
    u = cyc % 1.0
    env = envelope(layer, t, is_b)
    level = layer["level"] * ((1 - layer["depth"]) + layer["depth"] * env)
    out = []
    for s in range(n):
        x = s / (n - 1) if n > 1 else 0.5
        if layer["reverse"] != (row2 == "mirror"):
            x = 1 - x
        if is_b and layer["b"] == "mirror":
            x = 1 - x
        if layer["flip_bars"] and c.bar % 2 and layer["pattern"] != "midout":
            x = 1 - x
        seed = salt * 1000 + i * 100 + s
        p = pattern_value(layer, x, s, u, t, c, i, n, seed, cyc)
        p = layer["floor"] + (1 - layer["floor"]) * p
        out.append((layer_colour(layer, x, u, t, c, i, seed, n, cyc, row2 == "split"), level * p))
    return out


def lightning(fx, secs, i, freq=1.0):
    """Thunderstorm flashes: random strikes of a few quick flashes (always in seconds)."""
    rate = fx["lightning"] * freq
    if rate <= 0:
        return 0.0
    env, now = 0.0, math.floor(secs)
    for slot in (now, now - 1):   # a strike can run into the next second
        if h01(slot, 31) >= rate / 60.0:
            continue
        start = slot + h01(slot, 32) * 0.7 + (0.035 if i % 2 else 0.0)   # B a hair later
        for k, (off, amp) in enumerate(((0.0, 1.0), (0.11, 0.55), (0.27, 0.85), (0.45, 0.4))):
            if k and h01(slot, 40 + k) > 0.7:
                continue
            dt = secs - start - off
            if 0 <= dt < 0.6:
                env = max(env, amp * math.exp(-dt * 16))
    return env * fx["lightning_level"]


def strobe_burst(fx, c, t, clock):
    """Random strobe bursts: now and then a moment of hard white flashes, 16ths on the beat
    grid (eight a second on the seconds clock). fx["strobe"] is bursts a minute of real time,
    scaled by the show's strobe frequency. Returns (in a burst, flash on)."""
    rate = fx["strobe"] * c.flash
    if rate <= 0:
        return False, 0.0
    if clock == "seconds":
        unit, sub = 1.0 / c.speed, 8
    else:   # whole BPM, so a tempo wobbling by a hair can't switch a burst on and off
        unit, sub = 60.0 / (max(40, round(c.info["bpm"])) * c.speed), 4
    chance = rate * unit / 60.0   # that a burst starts on any one beat (or second)
    length, now = fx["strobe_len"], math.floor(t)
    for k in range(now, now - math.ceil(length) - 1, -1):   # bursts that started up to `length` ago
        if t - k < length and h01(k, 51) < chance:
            return True, (1.0 if ((t - k) * sub) % 1.0 < 0.4 else 0.0)
    return False, 0.0


def fill_at(fx, t):
    """How far into a fill we are (0..1), or None. A fill is the last beats of every N bars."""
    if not fx["fill"]:
        return None
    block = 4.0 * fx["fill"]
    into = t % block - (block - fx["fill_len"])
    return into / fx["fill_len"] if into >= 0 else None


def race(f, c, i, p):
    """A fill: one pass of the beams racing down the line one at a time, the ranges taking
    turns, with the LED side running alongside."""
    k = min(N_BEAMS - 1, int(p * N_BEAMS))
    col = c.pick(k + i, 0.3 + 0.4 * p, 0.6)
    f.beams = [rgbw(col) if g == k else (0.0, 0.0, 0.0, 0.0) for g in range(N_BEAMS)]
    ks = min(N_STRIP - 1, int(p * N_STRIP))
    f.strip, f.strip2 = [col if s == ks else (0.0, 0.0, 0.0) for s in range(N_STRIP)], None


def jitter_offset(m, sign, t):
    """Jitter around the target, in degrees."""
    j = m["jitter"]
    if not j:
        return 0.0
    return sign * (0, j, 0, -j)[int(t * JITTER_DIV[m["jitter_rate"]]) % 4] * DEG_PER_STEP


def render_move(m, c, i, t, bar):
    """Where fixture i points this frame (a Frame with pos/spin/speed)."""
    is_b = i % 2 == 1
    sign = -1 if is_b and m["pair"] == "mirror" else 1
    if m["pair"] == "trade" and (bar // m["trade_bars"]) % 2 != i % 2:
        # Taking turns: this light rests on its target, jittering, while the other moves.
        elev, face = c.facing(i, m["rest"], m["face"])
        return Frame(pos=c.place(i, elev + m["lift"] + jitter_offset(m, 1, t), face), speed=1.0)
    mode = m["mode"]
    if mode == "spin":
        return Frame(pos=c.here(i), spin=m["spin"] * sign, speed=1.0)
    if mode == "sweep":
        width = m["sweep"] or c.sweep
        x = ease(pingpong(t / (4.0 * m["period"])))
        elev, face = c.facing(i, m["target"], m["face"])
        elev += (-width + 2 * width * x) * sign
    elif mode == "hop":
        k = int(t // m["hop_every"]) % m["hops"]
        if is_b and m["pair"] == "mirror":
            k = m["hops"] - 1 - k
        lo, hi = c.cluster_span(i)
        elev, face = lo + (hi - lo) * k / (m["hops"] - 1), "beams"
        if m["face"] == "leds":
            elev, face = geometry.wrap180(elev + 180.0), "leds"
    else:
        elev, face = c.facing(i, m["target"], m["face"])
    if m["converge"] and (bar // 2) % 2 == 1:   # start above, drop onto the target by beat 3
        elev += m["converge"] * (1 - ease((t % 4.0) / 4.0 * 1.33))
    pos = c.place(i, elev + m["lift"] + jitter_offset(m, sign, t), face)
    return Frame(pos=pos, speed=m["speed"])


class Scene:
    """A normalized spec plus a render() that turns it into one Frame per fixture."""

    def __init__(self, spec, builtin=False, modified=False):
        self.spec = normalize(spec)
        self.name, self.label, self.group = self.spec["name"], self.spec["label"], self.spec["group"]
        self.set, self.tags = self.spec["set"], self.spec["tags"]
        self.blurb = self.spec["blurb"]
        self.palette = self.spec["palette"]
        self.builtin, self.modified = builtin, modified

    def render(self, c):
        sp, fx, strip = self.spec, self.spec["fx"], self.spec["strip"]
        t = c.secs if sp["clock"] == "seconds" else c.rb
        bar = int(t // 4)
        if sp["swap"] and (bar // sp["swap"]) % 2:
            c.colours = c.colours[::-1]
        burst, on = strobe_burst(fx, c, t, sp["clock"])
        fill = fill_at(fx, t)
        out = []
        for i in range(c.n):
            f = render_move(sp["move"], c, i, t, bar)
            f.beams = [rgbw(col, lv) for col, lv in render_layer(sp["beams"], c, i, N_BEAMS, t, 1)]
            f.strip = [scale3(col, lv) for col, lv in render_layer(strip, c, i, N_STRIP, t, 2)]
            if strip["rows"] in ("split", "mirror", "offset"):
                f.strip2 = [scale3(col, lv) for col, lv in render_layer(strip, c, i, N_STRIP, t, 2, strip["rows"])]
            elif strip["rows"] == "alt":   # the rows take turns, beat by beat
                f.strip2 = [(0.0, 0.0, 0.0)] * N_STRIP
                if math.floor(t) % 2:
                    f.strip, f.strip2 = f.strip2, f.strip
            f.white = [lv for _, lv in render_layer(sp["white"], c, i, N_WHITE, t, 3)]
            if fill is not None:
                race(f, c, i, fill)
            strike = lightning(fx, c.secs, i, c.flash)
            if strike > 0.01:
                f.white = [max(w, strike) for w in f.white]
                f.map_strip(lambda s: mix(s, STORM_WHITE, min(1.0, strike * 0.8)))
                if fx["lightning_beams"]:
                    f.beams = [(r, g, b, max(w, strike)) for r, g, b, w in f.beams]
            if burst:   # both bars together: a hard on/off strobe, not a fade
                f.white = [on] * N_WHITE
                if fx["strobe_beams"]:
                    f.beams = [(0.0, 0.0, 0.0, on)] * N_BEAMS
            out.append(f)
        return out

    def summary(self):
        return {"name": self.name, "label": self.label, "group": self.group, "set": self.set,
                "tags": self.tags, "blurb": self.blurb, "palette": self.palette, "builtin": self.builtin,
                "modified": self.modified, "spec": self.spec}


# --- presets -----------------------------------------------------------------------------------
def _p(name, label, group, blurb, palette, move=None, beams=None, strip=None, white=None, fx=None, **general):
    return dict({"name": name, "label": label, "group": group, "blurb": blurb, "palette": palette,
                 "move": move or {}, "beams": beams or {"pattern": "off"}, "strip": strip or {"pattern": "off"},
                 "white": white or {"pattern": "off"}, "fx": fx or {}}, **general)


def _halloween(*args, **kw):
    return _p(*args, set="halloween", **kw)


def _window(*args, **kw):
    """For the bars on the windowsill facing the street: the beams stay 35-75 degrees up (never
    level at the houses opposite, never back over the house), the LED side never turns out, and
    there are no strobes or lightning, for the children walking past."""
    kw.setdefault("clock", "seconds")
    return _p(*args, set="window", **kw)


CROWD = {"target": "crowd", "face": "leds", "speed": 0.8}   # LED side to the dance floor, beams up and away
SKY = {"mode": "sweep", "target": "out", "lift": 55, "sweep": 20, "period": 3, "speed": 0.35, "pair": "mirror"}

PRESETS = [normalize(p) for p in [
    # groove
    _p("cross", "Cross", "groove", "Both curtains cross on the balls with comets racing along them every beat; every other 2 bars they drop in from above, and the ranges swap every 4 bars",
       pal(crange(345, 0.95, 0.12), crange(188, 1.0, 0.12)), swap=4,
       move={"mode": "aim", "target": "balls", "speed": 0.9, "converge": 13},
       beams={"pattern": "comet", "color": "own", "rate": 1, "width": 0.6, "floor": 0.3, "pulse": "beat", "decay": 5, "depth": 0.4},
       strip={"pattern": "solid", "color": "own", "rows": "split", "level": 0.2},
       fx={"fill": 4}),
    _p("balls", "Ball jitter", "groove", "The curtains hop across the ball cluster every bar, jittering on 16ths, a new colour for every flash",
       pal(crange(195, 0.45, 0.2), crange(0, 0.0, 0.12)),
       move={"mode": "hop", "hops": 3, "hop_every": 4, "jitter": 2, "pair": "mirror"},
       beams={"pattern": "solid", "color": "step", "rate": 3, "pulse": "8th", "decay": 7, "depth": 0.45},
       strip={"pattern": "sparkle", "color": "own", "rows": "offset", "rate": 0.5, "count": 2, "level": 0.3},
       fx={"fill": 8}),
    _p("sweep", "Sweep + jitter", "groove", "One light tilts across the ceiling each bar with a single beam scanning along it while the other jitters on the balls; they swap every 2 bars",
       pal(crange(155, 1.0, 0.12), crange(310, 0.85, 0.12)),
       move={"mode": "sweep", "target": "ceiling", "period": 1, "speed": 0.7, "pair": "trade", "trade_bars": 2,
             "rest": "balls", "jitter": 2},
       beams={"pattern": "pingpong", "color": "own", "rate": 2, "floor": 0.35, "pulse": "beat", "decay": 4, "depth": 0.3},
       strip={"pattern": "chase", "color": "own", "rows": "mirror", "rate": 2, "width": 0.25, "level": 0.25},
       fx={"fill": 4}),
    _p("pingpong", "Ping pong", "groove", "A flashes on 1 and 3, B answers on 2 and 4, both on the balls, every flash a new colour; the LED rows trade beats too",
       pal(crange(15, 1.0, 0.14), crange(200, 1.0, 0.14)),
       move={"mode": "aim", "target": "balls", "speed": 0.9},
       beams={"pattern": "solid", "color": "step", "rate": 12, "pulse": "2beat", "decay": 4, "depth": 1, "b": "offset"},
       strip={"pattern": "solid", "color": "own", "rows": "alt", "level": 0.18},
       fx={"fill": 8}),
    _p("pendulum", "Pendulum", "groove", "Both curtains swing from the balls up the wall and back each bar; a comet runs along the beams every beat while the LED rows bounce opposite ways",
       pal(crange(40, 1.0, 0.12), crange(340, 0.85, 0.14)),
       move={"mode": "sweep", "target": "balls", "lift": 15, "sweep": 15, "period": 1, "speed": 0.8},
       beams={"pattern": "comet", "color": "own", "rate": 1, "width": 0.5, "floor": 0.3, "pulse": "beat", "decay": 4, "depth": 0.35},
       strip={"pattern": "bounce", "color": "ab", "rows": "mirror", "rate": 4, "width": 0.2, "level": 0.35},
       fx={"fill": 4}),
    _p("scanner", "Scanner", "groove", "LED side to the floor: a band glides end to end on each row, half a cycle apart; whites tick on the 1, a single beam scans the wall behind",
       pal(crange(350, 1.0, 0.08), crange(20, 0.9, 0.12)),
       move=dict(CROWD),
       strip={"pattern": "bounce", "color": "own", "rows": "offset", "rate": 4, "width": 0.12, "b": "mirror"},
       white={"pattern": "bounce", "rate": 4, "width": 0.1, "b": "mirror", "pulse": "one", "decay": 6, "level": 0.6},
       beams={"pattern": "pingpong", "color": "step", "rate": 4, "level": 0.5}),
    _p("shooting", "Shooting stars", "groove", "Beams shoot out one at a time down the line on 16ths, each a new colour, while the curtains tilt slowly over the ceiling",
       pal(crange(285, 0.9, 0.2), crange(185, 1.0, 0.2)),
       move={"mode": "sweep", "target": "ceiling", "sweep": 25, "period": 4, "speed": 0.5, "pair": "mirror"},
       beams={"pattern": "step", "color": "step", "rate": 1.5, "floor": 0.05},
       strip={"pattern": "comet", "color": "own", "rows": "mirror", "rate": 2, "width": 0.4, "level": 0.3},
       fx={"fill": 8}),
    _p("tennis", "Back and forth", "groove", "A single beam runs back and forth along each curtain on 8ths, a new colour every step, on the balls; the LED side rallies too",
       pal(crange(330, 1.0, 0.15), crange(60, 1.0, 0.12)),
       move={"mode": "aim", "target": "balls", "speed": 0.9, "jitter": 1, "jitter_rate": "beat"},
       beams={"pattern": "pingpong", "color": "step", "rate": 5, "floor": 0.1, "b": "mirror"},
       strip={"pattern": "pingpong", "color": "step", "rows": "split", "rate": 7, "level": 0.4},
       fx={"fill": 4}),
    _p("popcorn", "Popcorn", "groove", "One beam pops at a time, somewhere new on every 8th, in colours from both ranges; the LED side sparkles back",
       pal(crange(50, 0.9, 0.2), crange(320, 0.9, 0.2)),
       move={"mode": "aim", "target": "ceiling", "speed": 0.6, "jitter": 1, "jitter_rate": "8th"},
       beams={"pattern": "scatter", "color": "step", "rate": 3, "floor": 0.08},
       strip={"pattern": "sparkle", "color": "random", "rows": "offset", "rate": 0.5, "count": 2, "level": 0.4}),
    # calm
    _p("corner", "Far edge", "calm", "Both curtains graze the far walls' top edges, breathing over 2 bars while a slow ripple runs along them",
       pal(crange(270, 0.85, 0.15), crange(30, 1.0, 0.12)),
       move={"mode": "aim", "target": "corner", "speed": 0.5},
       beams={"pattern": "wave", "color": "ab", "rate": 4, "width": 0.6, "floor": 0.5, "pulse": "breathe", "depth": 0.6, "b": "mirror"},
       strip={"pattern": "wave", "color": "ab", "rows": "split", "rate": 8, "width": 1.0, "level": 0.25, "floor": 0.2}),
    _p("drift", "Slow drift", "calm", "Slow mirrored tilts across the ceiling over 4 bars; colour drifts along the curtains and the LED rows run opposite ways",
       pal(crange(215, 0.85, 0.18), crange(280, 0.6, 0.18)),
       move={"mode": "sweep", "target": "ceiling", "sweep": 35, "period": 4, "speed": 0.35, "pair": "mirror"},
       beams={"pattern": "wave", "color": "ab", "rate": 8, "width": 1.0, "level": 0.75, "floor": 0.35, "pulse": "breathe", "depth": 0.5},
       strip={"pattern": "wave", "color": "own", "rows": "mirror", "rate": 8, "width": 0.8, "level": 0.3, "floor": 0.1}),
    _p("lighthouse", "Lighthouse", "calm", "A slow tilt from level to straight up and back while a lamp sweeps round each curtain",
       pal(crange(205, 0.4, 0.12), crange(0, 0.0, 0.08)),
       move={"mode": "sweep", "target": "ceiling", "sweep": 45, "period": 4, "speed": 0.4, "pair": "mirror"},
       beams={"pattern": "comet", "color": "own", "rate": 4, "width": 0.7, "floor": 0.3, "level": 0.85},
       strip={"pattern": "comet", "color": "own", "rows": "mirror", "rate": 4, "width": 0.5, "level": 0.2}),
    _p("breathe", "Breathe", "calm", "Curtains on the ceiling, a soft two-colour gradient breathing in and out over 2 bars",
       pal(crange(195, 1.0, 0.15), crange(270, 0.85, 0.15)),
       move={"mode": "aim", "target": "ceiling", "speed": 0.4},
       beams={"pattern": "solid", "color": "ab", "level": 0.7, "pulse": "breathe", "depth": 0.8, "b": "mirror"},
       strip={"pattern": "solid", "color": "ab", "rows": "split", "level": 0.18, "pulse": "breathe", "depth": 0.8}),
    _p("shimmer", "Shimmer", "calm", "Every beam and segment drifts at its own pace through colours from both ranges",
       pal(crange(330, 0.6, 0.3), crange(200, 0.6, 0.3)),
       move={"mode": "aim", "target": "ceiling", "speed": 0.4, "jitter": 1, "jitter_rate": "beat"},
       beams={"pattern": "shimmer", "color": "random", "rate": 3, "floor": 0.05},
       strip={"pattern": "shimmer", "color": "random", "rows": "offset", "rate": 2.5, "level": 0.6, "floor": 0.05}),
    _p("rainbowwaves", "Rainbow waves", "calm", "Slow rainbow waves roll along both bars, A into B, the LED rows half a wave apart",
       pal(crange(0, 1.0, 0.5), crange(180, 1.0, 0.5)), own_colors=True,
       move=dict(CROWD, speed=0.4),
       strip={"pattern": "wave", "color": "rainbow", "rows": "offset", "rate": 16, "width": 1.0, "level": 0.85, "floor": 0.15, "b": "offset"},
       beams={"pattern": "wave", "color": "rainbow", "rate": 16, "width": 1.0, "level": 0.35, "floor": 0.2, "b": "offset"}),
    _p("comets", "Comet tails", "calm", "Slow comets glide along the curtains on the ceiling, A and B passing each other",
       pal(crange(175, 0.9, 0.15), crange(260, 0.8, 0.15)),
       move={"mode": "aim", "target": "ceiling", "speed": 0.4},
       beams={"pattern": "comet", "color": "own", "rate": 4, "width": 0.8, "floor": 0.1, "b": "mirror", "pulse": "breathe", "depth": 0.3},
       strip={"pattern": "comet", "color": "own", "rows": "split", "rate": 6, "width": 0.6, "level": 0.25, "b": "mirror"}),
    # peak
    _p("updown", "Side to side", "peak", "A band bounces end to end every 2 beats, the LED rows mirrored, with white 8th hits; a beam ping-pongs on the wall behind",
       pal(crange(345, 1.0, 0.1), crange(200, 1.0, 0.12)),
       move=dict(CROWD),
       strip={"pattern": "bounce", "color": "own", "rows": "mirror", "rate": 2, "width": 0.14, "b": "mirror"},
       white={"pattern": "bounce", "rate": 2, "width": 0.13, "b": "mirror", "pulse": "8th", "decay": 9},
       beams={"pattern": "pingpong", "color": "step", "rate": 2.5, "level": 0.6}),
    _p("midout", "Mid out", "peak", "Rings burst from the middle on every beat (inwards on odd bars), on both rows, the whites and the beams",
       pal(crange(55, 1.0, 0.1), crange(315, 1.0, 0.12)),
       move=dict(CROWD),
       strip={"pattern": "midout", "color": "own", "rows": "split", "rate": 1, "width": 0.3, "flip_bars": True},
       white={"pattern": "midout", "rate": 1, "width": 0.3, "flip_bars": True, "pulse": "beat", "decay": 10},
       beams={"pattern": "midout", "color": "ab", "rate": 1, "width": 0.35, "flip_bars": True, "level": 0.6}),
    _p("rainbow", "Rainbow", "peak", "A rainbow scrolls along the bar once a bar, the rows half a bar apart; whites strobe on the off-beats and rainbow comets run up the wall",
       pal(crange(0, 1.0, 0.5), crange(180, 1.0, 0.5)), own_colors=True,
       move=dict(CROWD),
       strip={"pattern": "solid", "color": "rainbow", "rows": "offset", "rate": 4, "level": 0.9, "b": "offset"},
       white={"pattern": "alternate", "rate": 1, "pulse": "offbeat", "decay": 12},
       beams={"pattern": "comet", "color": "rainbow", "rate": 2, "width": 0.5, "level": 0.6}),
    _p("fill", "Fill + flash", "peak", "The LED rows fill from opposite ends over each bar, then everything flashes on the 1; the beams stack up in a new colour each step",
       pal(crange(25, 1.0, 0.1), crange(170, 1.0, 0.12)),
       move=dict(CROWD),
       strip={"pattern": "fill", "color": "own", "rows": "mirror", "rate": 4, "floor": 0.05},
       white={"pattern": "solid", "pulse": "one", "decay": 5},
       beams={"pattern": "fill", "color": "step", "rate": 4, "level": 0.6}),
    _p("sparkle", "Sparkle", "peak", "Random white segments pop on every 16th over a dim two-colour wash, single beams popping in colour on the wall",
       pal(crange(275, 1.0, 0.12), crange(160, 1.0, 0.12)),
       move=dict(CROWD),
       strip={"pattern": "solid", "color": "ab", "rows": "split", "level": 0.18},
       white={"pattern": "sparkle", "rate": 0.25, "count": 2, "pulse": "16th", "decay": 10},
       beams={"pattern": "scatter", "color": "step", "rate": 1.5, "level": 0.6}),
    _p("strobewall", "Strobe wall", "peak", "LED side to the floor: every white on 16ths over a pulsing wash, the rows trading beats",
       pal(crange(345, 1.0, 0.1), crange(220, 1.0, 0.12)),
       move=dict(CROWD),
       strip={"pattern": "solid", "color": "own", "rows": "alt", "level": 0.4, "pulse": "beat", "decay": 4, "depth": 0.6},
       white={"pattern": "solid", "pulse": "16th", "decay": 14}),
    _p("storm", "Ball storm", "peak", "Both curtains hop across the balls on 8ths with a beam racing along each on 16ths, white sparks and comets on the LED side",
       pal(crange(0, 0.0, 0.1), crange(315, 0.9, 0.15)),
       move={"mode": "hop", "hops": 4, "hop_every": 0.5, "jitter": 1, "pair": "mirror"},
       beams={"pattern": "step", "color": "own", "rate": 1.5, "floor": 0.35, "pulse": "16th", "decay": 8, "depth": 0.5},
       white={"pattern": "sparkle", "rate": 0.25, "count": 1, "pulse": "16th", "decay": 10},
       strip={"pattern": "comet", "color": "b", "rows": "mirror", "rate": 1, "width": 0.4, "level": 0.3},
       fx={"fill": 4}),
    _p("laserrace", "Laser race", "peak", "Beams race down the line on 16ths, every step a new colour, while the curtains hop across the balls; a fill every 2 bars",
       pal(crange(120, 1.0, 0.15), crange(300, 1.0, 0.15)),
       move={"mode": "hop", "hops": 3, "hop_every": 2, "jitter": 1, "pair": "mirror"},
       beams={"pattern": "step", "color": "step", "rate": 1.5, "floor": 0.1, "b": "mirror"},
       strip={"pattern": "step", "color": "step", "rows": "mirror", "rate": 2, "level": 0.5},
       fx={"fill": 2}),
    _p("confetti", "Confetti", "peak", "Single beams pop all over in colours from both ranges on 16ths, the LED side throws sparkles and the curtains shake on the balls",
       pal(crange(50, 0.9, 0.25), crange(290, 0.9, 0.25)),
       move={"mode": "aim", "target": "balls", "speed": 1, "jitter": 2, "jitter_rate": "8th", "pair": "mirror"},
       beams={"pattern": "scatter", "color": "step", "rate": 1.5, "floor": 0.1, "pulse": "16th", "decay": 6, "depth": 0.3},
       strip={"pattern": "sparkle", "color": "random", "rows": "offset", "rate": 0.25, "count": 3, "level": 0.6},
       white={"pattern": "sparkle", "rate": 0.5, "count": 1, "level": 0.5}),
    # ambient (seconds, not beats); these are pictures, so they keep their own colours
    _p("rain", "Light rain", "ambient", "Grey-blue drizzle running down both rows of the LED side, a faint glow on the ceiling",
       pal(crange(220, 0.65, 0.15), crange(225, 0.85, 0.1)), clock="seconds", own_colors=True,
       move={"mode": "aim", "target": "ceiling", "speed": 0.3},
       beams={"pattern": "solid", "color": "b", "level": 0.12, "pulse": "breathe", "depth": 0.4},
       strip={"pattern": "rain", "color": "spread", "rows": "offset", "rate": 1.6, "density": 0.36, "decay": 5, "level": 0.7, "floor": 0.06},
       white={"pattern": "rain", "rate": 2.5, "density": 0.08, "decay": 9, "level": 0.25}),
    _p("downpour", "Downpour", "ambient", "Heavy, fast rain across both bars and both rows, drops spattering on the ceiling",
       pal(crange(220, 0.7, 0.15), crange(230, 0.9, 0.1)), clock="seconds", own_colors=True,
       move={"mode": "aim", "target": "ceiling", "speed": 0.3},
       beams={"pattern": "rain", "color": "a", "rate": 1.2, "density": 0.3, "decay": 7, "level": 0.2},
       strip={"pattern": "rain", "color": "spread", "rows": "offset", "rate": 0.9, "density": 0.7, "decay": 6, "level": 0.85, "floor": 0.05},
       white={"pattern": "rain", "rate": 1.2, "density": 0.2, "decay": 10, "level": 0.35}),
    _p("thunder", "Thunderstorm", "ambient", "Rain in the dark, then lightning: a few strikes a minute, beams and all",
       pal(crange(232, 0.8, 0.12), crange(240, 0.9, 0.08)), clock="seconds", own_colors=True,
       move={"mode": "aim", "target": "ceiling", "speed": 0.3},
       beams={"pattern": "solid", "color": "b", "level": 0.06},
       strip={"pattern": "rain", "color": "spread", "rows": "offset", "rate": 1.1, "density": 0.5, "decay": 6, "level": 0.6, "floor": 0.04},
       fx={"lightning": 5, "lightning_level": 1.0, "lightning_beams": True}),
    _p("distant", "Distant storm", "ambient", "Indigo calm drifting along both rows, with faint lightning far away",
       pal(crange(255, 0.7, 0.15), crange(250, 0.85, 0.1)), clock="seconds", own_colors=True, tags=["washers"],
       move={"mode": "sweep", "target": "corner", "sweep": 10, "period": 8, "speed": 0.25},
       strip={"pattern": "wave", "color": "spread", "rows": "mirror", "rate": 20, "width": 1.0, "level": 0.2, "floor": 0.3, "pulse": "breathe", "depth": 0.5},
       beams={"pattern": "solid", "color": "b", "level": 0.08, "pulse": "breathe", "depth": 0.5},
       fx={"lightning": 2, "lightning_level": 0.5}),
    _p("campfire", "Campfire", "ambient", "Embers and flames flickering along both rows of the LED side, a warm glow above",
       pal(crange(18, 1.0, 0.1), crange(38, 1.0, 0.12)), clock="seconds", own_colors=True,
       move=dict(CROWD, speed=0.3),
       strip={"pattern": "flicker", "color": "random", "rows": "offset", "rate": 1.5, "level": 0.9, "floor": 0.15},
       beams={"pattern": "flicker", "color": "a", "rate": 3, "level": 0.2}),
    _p("aurora", "Aurora", "ambient", "Green, teal and violet curtains drifting slowly across the ceiling",
       pal(crange(150, 1.0, 0.2), crange(260, 0.85, 0.2)), clock="seconds", own_colors=True,
       move={"mode": "sweep", "target": "ceiling", "sweep": 20, "period": 5, "speed": 0.25, "pair": "mirror"},
       beams={"pattern": "wave", "color": "scroll", "rate": 14, "width": 1.0, "level": 0.4, "floor": 0.1},
       strip={"pattern": "wave", "color": "scroll", "rows": "offset", "rate": 10, "width": 1.0, "level": 0.7, "floor": 0.1, "b": "offset"}),
    _p("ocean", "Deep ocean", "ambient", "Blue-green swells rolling along both rows, light rippling on the ceiling",
       pal(crange(210, 1.0, 0.15), crange(180, 1.0, 0.12)), clock="seconds", own_colors=True,
       move={"mode": "sweep", "target": "ceiling", "sweep": 12, "period": 3, "speed": 0.25},
       beams={"pattern": "wave", "color": "spread", "rate": 7, "width": 0.7, "level": 0.35, "floor": 0.2},
       strip={"pattern": "wave", "color": "spread", "rows": "mirror", "rate": 9, "width": 0.9, "level": 0.6, "floor": 0.2, "b": "offset"}),
    _p("stars", "Starfield", "ambient", "Faint beams twinkle on the ceiling like stars over a deep blue glow",
       pal(crange(220, 0.15, 0.12), crange(230, 0.9, 0.08)), clock="seconds", own_colors=True,
       move={"mode": "aim", "target": "up", "speed": 0.2},
       beams={"pattern": "twinkle", "color": "a", "rate": 3, "density": 0.4, "level": 0.25},
       strip={"pattern": "twinkle", "color": "b", "rows": "offset", "rate": 5, "density": 0.2, "level": 0.15, "floor": 0.1},
       white={"pattern": "twinkle", "rate": 4, "density": 0.06, "level": 0.15}),
    _p("sunset", "Sunset", "ambient", "Orange, pink and purple sliding very slowly along the bars",
       pal(crange(25, 1.0, 0.15), crange(320, 0.85, 0.18)), clock="seconds", own_colors=True,
       move={"mode": "aim", "target": "ceiling", "speed": 0.25},
       beams={"pattern": "solid", "color": "scroll", "rate": 32, "level": 0.3},
       strip={"pattern": "solid", "color": "scroll", "rows": "offset", "rate": 30, "level": 0.8, "b": "offset"}),
    _p("fireflies", "Fireflies", "ambient", "Warm little lights blinking on and off in the dark, on both rows and up the wall",
       pal(crange(75, 0.75, 0.1), crange(50, 0.6, 0.12)), clock="seconds", own_colors=True,
       move=dict(CROWD, speed=0.2),
       strip={"pattern": "twinkle", "color": "random", "rows": "offset", "rate": 2.5, "density": 0.16, "level": 0.9},
       beams={"pattern": "twinkle", "color": "random", "rate": 4, "density": 0.12, "level": 0.15}),

    # Halloween (the party set): patterns on the beams and the LED side, the whites only for
    # the odd strobe burst (Strobe freq on Show / Perform turns those up or off)
    _halloween("pumpkin", "Pumpkin patch", "groove", "Pumpkin orange and witch purple comets chase along both curtains on the balls, candlelight flickers on the LED side, now and then a strobe burst",
               pal(crange(25, 1.0, 0.1), crange(275, 0.85, 0.12)),
               move={"mode": "aim", "target": "balls", "speed": 0.9, "pair": "mirror", "converge": 12},
               beams={"pattern": "comet", "color": "own", "rate": 2, "width": 0.4, "floor": 0.15, "pulse": "beat",
                      "decay": 4, "depth": 0.35, "b": "mirror"},
               strip={"pattern": "flicker", "color": "random", "rows": "split", "rate": 1.5, "level": 0.35, "floor": 0.1},
               fx={"strobe": 2, "strobe_beams": True, "fill": 8}),
    _halloween("ghosts", "Ghost train", "groove", "Pale ghosts drift along the curtains as they tilt across the ceiling, and along both rows of the LED side",
               pal(crange(165, 0.2, 0.12), crange(255, 0.35, 0.12)),
               move={"mode": "sweep", "target": "ceiling", "sweep": 30, "period": 2, "speed": 0.6, "pair": "mirror"},
               beams={"pattern": "wander", "color": "own", "rate": 4, "width": 0.45, "floor": 0.05, "b": "offset"},
               strip={"pattern": "wander", "color": "own", "rows": "offset", "rate": 6, "width": 0.4, "level": 0.35},
               fx={"strobe": 1.5, "strobe_beams": True}),
    _halloween("monster", "Monster mash", "groove", "LED side to the dance floor: slime green and purple bounce both ways along the rows while the beams trade places on the wall behind",
               pal(crange(110, 1.0, 0.1), crange(280, 1.0, 0.1)),
               move=dict(CROWD),
               strip={"pattern": "bounce", "color": "own", "rows": "mirror", "rate": 2, "width": 0.22, "b": "mirror"},
               beams={"pattern": "alternate", "color": "own", "rate": 1, "level": 0.8, "b": "offset"},
               fx={"strobe": 2.5, "fill": 4}),
    _halloween("skeleton", "Skeleton dance", "groove", "Bones rattling: a bone-white beam rattles back and forth along each curtain as they hop the balls every beat, the LED rows clattering in turn",
               pal(crange(45, 0.12, 0.1), crange(275, 0.6, 0.12)),
               move={"mode": "hop", "hops": 2, "hop_every": 1, "pair": "mirror"},
               beams={"pattern": "pingpong", "color": "own", "rate": 2.5, "floor": 0.15, "pulse": "8th", "decay": 6, "depth": 0.3},
               strip={"pattern": "alternate", "color": "own", "rows": "alt", "rate": 0.5, "level": 0.6},
               white={"pattern": "alternate", "rate": 1, "pulse": "beat", "decay": 9, "level": 0.5, "b": "offset"},
               fx={"fill": 4}),
    _halloween("cauldron", "Witch's cauldron", "groove", "A bubbling green brew: beams pop like bubbles on the beat over a purple swirl, the LED rows fizzing",
               pal(crange(120, 1.0, 0.12), crange(285, 0.9, 0.12)),
               move={"mode": "aim", "target": "ceiling", "speed": 0.6, "jitter": 1, "jitter_rate": "beat", "pair": "mirror"},
               beams={"pattern": "twinkle", "color": "own", "rate": 1, "density": 0.4, "floor": 0.2, "pulse": "beat", "decay": 4, "depth": 0.3},
               strip={"pattern": "rain", "color": "spread", "rows": "offset", "rate": 1, "density": 0.4, "decay": 5, "level": 0.6},
               fx={"strobe": 1.5, "fill": 8}),
    _halloween("witching", "Witching hour", "calm", "Purple and green roll across the ceiling to a slow heartbeat while stars twinkle on both rows of the LED side",
               pal(crange(272, 0.9, 0.12), crange(155, 1.0, 0.12)),
               move={"mode": "aim", "target": "ceiling", "speed": 0.4},
               beams={"pattern": "wave", "color": "ab", "rate": 8, "width": 0.8, "floor": 0.2, "level": 0.8,
                      "pulse": "heartbeat", "decay": 4, "depth": 0.45, "b": "offset"},
               strip={"pattern": "twinkle", "color": "spread", "rows": "offset", "rate": 3, "density": 0.3, "level": 0.5},
               fx={"strobe": 1}),
    _halloween("heart", "Tell-tale heart", "calm", "Blood red on the balls: a heartbeat thumps through A, then B, and pumps out from the middle of the LED side, building to the drop",
               pal(crange(355, 1.0, 0.06), crange(350, 1.0, 0.08)), own_colors=True,
               move={"mode": "aim", "target": "balls", "speed": 0.5},
               beams={"pattern": "solid", "color": "own", "pulse": "heartbeat", "decay": 5, "depth": 0.9, "b": "offset"},
               strip={"pattern": "midout", "color": "b", "rate": 2, "width": 0.4, "level": 0.25, "pulse": "heartbeat",
                      "decay": 5, "depth": 0.9, "b": "offset"},
               fx={"strobe": 1}),
    _halloween("graveyard", "Graveyard fog", "calm", "Pale green fog rolls slowly over the ceiling while a pair of eyes watches from the LED side, and far-off lightning",
               pal(crange(150, 0.55, 0.15), crange(215, 0.45, 0.15)),
               move={"mode": "sweep", "target": "ceiling", "sweep": 15, "period": 8, "speed": 0.3, "pair": "mirror"},
               beams={"pattern": "wave", "color": "ab", "rate": 12, "width": 1.0, "level": 0.5, "floor": 0.25},
               strip={"pattern": "eyes", "color": "a", "rows": "split", "rate": 6, "count": 1, "level": 0.6},
               fx={"lightning": 1, "lightning_level": 0.6}),
    _halloween("alive", "It's alive!", "peak", "Frankenstein's lab: electric green curtains glitch as they hop across the balls, lightning cracks and the strobes burst more often",
               pal(crange(85, 1.0, 0.12), crange(175, 0.3, 0.12)),
               move={"mode": "hop", "hops": 4, "hop_every": 1, "jitter": 1, "pair": "mirror"},
               beams={"pattern": "glitch", "color": "own", "rate": 2, "density": 0.3, "pulse": "8th", "decay": 6, "depth": 0.3},
               strip={"pattern": "midout", "color": "ab", "rows": "split", "rate": 1, "width": 0.3, "flip_bars": True, "level": 0.6},
               fx={"lightning": 4, "lightning_beams": True, "strobe": 4, "strobe_len": 2, "strobe_beams": True}),
    _halloween("poltergeist", "Poltergeist", "peak", "Things go bump: single beams pop up at random on 16ths while the curtains jitter on the balls and the LED side glitches",
               pal(crange(25, 1.0, 0.15), crange(275, 0.9, 0.2)),
               move={"mode": "aim", "target": "balls", "speed": 1, "jitter": 2, "jitter_rate": "16th", "pair": "mirror"},
               beams={"pattern": "scatter", "color": "step", "rate": 1.5, "floor": 0.1, "pulse": "16th", "decay": 9, "depth": 0.5},
               strip={"pattern": "glitch", "color": "random", "rows": "offset", "rate": 2, "density": 0.4, "level": 0.6},
               fx={"strobe": 4, "strobe_beams": True}),
    _halloween("bats", "Bat swarm", "peak", "A swarm of bats: single beams flutter all over on 32nds while the curtains dart across the balls and comets swoop along both rows",
               pal(crange(280, 1.0, 0.1), crange(15, 1.0, 0.08)),
               move={"mode": "hop", "hops": 3, "hop_every": 0.5, "jitter": 2, "pair": "mirror"},
               beams={"pattern": "scatter", "color": "own", "rate": 0.75},
               strip={"pattern": "comet", "color": "own", "rows": "mirror", "rate": 1, "width": 0.3, "level": 0.7, "b": "mirror"},
               fx={"strobe": 3, "strobe_beams": True}),
    _halloween("halloween", "Halloween", "ambient", "Pumpkin orange and witchy purple rolling past, candle flicker, the odd crack of lightning",
               pal(crange(25, 1.0, 0.12), crange(275, 0.85, 0.15)), clock="seconds",
               move={"mode": "sweep", "target": "ceiling", "sweep": 20, "period": 6, "speed": 0.3, "pair": "mirror"},
               beams={"pattern": "wave", "color": "own", "rate": 8, "width": 0.8, "level": 0.45, "floor": 0.3, "pulse": "breathe", "depth": 0.4},
               strip={"pattern": "wave", "color": "spread", "rows": "offset", "rate": 12, "width": 0.8, "level": 0.8, "floor": 0.25, "b": "offset"},
               fx={"lightning": 1.2, "lightning_level": 0.9, "lightning_beams": True}),
    _halloween("haunted", "Haunted house", "ambient", "LED side to the room: eyes open in the dark and blink, a dying bulb flickers up the wall, thunder now and then",
               pal(crange(38, 0.75, 0.08), crange(140, 1.0, 0.1)), clock="seconds",
               move=dict(CROWD, speed=0.3),
               strip={"pattern": "eyes", "color": "b", "rate": 7, "count": 2, "level": 0.9},
               beams={"pattern": "glitch", "color": "a", "rate": 4, "density": 0.2, "level": 0.5},
               fx={"lightning": 1.5, "lightning_level": 0.9, "lightning_beams": True, "strobe": 1}),

    # Halloween window: the bars on the windowsill facing the street (see _window)
    _p("window", "Haunted window", "ambient", "The spooky one: beams sweep the sky 35-75 degrees up, crossing like searchlights with ghosts drifting along them; candlelight flickers in the window, now and then lightning or a strobe burst",
       pal(crange(25, 1.0, 0.1), crange(270, 1.0, 0.1)), set="window", clock="seconds", swap=6,
       move=dict(SKY),
       beams={"pattern": "wander", "color": "own", "rate": 5, "width": 0.5, "floor": 0.2, "b": "offset"},
       strip={"pattern": "flicker", "color": "random", "rows": "offset", "rate": 1.5, "level": 0.6, "floor": 0.15},
       fx={"strobe": 1.5, "strobe_beams": True, "lightning": 1, "lightning_level": 0.8}),
    _window("parade", "Pumpkin parade", "ambient", "Pumpkins marching one at a time along the beams over an orange glow, leafy green and orange in the window",
            pal(crange(28, 1.0, 0.1), crange(110, 0.9, 0.08)), own_colors=True,
            move=dict(SKY, sweep=15, period=4, pair="same"),
            beams={"pattern": "step", "color": "a", "rate": 3, "floor": 0.25},
            strip={"pattern": "step", "color": "own", "rows": "split", "rate": 4, "floor": 0.3, "level": 0.8}),
    _window("candycorn", "Candy corn", "ambient", "Candy-corn yellow, orange and white stacking up the beams and striping both rows",
            pal(crange(35, 1.0, 0.18), crange(40, 0.05, 0.05)), own_colors=True,
            move=dict(SKY),
            beams={"pattern": "fill", "color": "step", "rate": 3, "floor": 0.1},
            strip={"pattern": "solid", "color": "spread", "rows": "split", "level": 0.7}),
    _window("friendlyghosts", "Friendly ghosts", "ambient", "Soft, friendly ghosts floating along the beams and peeking along the window, slowly glowing",
            pal(crange(170, 0.15, 0.1), crange(200, 0.3, 0.1)),
            move=dict(SKY, period=5, speed=0.3),
            beams={"pattern": "wander", "color": "own", "rate": 4, "width": 0.5, "floor": 0.1, "b": "offset"},
            strip={"pattern": "wander", "color": "own", "rows": "offset", "rate": 5, "width": 0.5, "level": 0.7, "pulse": "breathe", "depth": 0.4}),
    _window("batflight", "Bat flight", "ambient", "Purple bats swooping back and forth across the sky and along the window",
            pal(crange(275, 1.0, 0.1), crange(300, 0.7, 0.12)),
            move=dict(SKY, period=1.5, speed=0.6),
            beams={"pattern": "pingpong", "color": "own", "rate": 2, "floor": 0.15},
            strip={"pattern": "comet", "color": "own", "rows": "mirror", "rate": 1.5, "width": 0.4, "level": 0.7}),
    _window("brew", "Witch's brew", "ambient", "Bubbling green and purple brew: beams pop like bubbles over a swirling glow in the window",
            pal(crange(115, 1.0, 0.12), crange(285, 0.9, 0.12)),
            move=dict(SKY, sweep=10, period=4, speed=0.3),
            beams={"pattern": "twinkle", "color": "own", "rate": 1.5, "density": 0.5, "floor": 0.25},
            strip={"pattern": "wave", "color": "spread", "rows": "offset", "rate": 4, "width": 0.7, "level": 0.7, "floor": 0.2}),
    _window("monsterdisco", "Monster disco", "groove", "A monster dance party to the beat: beams pop in party colours and rainbows bounce both ways across the window",
            pal(crange(310, 1.0, 0.2), crange(130, 1.0, 0.2)), clock="beats",
            move=dict(SKY, period=2, speed=0.5),
            beams={"pattern": "scatter", "color": "step", "rate": 3, "floor": 0.1},
            strip={"pattern": "bounce", "color": "rainbow", "rows": "mirror", "rate": 2, "width": 0.25, "level": 0.8},
            fx={"fill": 4}),
    _window("spiderweb", "Spider web", "ambient", "Silvery web rings spreading from the middle of the beams, with dewdrops glinting along the window",
            pal(crange(0, 0.0, 0.06), crange(275, 0.8, 0.12)),
            move=dict(SKY, sweep=15, period=6, speed=0.3, pair="same"),
            beams={"pattern": "midout", "color": "own", "rate": 3, "width": 0.3, "floor": 0.15},
            strip={"pattern": "twinkle", "color": "a", "rows": "offset", "rate": 3, "density": 0.2, "level": 0.6, "floor": 0.1}),
    _window("trickortreat", "Trick or treat", "ambient", "Candy colours shooting one at a time along the beams while sweets chase both ways along the window",
            pal(crange(330, 0.8, 0.2), crange(35, 1.0, 0.2)),
            move=dict(SKY, speed=0.4),
            beams={"pattern": "step", "color": "step", "rate": 2, "floor": 0.1, "b": "mirror"},
            strip={"pattern": "chase", "color": "scroll", "rows": "mirror", "rate": 3, "width": 0.3, "level": 0.8}),
]]
PRESET_NAMES = {p["name"] for p in PRESETS}


def library(saved):
    """Presets overlaid by the config's saved scenes -> {name: Scene}, grouped in order."""
    saved = {s["name"]: s for s in (normalize(x) for x in (saved or []))}
    scenes = [Scene(saved.get(p["name"], p), builtin=True, modified=p["name"] in saved) for p in PRESETS]
    scenes += [Scene(s) for name, s in saved.items() if name not in PRESET_NAMES]
    scenes.sort(key=lambda s: GROUPS.index(s.group))   # stable: presets keep their order
    return {s.name: s for s in scenes}


def preset(name):
    for p in PRESETS:
        if p["name"] == name:
            return copy.deepcopy(p)
    return None
