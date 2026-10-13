"""Room geometry for bars mounted horizontally on the walls, and beam safety.

Each bar lies along a wall at height z, facing into the room (`facing` is the plan angle of
the wall's inward normal, y pointing down the screen). The head turns about the bar's long
axis, so the 12 beams (a curtain spaced along the bar) tilt in the vertical plane at right
angles to the wall. Angles here are beam *elevations* in that plane:

    0 = level, straight out into the room     90 = straight up
    180 = level, into the wall                -90 = straight down

CH1 covers 540 degrees in 255 steps, so one turn is 170 steps (measured with the webcam
probe). Each fixture is calibrated with "up" (the CH1 value that points the beams straight
up) and "dir" (+1 if increasing CH1 tilts them toward the room, -1 toward the wall).

Beam safety: beams may only be lit while they point between the lowest and highest allowed
elevation (by default level, through straight up, to level into the wall; never below level).
Each copy of that arc is one contiguous CH1 range, and safe positions are placed inside the
copy the head is already in, so moving between them never swings the beams through the people
below.
"""
import math

DEG_PER_STEP = 540.0 / 255.0
STEPS_PER_TURN = 360.0 / DEG_PER_STEP
# Measured on light A: CH3 value -> seconds for half a turn (85 steps).
HALF_TURN_S = [(0, 0.86), (128, 1.2), (192, 1.92), (224, 3.62), (255, 6.73)]
SLOW_MARGIN = 1.25   # assume the head is 25% slower than measured when guessing where it is


def wrap180(a):
    return (a + 180.0) % 360.0 - 180.0


def elevation(fx, ch1):
    """Beam elevation (degrees, -180..180) at a CH1 value."""
    return wrap180(90.0 - fx["dir"] * (ch1 - fx["up"]) * DEG_PER_STEP)


def raw_ch1(fx, elev):
    """A CH1 value (maybe outside 0..255) that points the beams at `elev` degrees."""
    return fx["up"] + fx["dir"] * (90.0 - elev) / DEG_PER_STEP


def equivalents(ch1):
    """All CH1 values in range that point the same way as `ch1` (one per turn)."""
    return [c for c in (ch1 + k * STEPS_PER_TURN for k in range(-3, 4)) if -0.5 <= c <= 255.5]


def safe_range(fx, safety):
    """A fixture's allowed beam elevations: the global range, narrowed by its own `limit`
    (the first CH1 step, tilting toward the room, where the beams would reach people; that
    step itself is off limits)."""
    low, high = safety["low"], safety["high"]
    if fx.get("limit") is not None:
        low = max(low, elevation(fx, fx["limit"] - 0.5 * fx["dir"]))
    return low, high


def in_arc(elev, low, high):
    """Is an elevation inside the arc low..high? The arc may run past 180 (e.g. 4..270: from
    just above level, over the top, down the wall side to straight down)."""
    return (elev - low) % 360.0 <= high - low


def clamp_to_arc(elev, low, high):
    """The elevation itself if it's inside low..high, else the nearer end of the arc."""
    if in_arc(elev, low, high):
        return low + (elev - low) % 360.0
    return low if (low - elev) % 360.0 <= (elev - high) % 360.0 else high


def safe_windows(fx, low, high):
    """Every copy of the safe arc (beams between `low` and `high` degrees) inside 0..255,
    as CH1 intervals (lo, hi). CH1 covers 1.5 turns, so there are usually two. Each copy is
    contiguous: a head moving between two points in the same copy stays in it."""
    a, b = raw_ch1(fx, low), raw_ch1(fx, high)
    lo, hi = min(a, b), max(a, b)
    out = []
    for k in range(-3, 4):
        l, h = max(0.0, lo + k * STEPS_PER_TURN), min(255.0, hi + k * STEPS_PER_TURN)
        if h > l:
            out.append((l, h))
    return out


def window_of(windows, ch1):
    """The copy of the safe arc that contains a CH1 value, or None."""
    return next((w for w in windows if w[0] <= ch1 <= w[1]), None)


def place(fx, elev, windows, near):
    """CH1 for a beam elevation, staying in the copy of the safe arc the head is already in
    (or the largest copy), so the head never goes the long way round:
    - a safe elevation lands inside that copy (at its edge if the copy is cut off by 0..255);
    - an unsafe one (aiming the LED side) lands on the copy of it nearest that window."""
    cands = equivalents(raw_ch1(fx, elev))
    if not windows:
        return min(cands, key=lambda c: abs(c - near)) if cands else max(0.0, min(255.0, raw_ch1(fx, elev)))
    home = window_of(windows, near) or max(windows, key=lambda w: w[1] - w[0])
    inside = [c for c in cands if home[0] <= c <= home[1]]
    if inside:
        return inside[0]
    gap = lambda c: max(home[0] - c, c - home[1])
    raw = [raw_ch1(fx, elev) + k * STEPS_PER_TURN for k in range(-3, 4)]
    nearest = min(raw, key=gap)
    if gap(nearest) <= 6 and any(w is home for w in windows):   # just past a cut-off end
        return max(home[0], min(home[1], nearest))
    return min(cands, key=gap) if cands else max(0.0, min(255.0, nearest))


def normal(fx):
    a = math.radians(fx["facing"])
    return math.cos(a), math.sin(a)


def to_bar(fx, x, y, z):
    """A room point in the bar's frame: (out from the wall, along the bar, up from the bar)."""
    nx, ny = normal(fx)
    dx, dy = x - fx["x"], y - fx["y"]
    return dx * nx + dy * ny, -dx * ny + dy * nx, z - fx["z"]


def point_elevation(fx, x, y, z):
    """Elevation that tilts the curtain through a point, and whether a beam actually hits it
    (the point has to be in front of the bar, within its length)."""
    out, side, up = to_bar(fx, x, y, z)
    return math.degrees(math.atan2(up, out)), abs(side) <= fx.get("length", 1.0) / 2 + 0.15


def ray_length(x, y, angle, w, d):
    """Distance from (x, y) along a plan `angle` to the room's walls."""
    dx, dy = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    best = 50.0
    for t in (
        (0 - x) / dx if dx < -1e-9 else None,
        (w - x) / dx if dx > 1e-9 else None,
        (0 - y) / dy if dy < -1e-9 else None,
        (d - y) / dy if dy > 1e-9 else None,
    ):
        if t is not None and 0 < t < best:
            best = t
    return best


def depth(fx, room):
    """How far the room reaches straight out in front of the bar."""
    return ray_length(fx["x"], fx["y"], fx["facing"], room["w"], room["d"])


def named_elevation(fx, room, name):
    """Elevations for the targets that aren't points in the room."""
    rise = max(0.05, room["h"] - fx["z"])
    if name == "up":
        return 90.0
    if name == "out":
        return 0.0
    if name == "ceiling":     # the curtain lands on the ceiling halfway across the room
        return math.degrees(math.atan2(rise, depth(fx, room) / 2))
    if name == "corner":      # it grazes the far wall's top edge
        return math.degrees(math.atan2(rise, depth(fx, room)))
    raise KeyError(name)


def motor_speed_for(sps, fx):
    """The motor speed (0..1, as in Frame.speed) that turns the head at about `sps` CH1 steps
    a second, from the measured curve. Clamped to what the motor can do."""
    t = 85.0 / max(sps, 1e-3)
    pts = HALF_TURN_S
    if t <= pts[0][1]:
        ch3 = pts[0][0]
    elif t >= pts[-1][1]:
        ch3 = pts[-1][0]
    else:
        ch3 = next(c0 + (c1 - c0) * (t - t0) / (t1 - t0)
                   for (c0, t0), (c1, t1) in zip(pts, pts[1:]) if t0 <= t <= t1)
    fast, slow = fx.get("speed_fast", 0), fx.get("speed_slow", 255)
    return max(0.0, min(1.0, (ch3 - slow) / (fast - slow))) if fast != slow else 1.0


def steps_per_second(ch3):
    """How fast the head turns at a CH3 value, on the slow side (for the safety guess)."""
    pts = HALF_TURN_S
    if ch3 <= pts[0][0]:
        t = pts[0][1]
    else:
        t = pts[-1][1]
        for (c0, t0), (c1, t1) in zip(pts, pts[1:]):
            if c0 <= ch3 <= c1:
                t = t0 + (t1 - t0) * (ch3 - c0) / (c1 - c0)
                break
    return 85.0 / (t * SLOW_MARGIN)
