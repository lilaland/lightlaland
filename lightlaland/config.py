"""Config with defaults, stored as JSON so calibration survives restarts."""
import copy
import json
import os
import threading

VERSION = 4   # 2: bars mounted horizontally (tilt model), beam safety, palettes. 3: no Blahaj.
#               4: no cross point (horizontal curtains can only meet in the ball corner)

FIXTURE = {
    "ip": "broadcast", "universe": 0, "address": 1,   # or the node's IP
    # Where the bar is: on the plan at (x, y) against a wall, facing into the room (plan angle
    # of the wall's inward normal), mounted horizontally at height z (m); length is the beams' span.
    "x": 1.0, "y": 0.0, "facing": 90, "z": 2.44, "length": 1.0, "flip": False,
    # Tilt calibration (Mount tab): up = CH1 that points the beams straight up; dir = +1 if
    # increasing CH1 tilts them toward the room. Beams stay off until this is done.
    "up": 42, "dir": 1, "up_set": False, "calibrated": False,
    # limit = the first CH1 step toward the room where beams would reach people (off limits
    # from there on); ball_span = the CH1 range where the curtain hits the disco balls.
    "limit": None, "ball_span": None,
    # CH2 spin ranges, measured with the webcam probe: "fwd" turns the same way as increasing
    # CH1, "rev" the other way; [slowest, fastest]. 0-8 and ~130-135 = no spin.
    # CH3: 0 is fastest (half a turn in 0.9 s), 255 slowest (6.7 s).
    "spin_fwd": [140, 255], "spin_rev": [16, 129],
    "speed_fast": 0, "speed_slow": 255,
    "targets": {},  # target id -> CH1 value saved while aiming on the Mount tab
}

DEFAULT = {
    "version": VERSION,
    "server": {"host": "0.0.0.0", "port": 8080},
    "output": {"fps": 40, "enabled": True},
    # Room in metres, seen from above: x to the right, y towards the front (stairs side);
    # h is the ceiling height.
    "room": {"w": 5.6, "d": 4.6, "h": 2.8},
    "fixtures": [
        dict(FIXTURE, id="A", name="Left (yellow cabinet)", universe=0, x=1.0, y=0.0, facing=90),
        dict(FIXTURE, id="B", name="Right (bookshelf)", universe=1, x=5.6, y=2.0, facing=180),
    ],
    # Beams may only be lit between these elevations (0 = level, 90 = up, 180 = level into the
    # wall, 270 = straight down the wall side). Never below level on the room side unless you
    # change it, and only after the tilt is calibrated. Each fixture's "limit" can narrow it.
    "safety": {"on": True, "low": 0, "high": 180},
    "points": [
        # The disco balls hang as one cluster: a rectangle on the plan (centre x, y; size w, h)
        # at height z.
        {"id": "balls", "kind": "cluster", "label": "Balls", "x": 2.8, "y": 2.6, "w": 1.6, "h": 0.8, "z": 2.55},
    ],
    "audio": {
        "device": None,          # None = system default input
        "bpm_min": 115, "bpm_max": 150,
        "sync_ms": 40,           # lookahead so lights land on the beat (audio + DMX latency)
        "phrase_bars": 16,
        "follow_audio": True,    # False = manual tempo (tap / nudge)
        "song_detect": True,
    },
    "show": {
        "auto": True, "intensity": 1.0, "speed": 1.0,
        "sweep_width": 40,       # degrees either side of a sweep's centre (scenes with sweep 0)
        # Live palette: when on, every scene uses these five colours instead of its own.
        "palette": {"on": False, "colors": ["#ff1f5a", "#ff6a3d", "#ffb000", "#00e1ff", "#7b2cff"]},
        "ambient_on_silence": True,   # when the music stops, auto mode plays an ambient scene
        "smooth": True,          # motor speed follows the move, so slow tilts glide
    },
    "rotation": {},   # scene name -> bool, whether auto mode may pick it
    "scenes": [],     # scene specs saved from the scene maker (override presets by name)
}


def _lighten(hexc, k=0.45):
    r, g, b = (int(hexc[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % tuple(int(v + (255 - v) * k) for v in (r, g, b))


def _migrate(data):
    """Bring a config saved by an older version up to date (before defaults are merged in)."""
    _migrate_v2(data)
    _migrate_v3(data)
    _migrate_v4(data)


def _migrate_v4(data):
    """Version 4: no cross point. Scenes that used it aim at the balls instead."""
    if data.get("version", 1) >= 4:
        return
    data["points"] = [p for p in data.get("points", []) if p.get("kind") != "cross"]
    for fx in data.get("fixtures", []):
        fx.get("targets", {}).pop("cross", None)
    data["version"] = 4


def _migrate_v2(data):
    if data.get("version", 1) >= 2:
        return
    # Version 1 assumed vertically mounted bars: its aim calibration means nothing now.
    for fx in data.get("fixtures", []):
        for old in ("spin_cw", "spin_ccw", "home", "cw", "nogo"):
            fx.pop(old, None)
        fx["calibrated"] = False
        fx["targets"] = {}
    for key in ("palettes",):
        data.pop(key, None)
    show = data.get("show", {})
    show.pop("jitter_steps", None)
    show.pop("sweep_width", None)
    old = show.pop("colors", None)
    if old:
        a, b = old.get("a", "#ff1f5a"), old.get("b", "#00e1ff")
        show["palette"] = {"on": bool(old.get("on")), "colors": [a, b, _lighten(a), _lighten(b), "#ffffff"]}
    pts = data.get("points")
    if pts is not None:
        balls = [p for p in pts if p.get("kind") == "ball"]
        if balls and not any(p.get("kind") == "cluster" for p in pts):
            xs, ys = [p["x"] for p in balls], [p["y"] for p in balls]
            pts = [{"id": "balls", "kind": "cluster", "label": "Balls",
                    "x": round((min(xs) + max(xs)) / 2, 2), "y": round((min(ys) + max(ys)) / 2, 2),
                    "w": round(max(0.6, max(xs) - min(xs) + 0.6), 2),
                    "h": round(max(0.6, max(ys) - min(ys) + 0.6), 2)}] + [p for p in pts if p.get("kind") != "ball"]
        pts = [p for p in pts if p.get("kind") in ("cluster", "cross")]   # corner is a computed target now
        for p in pts:
            p.setdefault("z", 2.55 if p["kind"] == "cluster" else 2.6)
        data["points"] = pts
    data["version"] = 2


def _migrate_v3(data):
    """Version 3: the Blahaj target is gone (the user asked to remove it)."""
    if data.get("version", 1) >= 3:
        return
    gone = {p["id"] for p in data.get("points", []) if p.get("kind") == "spot"}
    data["points"] = [p for p in data.get("points", []) if p["id"] not in gone]
    for fx in data.get("fixtures", []):
        for pid in gone:
            fx.get("targets", {}).pop(pid, None)
    data["version"] = 3


def _merge(base, extra):
    """Recursively overlay `extra` onto a copy of `base` (lists are replaced, not merged)."""
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    """Thread-safe dict wrapper with atomic JSON saves."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULT)
        if os.path.exists(path):
            with open(path) as f:
                saved = json.load(f)
            _migrate(saved)
            self.data = _merge(DEFAULT, saved)
            # Fixtures saved by an older version may lack newer keys.
            self.data["fixtures"] = [_merge(FIXTURE, fx) for fx in self.data["fixtures"]]

    def __getitem__(self, key):
        return self.data[key]

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
