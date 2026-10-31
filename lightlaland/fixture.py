"""The 84-channel mode of the 250W double-sided beam curtain / strobe bar.

Measured with the webcam probe (tools/channel_probe.py) on light A, 2026-10-08:

    CH1        position, 0-255 over 540 degrees (one turn = 170 steps)
    CH2        infinite rotation: 0-8 off, ~16-129 one way, ~140-255 the other (slow -> fast)
    CH3        motor speed, 0 = fastest
    CH4-27     6 beam groups x R G B W            (12 beams, in adjacent pairs)
    CH28-51    strip row 1: 8 segments x R G B   (one long edge of the LED face)
    CH52-75    strip row 2: 8 segments x R G B   (the other long edge)
    CH76-83    8 white segments                   (the middle row)
    CH84       reset (always 0)

So the LED face is 8 positions along the bar, each with two RGB segments and a white.
Group/segment 1 is at the same end of the bar for beams, strip and whites. Index 0 of
every list below is that end; set "flip" on the fixture to number from the other end.
The two strip rows can show different colours (Frame.strip2); by default both show `strip`.

The map is plain tables of 1-based channel numbers, so a different layout (or a second
fixture model) only needs new tables.
"""
from dataclasses import dataclass, field

POSITION, SPIN, SPEED, RESET = 1, 2, 3, 84
BEAMS = [tuple(4 + 4 * g + k for k in range(4)) for g in range(6)]            # (R, G, B, W)
STRIP = [tuple(tuple(row + 3 * s + k for k in range(3)) for row in (28, 52))  # ((R, G, B) row 1,
         for s in range(8)]                                                   #  (R, G, B) row 2)
WHITE = [76 + i for i in range(8)]
N_BEAMS, N_STRIP, N_WHITE, N_CH = len(BEAMS), len(STRIP), len(WHITE), 84


@dataclass
class Frame:
    """What one fixture should do this frame, in friendly units (0..1 floats)."""
    pos: float = 127.0      # CH1 value (fractional is fine)
    spin: float = 0.0       # -1..1, non-zero switches to infinite rotation
    speed: float = 1.0      # motor speed, 1 = fastest
    beams: list = field(default_factory=lambda: [(0.0, 0.0, 0.0, 0.0)] * N_BEAMS)
    strip: list = field(default_factory=lambda: [(0.0, 0.0, 0.0)] * N_STRIP)
    white: list = field(default_factory=lambda: [0.0] * N_WHITE)
    strip2: list = None     # the strip's second row, if it differs from the first

    def dark(self):
        return Frame(pos=self.pos, spin=self.spin, speed=self.speed)

    def map_strip(self, fn):
        """Apply fn to every strip segment, in both rows."""
        self.strip = [fn(s) for s in self.strip]
        if self.strip2 is not None:
            self.strip2 = [fn(s) for s in self.strip2]


def to_byte(v):
    return 0 if v <= 0.0 else 255 if v >= 1.0 else int(v * 255.0 + 0.5)


def spin_value(spin, fx):
    """spin > 0 turns clockwise on the plan, like increasing CH1 does when the fixture is "cw"."""
    if abs(spin) < 1e-3:
        return 0
    same_as_ch1 = (spin > 0) == bool(fx.get("cw", True))
    lo, hi = fx["spin_fwd"] if same_as_ch1 else fx["spin_rev"]
    return int(round(lo + (hi - lo) * min(1.0, abs(spin))))


def speed_value(speed, fx):
    """Motor speed 0..1 (1 = fastest) -> the CH3 value."""
    fast, slow = fx.get("speed_fast", 0), fx.get("speed_slow", 255)
    return int(round(slow + (fast - slow) * max(0.0, min(1.0, speed))))


def encode(frame, fx):
    """Frame -> the fixture's 84 DMX channel values."""
    ch = bytearray(N_CH)
    ch[POSITION - 1] = max(0, min(255, int(round(frame.pos))))
    ch[SPIN - 1] = spin_value(frame.spin, fx)
    ch[SPEED - 1] = speed_value(frame.speed, fx)

    flip = fx.get("flip", False)
    order = (lambda xs: xs[::-1]) if flip else (lambda xs: xs)
    beams, white = order(frame.beams), order(frame.white)
    strip = order(frame.strip)
    strip2 = strip if frame.strip2 is None else order(frame.strip2)
    for rgbw, chans in zip(beams, BEAMS):
        for v, c in zip(rgbw, chans):
            ch[c - 1] = to_byte(v)
    for rgb1, rgb2, (row1, row2) in zip(strip, strip2, STRIP):
        for rgb, chans in ((rgb1, row1), (rgb2, row2)):
            for v, c in zip(rgb, chans):
                ch[c - 1] = to_byte(v)
    for w, c in zip(white, WHITE):
        ch[c - 1] = to_byte(w)
    ch[RESET - 1] = 0
    return ch


def _labels():
    labels = {POSITION: "Position", SPIN: "Rotation", SPEED: "Motor speed", RESET: "Reset (don't)"}
    for g, chans in enumerate(BEAMS):
        for c, name in zip(chans, "RGBW"):
            labels[c] = f"Beam group {g + 1} {name}"
    for s, rows in enumerate(STRIP):
        for r, chans in enumerate(rows):
            for c, name in zip(chans, "RGB"):
                labels[c] = f"Strip segment {s + 1} row {r + 1} {name}"
    for i, c in enumerate(WHITE):
        labels[c] = f"White segment {i + 1}"
    return labels


LABELS = _labels()


def describe(channel):
    """Human label for a 1-based channel number (used by the test page)."""
    return LABELS.get(channel, "?")
