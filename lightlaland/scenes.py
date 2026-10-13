"""Scenes: a scene is data (a "spec"), and one renderer turns any spec into what each light
does this frame, from musical time (or plain seconds, for ambient scenes).

A spec has a palette of five colours, a movement, three light layers (the beams, the LED
strip and the white strobes) and effects (lightning). SCHEMA lists every field with its
default and limits; the scene maker in the web UI is built from it, and normalize() makes
any incoming spec safe to render. The built-in scenes are PRESETS. Scenes saved from the
maker live in the config ("scenes") and override the preset with the same name.

The bars are mounted horizontally, so movement is tilt: the head turns about the bar's long
axis (see geometry.py). Beam safety is enforced by the engine, not here: a scene can ask
for anything and the beams still go dark where they'd point below level.

Fixture 0 is A (left), 1 is B (right). Index 0 of beams/strip/whites is the first end of the bar.
"""
import colorsys
import copy
import math
import random
import re

from . import geometry
from .fixture import N_BEAMS, N_STRIP, N_WHITE, Frame
from .geometry import DEG_PER_STEP

GROUPS = ("groove", "calm", "peak", "ambient")


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
    for k, x in enumerate(xs):
        v += x * (12.9898 + 31.4159 * k)
    s = math.sin(v + 78.233) * 43758.5453
    return s - math.floor(s)


# --- the spec ------------------------------------------------------------------------------
TARGETS = [["ceiling", "Ceiling, mid-room"], ["up", "Straight up"], ["out", "Level, across the room"],
           ["corner", "Far wall's top edge"], ["balls", "Ball cluster"], ["crowd", "Dance floor"]]
FACES = [["beams", "The beams"], ["leds", "The LED side"],
         ["auto", "Beams if it's safe, else the LED side"]]
PULSES = [["none", "Steady"], ["beat", "Every beat"], ["8th", "8ths"], ["16th", "16ths"],
          ["offbeat", "Off-beats"], ["2beat", "Every other beat"], ["one", "The 1 of each bar"],
          ["breathe", "Slow breathe"]]
PATTERNS = [["off", "Off"], ["solid", "All on"], ["chase", "Chase"], ["bounce", "Bounce"],
            ["midout", "Rings from the middle"], ["fill", "Fill from one end"], ["wave", "Wave"],
            ["shimmer", "Shimmer"], ["sparkle", "Sparkle"], ["twinkle", "Twinkle"], ["rain", "Rain drops"],
            ["flicker", "Fire flicker"], ["alternate", "Alternate segments"]]
COLOURS = [["own", "A uses colour 1, B uses 2"], ["a", "Colour 1"], ["b", "Colour 2"], ["c", "Colour 3"],
           ["ab", "Gradient 1 to 2"], ["spread", "Whole palette along the bar"],
           ["scroll", "Palette scrolling along"], ["random", "Random palette colours"],
           ["rainbow", "Rainbow"], ["white", "White"]]
B_MODES = [["same", "Same as A"], ["mirror", "Mirrored"], ["offset", "Half a cycle later"]]


def _choice(key, default, options, label, hint=""):
    return {"key": key, "kind": "choice", "default": default, "options": options, "label": label, "hint": hint}


def _num(key, default, lo, hi, step, label, hint=""):
    return {"key": key, "kind": "num", "default": default, "min": lo, "max": hi, "step": step,
            "label": label, "hint": hint}


def _bool(key, default, label, hint=""):
    return {"key": key, "kind": "bool", "default": default, "label": label, "hint": hint}


def _layer(colour=True, default_pattern="off"):
    fields = [_choice("pattern", default_pattern, PATTERNS, "Pattern")]
    if colour:
        fields.append(_choice("color", "own", COLOURS, "Colour"))
    fields += [
        _num("level", 1.0, 0, 1, 0.05, "Brightness"),
        _num("floor", 0.0, 0, 1, 0.05, "Never below", "Brightness of the parts the pattern leaves dark"),
        _choice("pulse", "none", PULSES, "Pulse"),
        _num("depth", 1.0, 0, 1, 0.05, "Pulse depth", "0 = steady, 1 = full flashes"),
        _num("decay", 6, 1, 16, 0.5, "Flash length", "Higher = shorter flashes"),
        _num("rate", 2, 0.25, 32, 0.25, "Cycle", "Beats (or seconds on the seconds clock) for one chase, wave, drop..."),
        _num("width", 0.25, 0.05, 1, 0.01, "Band width", "Share of the bar a band or wave covers"),
        _num("count", 2, 1, 8, 1, "Sparkles at once"),
        _num("density", 0.3, 0.02, 1, 0.02, "How many", "Share of segments that rain or twinkle at a time"),
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
        {"key": "blurb", "kind": "text", "default": "", "label": "Description"},
        _choice("clock", "beats", [["beats", "The music's beats"], ["seconds", "Seconds (ambient)"]], "Runs on"),
        _num("swap", 0, 0, 16, 1, "Swap colours 1 and 2 every (bars)", "0 = never"),
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
    "strip": _layer(),
    "white": _layer(colour=False),
    "fx": [
        _num("lightning", 0, 0, 20, 0.5, "Lightning (strikes a minute)", "0 = none. Strikes flash the whites"),
        _num("lightning_level", 1.0, 0.1, 1, 0.05, "Lightning brightness"),
        _bool("lightning_beams", False, "Lightning also flashes the beams"),
    ],
}
DEFAULT_PALETTE = ["#ff1f5a", "#ff6a3d", "#ffb000", "#00e1ff", "#7b2cff"]
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
            else:
                v = str(v)[:80]
        except (TypeError, ValueError):
            v = f["default"]
        out[f["key"]] = v
    return out


def _palette(raw):
    """Five #rrggbb colours. Also takes the old {"a", "b", "c"} form."""
    if isinstance(raw, dict):
        raw = [raw.get(k) for k in ("a", "b", "c")]
    raw = [c.lower() for c in (raw if isinstance(raw, list) else []) if isinstance(c, str) and HEX_RE.fullmatch(c)]
    if not raw:
        return list(DEFAULT_PALETTE)
    while len(raw) < 5:   # fill up with lighter versions of what's there
        c = hex_rgb(raw[len(raw) % len(raw)] if len(raw) < 3 else raw[len(raw) - 3])
        raw.append(rgb_hex(mix(c, (1.0, 1.0, 1.0), 0.4)))
    return raw[:5]


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
        out[part] = _norm_fields(SCHEMA[part], spec.get(part))
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
    def __init__(self, cfg, info, palette, prev, speed=1.0, build=0.0, secs=0.0, windows=None):
        self.fixtures = cfg["fixtures"]
        self.n = len(self.fixtures)
        self.room = cfg["room"]
        self.points = {p["id"]: p for p in cfg["points"]}
        self.sweep = float(cfg["show"]["sweep_width"])
        self.ranges = [geometry.safe_range(fx, cfg["safety"]) for fx in self.fixtures]   # (low, high)
        self.windows = windows or [[] for _ in range(self.n)]   # copies of the safe arc per fixture
        self.info = info
        self.palette = palette          # list of five (r, g, b)
        self.prev = prev
        self.build = build
        self.speed = speed
        rb = (info["beat"] - info["anchor"]) * speed  # beats since the phrase started
        self.rb = rb
        self.secs = secs * speed
        self.ph = rb % 1.0                    # phase within the beat
        self.bib = int(math.floor(rb)) % 4    # beat in bar
        self.bar = int(math.floor(rb / 4.0))
        self.bar_ph = (rb % 4.0) / 4.0        # phase within the bar

    def color(self, key):
        idx = {"a": 0, "b": 1, "c": 2}.get(key, key)
        return self.palette[idx % len(self.palette)] if self.palette else (1.0, 1.0, 1.0)

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
PULSE_PERIOD = {"beat": 1.0, "8th": 0.5, "16th": 0.25, "offbeat": 1.0, "2beat": 2.0, "one": 4.0}
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


def pattern_value(layer, x, s, u, t, c, i, n, seed):
    pat, w, rate = layer["pattern"], layer["width"], layer["rate"]
    if pat == "solid":
        return 1.0
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
        step = int(math.floor(t / rate))
        lit = random.Random(step * 7919 + i * 104729 + n).sample(range(n), min(layer["count"], n))
        return 1.0 if s in lit else 0.0
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
    return 0.0


def layer_colour(layer, x, u, t, c, i, seed):
    kind = layer.get("color", "white")
    pal = c.palette
    if kind == "own":
        return c.color(0 if i % 2 == 0 else 1)
    if kind in ("a", "b", "c"):
        return c.color(kind)
    if kind == "ab":
        return mix(c.color(0), c.color(1), x)
    if kind in ("spread", "scroll"):
        v = ((x + (u if kind == "scroll" else 0.0)) % 1.0) * len(pal)
        k = int(v) % len(pal)
        return mix(pal[k], pal[(k + 1) % len(pal)], v - int(v))
    if kind == "random":   # each segment picks a palette colour, and changes at its own moment
        slot = math.floor(t / (layer["rate"] * 4) + h01(seed, 5))
        return pal[int(h01(seed, slot, 7) * len(pal)) % len(pal)]
    if kind == "rainbow":
        return colorsys.hsv_to_rgb((x * 0.6 + u) % 1.0, 1.0, 1.0)
    return (1.0, 1.0, 1.0)


def render_layer(layer, c, i, n, t, salt):
    """One light layer of fixture i -> list of (colour rgb, level) for its n segments."""
    if layer["pattern"] == "off" or layer["level"] <= 0:
        return [((0.0, 0.0, 0.0), 0.0)] * n
    is_b = i % 2 == 1
    shift = 0.5 if is_b and layer["b"] == "offset" else 0.0
    u = (t / layer["rate"] + shift) % 1.0
    env = envelope(layer, t, is_b)
    level = layer["level"] * ((1 - layer["depth"]) + layer["depth"] * env)
    out = []
    for s in range(n):
        x = s / (n - 1) if n > 1 else 0.5
        if layer["reverse"]:
            x = 1 - x
        if is_b and layer["b"] == "mirror":
            x = 1 - x
        if layer["flip_bars"] and c.bar % 2 and layer["pattern"] != "midout":
            x = 1 - x
        seed = salt * 1000 + i * 100 + s
        p = pattern_value(layer, x, s, u, t, c, i, n, seed)
        p = layer["floor"] + (1 - layer["floor"]) * p
        out.append((layer_colour(layer, x, u, t, c, i, seed), level * p))
    return out


def lightning(fx, secs, i):
    """Thunderstorm flashes: random strikes of a few quick flashes (always in seconds)."""
    rate = fx["lightning"]
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
        self.blurb = self.spec["blurb"]
        self.palette = self.spec["palette"]
        self.builtin, self.modified = builtin, modified

    def render(self, c):
        sp = self.spec
        t = c.secs if sp["clock"] == "seconds" else c.rb
        bar = int(t // 4)
        if sp["swap"] and (bar // sp["swap"]) % 2:
            c.palette = [c.palette[1], c.palette[0]] + list(c.palette[2:])
        out = []
        for i in range(c.n):
            f = render_move(sp["move"], c, i, t, bar)
            f.beams = [rgbw(col, lv) for col, lv in render_layer(sp["beams"], c, i, N_BEAMS, t, 1)]
            f.strip = [scale3(col, lv) for col, lv in render_layer(sp["strip"], c, i, N_STRIP, t, 2)]
            f.white = [lv for _, lv in render_layer(sp["white"], c, i, N_WHITE, t, 3)]
            strike = lightning(sp["fx"], c.secs, i)
            if strike > 0.01:
                f.white = [max(w, strike) for w in f.white]
                f.strip = [mix(s, STORM_WHITE, min(1.0, strike * 0.8)) for s in f.strip]
                if sp["fx"]["lightning_beams"]:
                    f.beams = [(r, g, b, max(w, strike)) for r, g, b, w in f.beams]
            out.append(f)
        return out

    def summary(self):
        return {"name": self.name, "label": self.label, "group": self.group, "blurb": self.blurb,
                "palette": self.palette, "builtin": self.builtin, "modified": self.modified,
                "spec": self.spec}


# --- presets -----------------------------------------------------------------------------------
def _p(name, label, group, blurb, palette, move=None, beams=None, strip=None, white=None, swap=0,
       clock="beats", fx=None):
    return {"name": name, "label": label, "group": group, "blurb": blurb, "palette": palette, "swap": swap,
            "clock": clock, "move": move or {}, "beams": beams or {"pattern": "off"},
            "strip": strip or {"pattern": "off"}, "white": white or {"pattern": "off"}, "fx": fx or {}}


CROWD = {"target": "crowd", "face": "leds", "speed": 0.8}   # LED side to the dance floor, beams up and away

PRESETS = [normalize(p) for p in [
    # groove
    _p("cross", "Cross", "groove", "Both curtains cross on the balls; colours swap every 4 bars, and every other 2 bars they drop in from above",
       ["#ff1f5a", "#00e1ff", "#ff7aa0", "#7af0ff", "#ffffff"], swap=4,
       move={"mode": "aim", "target": "balls", "speed": 0.9, "converge": 13},
       beams={"pattern": "solid", "pulse": "beat", "decay": 5, "depth": 0.55},
       strip={"pattern": "solid", "level": 0.1}),
    _p("balls", "Ball jitter", "groove", "The curtains hop across the ball cluster every bar, jittering on 16ths",
       ["#ffffff", "#8ff7ff", "#c8d8ff", "#ffffff", "#8ff7ff"],
       move={"mode": "hop", "hops": 3, "hop_every": 4, "jitter": 2, "pair": "mirror"},
       beams={"pattern": "solid", "pulse": "8th", "decay": 7, "depth": 0.45},
       strip={"pattern": "solid", "level": 0.06}),
    _p("sweep", "Sweep + jitter", "groove", "One light tilts across the ceiling each bar while the other jitters on the balls; swap every 2 bars",
       ["#00ff9c", "#ff2bd6", "#7dffcf", "#ff8de9", "#ffffff"],
       move={"mode": "sweep", "target": "ceiling", "period": 1, "speed": 0.7, "pair": "trade", "trade_bars": 2,
             "rest": "balls", "jitter": 2},
       beams={"pattern": "solid", "pulse": "beat", "decay": 4, "depth": 0.42}),
    _p("pingpong", "Ping pong", "groove", "A flashes on 1 and 3, B answers on 2 and 4, both on the balls",
       ["#ff3d00", "#00b3ff", "#ff8a5c", "#66d4ff", "#ffffff"],
       move={"mode": "aim", "target": "balls", "speed": 0.9},
       beams={"pattern": "solid", "pulse": "2beat", "decay": 4, "depth": 1, "b": "offset"},
       strip={"pattern": "solid", "level": 0.08}),
    _p("pendulum", "Pendulum", "groove", "Both curtains swing from the balls up the wall and back each bar; a band runs along the beams each beat",
       ["#ffb000", "#ff2d6f", "#ffd666", "#ff7aa0", "#ffffff"],
       move={"mode": "sweep", "target": "balls", "lift": 15, "sweep": 15, "period": 1, "speed": 0.8},
       beams={"pattern": "chase", "rate": 1, "width": 0.45, "floor": 0.3, "pulse": "beat", "decay": 4, "depth": 0.4}),
    _p("scanner", "Scanner", "groove", "LED side to the floor: a narrow band glides end to end, whites tick on the 1",
       ["#ff0030", "#ff0030", "#ff5c75", "#ff0030", "#ffffff"],
       move=dict(CROWD),
       strip={"pattern": "bounce", "color": "a", "rate": 4, "width": 0.12, "b": "mirror"},
       white={"pattern": "bounce", "rate": 4, "width": 0.1, "b": "mirror", "pulse": "one", "decay": 6, "level": 0.6}),
    # calm
    _p("corner", "Far edge", "calm", "Both curtains graze the far walls' top edges, breathing over 2 bars with a slow ripple",
       ["#7b2cff", "#ff8a00", "#b47dff", "#ffb35c", "#ffffff"],
       move={"mode": "aim", "target": "corner", "speed": 0.5},
       beams={"pattern": "chase", "color": "ab", "rate": 2, "width": 0.5, "floor": 0.65, "pulse": "breathe",
              "depth": 0.7, "b": "mirror"}),
    _p("drift", "Slow drift", "calm", "Slow mirrored tilts across the ceiling over 4 bars, soft colour gradient along the curtain",
       ["#3a86ff", "#c77dff", "#8fb8ff", "#e0b3ff", "#ffffff"],
       move={"mode": "sweep", "target": "ceiling", "sweep": 35, "period": 4, "speed": 0.35, "pair": "mirror"},
       beams={"pattern": "solid", "color": "ab", "level": 0.75, "pulse": "breathe", "depth": 0.67}),
    _p("lighthouse", "Lighthouse", "calm", "A slow tilt from level to straight up and back, beams breathing",
       ["#9ad8ff", "#ffffff", "#cdeaff", "#ffffff", "#9ad8ff"],
       move={"mode": "sweep", "target": "ceiling", "sweep": 45, "period": 4, "speed": 0.4, "pair": "mirror"},
       beams={"pattern": "solid", "color": "a", "level": 0.8, "pulse": "breathe", "depth": 0.5}),
    _p("breathe", "Breathe", "calm", "Curtains on the ceiling, a soft gradient fading in and out over 2 bars",
       ["#00c2ff", "#7b2cff", "#66daff", "#b47dff", "#ffffff"],
       move={"mode": "aim", "target": "ceiling", "speed": 0.4},
       beams={"pattern": "solid", "color": "ab", "level": 0.7, "pulse": "breathe", "depth": 0.8, "b": "mirror"},
       strip={"pattern": "solid", "color": "ab", "level": 0.15, "pulse": "breathe", "depth": 0.8}),
    _p("shimmer", "Shimmer", "calm", "Every beam and segment drifts through the palette at its own brightness",
       ["#ff5fa2", "#ffb86b", "#9b8cff", "#5ce1ff", "#fff3c4"],
       move={"mode": "aim", "target": "ceiling", "speed": 0.4, "jitter": 1, "jitter_rate": "beat"},
       beams={"pattern": "shimmer", "color": "random", "rate": 3, "floor": 0.05},
       strip={"pattern": "shimmer", "color": "random", "rate": 2.5, "level": 0.6, "floor": 0.05}),
    _p("rainbowwaves", "Rainbow waves", "calm", "Slow rainbow waves roll along both bars, A into B",
       ["#ff0000", "#00ff40", "#3000ff", "#ffe600", "#ff00c8"],
       move=dict(CROWD, speed=0.4),
       strip={"pattern": "wave", "color": "rainbow", "rate": 16, "width": 1.0, "level": 0.85, "floor": 0.15, "b": "offset"},
       beams={"pattern": "wave", "color": "rainbow", "rate": 16, "width": 1.0, "level": 0.35, "floor": 0.2, "b": "offset"}),
    # peak
    _p("updown", "Side to side", "peak", "A band bounces from end to end every 2 beats, mirrored, with white 8th hits",
       ["#ff0040", "#00b3ff", "#ff6688", "#66d1ff", "#ffffff"],
       move=dict(CROWD),
       strip={"pattern": "bounce", "rate": 2, "width": 0.14, "b": "mirror"},
       white={"pattern": "bounce", "rate": 2, "width": 0.13, "b": "mirror", "pulse": "8th", "decay": 9}),
    _p("midout", "Mid out", "peak", "Rings burst from the middle on every beat (inwards on odd bars)",
       ["#ffe600", "#ff00a8", "#fff066", "#ff66cb", "#ffffff"],
       move=dict(CROWD),
       strip={"pattern": "midout", "rate": 1, "width": 0.3, "flip_bars": True},
       white={"pattern": "midout", "rate": 1, "width": 0.3, "flip_bars": True, "pulse": "beat", "decay": 10}),
    _p("rainbow", "Rainbow", "peak", "Colour gradient scrolls along the bar once per bar; whites strobe on the off-beats",
       ["#ff0000", "#00ff40", "#3000ff", "#ffe600", "#ff00c8"],
       move=dict(CROWD),
       strip={"pattern": "solid", "color": "scroll", "rate": 4, "level": 0.9, "b": "offset"},
       white={"pattern": "alternate", "rate": 1, "pulse": "offbeat", "decay": 12}),
    _p("spiral", "Spiral", "peak", "Bars roll over and over while a band runs along them (beams stay off while spinning)",
       ["#00f0ff", "#ff3df5", "#7af6ff", "#ff8cf9", "#ffffff"],
       move={"mode": "spin", "spin": 0.35, "pair": "mirror"},
       strip={"pattern": "chase", "rate": 2, "width": 0.16, "b": "offset"},
       white={"pattern": "solid", "pulse": "one", "decay": 8}),
    _p("fill", "Fill + flash", "peak", "The bar fills from one end over each bar, then everything flashes on the 1",
       ["#ff5e00", "#00ffd0", "#ff9a5c", "#66ffe3", "#ffffff"],
       move=dict(CROWD),
       strip={"pattern": "fill", "rate": 4, "floor": 0.05},
       white={"pattern": "solid", "pulse": "one", "decay": 5}),
    _p("sparkle", "Sparkle", "peak", "Random white segments pop on every 16th over a dim gradient",
       ["#8000ff", "#00ffaa", "#b366ff", "#66ffcc", "#ffffff"],
       move=dict(CROWD),
       strip={"pattern": "solid", "color": "ab", "level": 0.18},
       white={"pattern": "sparkle", "rate": 0.25, "count": 2, "pulse": "16th", "decay": 10}),
    _p("strobewall", "Strobe wall", "peak", "LED side to the floor: every white on 16ths over a pulsing colour wash",
       ["#ff0040", "#0060ff", "#ff6688", "#66a0ff", "#ffffff"],
       move=dict(CROWD),
       strip={"pattern": "solid", "level": 0.35, "pulse": "beat", "decay": 4, "depth": 0.6},
       white={"pattern": "solid", "pulse": "16th", "decay": 14}),
    _p("storm", "Ball storm", "peak", "Both curtains hop across the balls on 8ths with 16th flashes and white sparks",
       ["#ffffff", "#ff2bd6", "#ffc2f2", "#ffffff", "#ff2bd6"],
       move={"mode": "hop", "hops": 4, "hop_every": 0.5, "jitter": 1, "pair": "mirror"},
       beams={"pattern": "solid", "pulse": "16th", "decay": 8, "depth": 0.6},
       white={"pattern": "sparkle", "rate": 0.25, "count": 1, "pulse": "16th", "decay": 10}),
    # ambient (seconds, not beats)
    _p("rain", "Light rain", "ambient", "Grey-blue drizzle running down the LED side, a faint glow on the ceiling",
       ["#5b8cff", "#9fc3ff", "#2a4a9a", "#c8dcff", "#e8f0ff"], clock="seconds",
       move={"mode": "aim", "target": "ceiling", "speed": 0.3},
       beams={"pattern": "solid", "color": "c", "level": 0.12, "pulse": "breathe", "depth": 0.4},
       strip={"pattern": "rain", "color": "spread", "rate": 1.6, "density": 0.35, "decay": 5, "level": 0.7, "floor": 0.06},
       white={"pattern": "rain", "rate": 2.5, "density": 0.08, "decay": 9, "level": 0.25}),
    _p("downpour", "Downpour", "ambient", "Heavy, fast rain across both bars",
       ["#4a7dff", "#8ab4ff", "#1e3a8a", "#bcd4ff", "#ffffff"], clock="seconds",
       move={"mode": "aim", "target": "ceiling", "speed": 0.3},
       beams={"pattern": "solid", "color": "c", "level": 0.08},
       strip={"pattern": "rain", "color": "spread", "rate": 0.9, "density": 0.7, "decay": 6, "level": 0.85, "floor": 0.05},
       white={"pattern": "rain", "rate": 1.2, "density": 0.2, "decay": 10, "level": 0.35}),
    _p("thunder", "Thunderstorm", "ambient", "Rain in the dark, then lightning: a few strikes a minute, beams and all",
       ["#2d3fbf", "#6a7dff", "#141f66", "#a9b4ff", "#ffffff"], clock="seconds",
       move={"mode": "aim", "target": "ceiling", "speed": 0.3},
       beams={"pattern": "solid", "color": "c", "level": 0.06},
       strip={"pattern": "rain", "color": "spread", "rate": 1.1, "density": 0.5, "decay": 6, "level": 0.6, "floor": 0.04},
       fx={"lightning": 5, "lightning_level": 1.0, "lightning_beams": True}),
    _p("distant", "Distant storm", "ambient", "Indigo calm with faint lightning far away",
       ["#3b2a8f", "#6b4bd6", "#1c1550", "#9f8cff", "#ffffff"], clock="seconds",
       move={"mode": "aim", "target": "corner", "speed": 0.3},
       strip={"pattern": "solid", "color": "spread", "level": 0.18, "pulse": "breathe", "depth": 0.5},
       beams={"pattern": "solid", "color": "c", "level": 0.08, "pulse": "breathe", "depth": 0.5},
       fx={"lightning": 2, "lightning_level": 0.5}),
    _p("halloween", "Halloween", "ambient", "Pumpkin orange and witchy purple rolling past, candle flicker, the odd crack of lightning",
       ["#ff6a00", "#8a2be2", "#ff9e3d", "#5a189a", "#ffd166"], clock="seconds",
       move={"mode": "sweep", "target": "ceiling", "sweep": 20, "period": 6, "speed": 0.3, "pair": "mirror"},
       beams={"pattern": "solid", "color": "own", "level": 0.4, "pulse": "breathe", "depth": 0.5},
       strip={"pattern": "wave", "color": "spread", "rate": 12, "width": 0.8, "level": 0.8, "floor": 0.25, "b": "offset"},
       white={"pattern": "off"},
       fx={"lightning": 1.2, "lightning_level": 0.9, "lightning_beams": True}),
    _p("campfire", "Campfire", "ambient", "Embers and flames flickering along the LED side, a warm glow above",
       ["#ff3c00", "#ff7a00", "#ffb000", "#c21800", "#ffd36b"], clock="seconds",
       move=dict(CROWD, speed=0.3),
       strip={"pattern": "flicker", "color": "random", "rate": 1.5, "level": 0.9, "floor": 0.15},
       beams={"pattern": "flicker", "color": "a", "rate": 3, "level": 0.2}),
    _p("aurora", "Aurora", "ambient", "Green, teal and violet curtains drifting slowly across the ceiling",
       ["#00ff9c", "#00c2ff", "#7b2cff", "#00ffd0", "#c77dff"], clock="seconds",
       move={"mode": "sweep", "target": "ceiling", "sweep": 20, "period": 5, "speed": 0.25, "pair": "mirror"},
       beams={"pattern": "wave", "color": "scroll", "rate": 14, "width": 1.0, "level": 0.4, "floor": 0.1},
       strip={"pattern": "wave", "color": "scroll", "rate": 10, "width": 1.0, "level": 0.7, "floor": 0.1, "b": "offset"}),
    _p("ocean", "Deep ocean", "ambient", "Blue-green swells rolling along, light rippling on the ceiling",
       ["#0047ab", "#00a6a6", "#002a66", "#00d4c8", "#5ab4ff"], clock="seconds",
       move={"mode": "sweep", "target": "ceiling", "sweep": 12, "period": 3, "speed": 0.25},
       beams={"pattern": "wave", "color": "spread", "rate": 7, "width": 0.7, "level": 0.35, "floor": 0.2},
       strip={"pattern": "wave", "color": "spread", "rate": 9, "width": 0.9, "level": 0.6, "floor": 0.2, "b": "offset"}),
    _p("stars", "Starfield", "ambient", "Faint beams twinkle on the ceiling like stars over a deep blue glow",
       ["#ffffff", "#cfe3ff", "#0a1a5c", "#fff4d6", "#8fb8ff"], clock="seconds",
       move={"mode": "aim", "target": "up", "speed": 0.2},
       beams={"pattern": "twinkle", "color": "spread", "rate": 3, "density": 0.4, "level": 0.25},
       strip={"pattern": "solid", "color": "c", "level": 0.06},
       white={"pattern": "twinkle", "rate": 4, "density": 0.05, "level": 0.15}),
    _p("sunset", "Sunset", "ambient", "Orange, pink and purple sliding very slowly along the bars",
       ["#ff7b00", "#ff3d6e", "#9b2fff", "#ffb347", "#ff6fb5"], clock="seconds",
       move={"mode": "aim", "target": "ceiling", "speed": 0.25},
       beams={"pattern": "solid", "color": "scroll", "rate": 40, "level": 0.3},
       strip={"pattern": "solid", "color": "scroll", "rate": 30, "level": 0.8, "b": "offset"}),
    _p("fireflies", "Fireflies", "ambient", "Warm little lights blinking on and off in the dark",
       ["#d4ff4f", "#ffe36b", "#a8ff6b", "#fff2a8", "#d4ff4f"], clock="seconds",
       move=dict(CROWD, speed=0.2),
       strip={"pattern": "twinkle", "color": "random", "rate": 2.5, "density": 0.15, "level": 0.9}),
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
