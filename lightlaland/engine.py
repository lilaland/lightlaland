"""The show loop: clock -> director -> scene -> overlays -> beam safety -> DMX -> Art-Net,
40 times a second."""
import collections
import json
import math
import queue
import random
import threading
import time

from . import geometry
from .fixture import N_BEAMS, N_WHITE, Frame, encode, speed_value
from .scenes import Ctx, hex_rgb, library, rgb_hex

DROP_BARS = 8          # peak section length after a drop
MAX_BREAK_BARS = 32    # give up waiting for a drop after this long
SPIN_SETTLE_MIN = 8.0  # after a spin, at least this long: the head may unwind turns we can't see
CALIB_DIM = 0.06       # Mount tab: beams that point somewhere unsafe are shown this dim
BEAM_CHANNELS = range(3, 27)   # 0-based CH4-27, for the channel test
EVENT_TEXT = {"breakdown": "Breakdown", "drop": "Drop", "song": "New record?", "silence": "Silence",
              "groove": "Back to groove", "music": "Music back", "ambient": "Ambient"}
GROUP_FOR_STATE = {"groove": "groove", "breakdown": "calm", "drop": "peak", "ambient": "ambient"}


class Director:
    """Auto mode: breakdown -> calm scene, drop -> peak scene, new phrase -> next groove scene,
    silence -> an ambient scene (and back when the music returns)."""

    def __init__(self, engine):
        self.engine = engine
        self.state = "groove"   # groove | breakdown | drop | ambient
        self.since = 0          # beat index the state started
        self.ambient_auto = False   # ambient because the music stopped (so leave when it's back)
        self.last_phrase = None
        self.rng = random.Random()

    def pick(self, group):
        """A random scene from the chosen set: from `group` if the set has any there, else any
        of the set's scenes (so the window set never plays a scene made for the room)."""
        cfg = self.engine.app.config
        scenes = list(self.engine.library.values())
        pool = [s for s in scenes if s.set == cfg["show"]["set"]] or scenes
        pool = [s for s in pool if s.group == group] or pool
        names = [s.name for s in pool if cfg["rotation"].get(s.name, True)] or [s.name for s in pool]
        options = [n for n in names if n != self.engine.scene] or names
        return self.rng.choice(options)

    def switch(self, group):
        if self.engine.app.config["show"]["auto"]:
            self.engine.set_scene(self.pick(group), by="auto")

    def choose(self, group, ev):
        """A named scene or a random pick (Perform pads), else auto mode's choice."""
        if ev.get("scene"):
            self.engine.set_scene(ev["scene"])
        elif ev.get("pick"):
            self.engine.set_scene(self.pick(group))
        else:
            self.switch(group)

    def on_event(self, ev):
        kind = ev["type"]
        forced = ev.get("scene") or ev.get("pick")
        if kind != "silence" and forced:
            self.ambient_auto = False
        if kind == "breakdown" and (self.state != "breakdown" or forced):
            self.state, self.since = "breakdown", ev["beat"]
            self.choose("calm", ev)
        elif kind == "drop":
            self.state, self.since = "drop", ev["beat"]
            self.choose("peak", ev)
        elif kind == "song":
            self.engine.color_swap = not self.engine.color_swap
            if self.state == "groove":
                self.switch("groove")
        elif kind == "groove" and (self.state != "groove" or forced):   # Break pad pressed again
            self.state, self.since = "groove", ev["beat"]
            self.choose("groove", ev)
        elif kind == "ambient":
            self.state, self.since = "ambient", ev["beat"]
            self.choose("ambient", ev)
        elif kind == "silence":
            show = self.engine.app.config["show"]
            if show["auto"] and show["ambient_on_silence"] and self.state != "ambient":
                self.state, self.since, self.ambient_auto = "ambient", ev["beat"], True
                self.switch("ambient")
        elif kind == "music" and self.state == "ambient" and self.ambient_auto:
            self.state, self.since, self.ambient_auto = "groove", ev["beat"], False
            self.switch("groove")

    def tick(self, info):
        n = info["beat_index"]
        if self.state == "drop" and n - self.since >= DROP_BARS * 4:
            self.state, self.since = "groove", n
            self.switch("groove")
        elif self.state == "breakdown" and n - self.since > MAX_BREAK_BARS * 4:
            self.state, self.since = "groove", n
            self.switch("groove")
        key = (info["anchor"], info["phrase"])
        last = self.last_phrase
        if last and key[0] == last[0] and key[1] == last[1] + 1 and self.state == "groove":
            self.switch("groove")   # a new phrase on an unchanged grid
        self.last_phrase = key


class BeamGuard:
    """Keeps the beams dark unless they point inside the safe window.

    It tracks the *range* of CH1 positions the head could physically be at. Every frame
    both ends move toward the position we're asking for, at no more than the head's slowest
    plausible speed, and the range always includes that target (the head could be faster).
    Beams light only if the target and the whole range are inside one copy of the safe arc:
    each copy is one contiguous CH1 range, so a head travelling between two points in it
    never swings the beams below level. Start-up and spins make the range all of 0..255;
    after a spin it is held there a while (SPIN_SETTLE_MIN, or three slow trips across),
    since the fixture may unwind turns we can't see.
    """

    def __init__(self, n, now):
        self.lo, self.hi = [0.0] * n, [255.0] * n
        self.hold_until = [now] * n

    def est(self, i):
        return (self.lo[i] + self.hi[i]) / 2

    def check(self, i, fx, f, now, dt, safety, windows):
        sps = geometry.steps_per_second(speed_value(f.speed, fx))
        target = float(round(f.pos))
        if f.spin:
            self.lo[i], self.hi[i] = 0.0, 255.0
            self.hold_until[i] = now + max(SPIN_SETTLE_MIN, 3 * 255.0 / sps)
            return False, "spinning"
        if now >= self.hold_until[i]:
            step = sps * dt
            lo = min(self.lo[i] + step, target) if self.lo[i] < target else target
            hi = max(self.hi[i] - step, target) if self.hi[i] > target else target
            self.lo[i], self.hi[i] = lo, hi
        else:
            self.lo[i], self.hi[i] = 0.0, 255.0
        if not safety["on"]:
            return True, "safety off"
        if not fx["calibrated"]:
            return False, "not calibrated"
        if not windows:
            return False, "no safe range"
        win = geometry.window_of([(a + 0.5, b - 0.5) for a, b in windows], target)   # DMX rounds
        if win is None:
            return False, "pointing low"
        if not (win[0] <= self.lo[i] and self.hi[i] <= win[1]):
            return False, "moving" if self.hi[i] - self.lo[i] < 250 else "settling"
        return True, ""


def _copy_cfg(config):
    """Small per-frame snapshot so the UI can edit config while the show runs."""
    with config.lock:
        d = config.data
        show = dict(d["show"])
        show["palette"] = dict(show["palette"], colors=list(show["palette"]["colors"]))
        return {
            "fixtures": [dict(fx, targets=dict(fx["targets"])) for fx in d["fixtures"]],
            "points": [dict(p) for p in d["points"]],
            "room": dict(d["room"]),
            "safety": dict(d["safety"]),
            "show": show,
            "sync": d["audio"]["sync_ms"] / 1000.0,
            "output": d["output"]["enabled"],
        }


class Engine:
    def __init__(self, app):
        self.app = app
        fixtures = app.config["fixtures"]
        self.library = library(app.config["scenes"])   # name -> Scene
        self.scene = "cross"
        self.preview = None          # a Scene from the scene maker, played instead while editing
        self.preview_until = 0.0
        self.director = Director(self)
        self.events = queue.SimpleQueue()          # from the analyzer thread and the pads
        self.log = collections.deque(maxlen=8)     # recent happenings, for the UI
        self.t0 = time.perf_counter()
        self.prev = [Frame(pos=fx["up"]) for fx in fixtures]
        self.guard = BeamGuard(len(fixtures), self.t0)
        self.spin_angle = [90.0] * len(fixtures)   # cosmetic, for the visualizer while spinning
        self.move_rate = [0.0] * len(fixtures)     # how fast each head is being asked to move (steps/s)
        self.strobe = False
        self.blackout = False
        self.color_swap = False
        self.drop_beat = None
        self.calib = None   # {"fixture": i, "pos": ch1}
        self.test = None    # {"fixture": i, "pos": ch1, "channels": {index: value}}
        self.vis = {}
        self._vis_json = (None, b"{}")
        self.fps = 0.0
        self.running = False

    # --- control (server threads) ---------------------------------------------------------
    def set_scene(self, name, by="you"):
        if name in self.library:
            self.scene = name
            if by == "you":
                self.preview = None
            self.note(self.library[name].label, by)

    def reload_scenes(self):
        """After the scene maker saves or deletes: rebuild the library from the config."""
        self.library = library(self.app.config["scenes"])
        if self.scene not in self.library:
            self.scene = next(iter(self.library))

    def set_preview(self, scene, seconds=120.0):   # the scene maker refreshes it while open
        self.preview = scene
        self.preview_until = time.perf_counter() + seconds

    def current(self):
        if self.preview is not None and time.perf_counter() < self.preview_until:
            return self.preview
        self.preview = None
        return self.library.get(self.scene) or next(iter(self.library.values()))

    def note(self, text, by="auto"):
        self.log.appendleft({"text": text, "by": by, "t": time.perf_counter()})

    def vis_json(self):
        """The latest visual state as JSON, encoded once however many pages are watching."""
        vis, cached = self.vis, self._vis_json
        if cached[0] is not vis:
            cached = self._vis_json = (vis, json.dumps(vis, separators=(",", ":")).encode())
        return cached[1]

    def push_event(self, ev):
        self.events.put(ev)

    # --- loop -------------------------------------------------------------------------------
    def start(self):
        self.running = True
        threading.Thread(target=self._run, daemon=True, name="engine").start()

    def stop(self):
        self.running = False

    def _run(self):
        period = 1.0 / self.app.config["output"]["fps"]
        nxt = last = time.perf_counter()
        while self.running:
            now = time.perf_counter()
            dt = now - last
            last = now
            if dt > 0:
                self.fps = 0.95 * self.fps + 0.05 / dt
            try:
                self.step(now, dt)
            except Exception as exc:  # keep the lights running whatever happens
                self.note(f"engine error: {exc!r}", "error")
            nxt += period
            delay = nxt - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.perf_counter()

    def step(self, now, dt):
        cfg = _copy_cfg(self.app.config)
        fixtures, show, safety = cfg["fixtures"], cfg["show"], cfg["safety"]
        info = self.app.clock.info(now + cfg["sync"])
        windows = [geometry.safe_windows(fx, *geometry.safe_range(fx, safety)) for fx in fixtures]

        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            self.director.on_event(ev)
            if ev["type"] == "drop" and ev.get("flash", True):
                # Flash on the drop beat; if the event arrived late (detection takes ~0.1 s),
                # start the flash now rather than showing only its tail.
                self.drop_beat = max(ev["beat"], info["beat"])
            self.note(EVENT_TEXT.get(ev["type"], ev["type"]), ev.get("by", "music"))
        self.director.tick(info)

        an = self.app.analyzer()
        build = an.build if (an is not None and self.director.state == "breakdown") else 0.0

        if self.test is not None:
            frames = [Frame(pos=self.test["pos"] if i == self.test["fixture"] else self.prev[i].pos)
                      for i in range(len(fixtures))]
        elif self.calib is not None:
            frames = self._calib_frames(fixtures)
        else:
            scene = self.current()
            palette = self._palette(scene, show["palette"])
            ctx = Ctx(cfg, info, palette, self.prev, speed=show["speed"], secs=now - self.t0, windows=windows)
            frames = scene.render(ctx)
            if show.get("smooth", True):
                self._smooth(frames, fixtures, dt)
            self._overlays(frames, ctx, info, show, build)

        safe = []
        for i, (f, fx) in enumerate(zip(frames, fixtures)):
            ok, why = self.guard.check(i, fx, f, now, dt, safety, windows[i])
            safe.append((ok, why))
            if not ok:
                if self.calib is not None and i == self.calib["fixture"]:
                    f.beams = [(0.0, 0.0, 0.0, min(CALIB_DIM, b[3])) for b in f.beams]
                else:
                    f.beams = [(0.0, 0.0, 0.0, 0.0)] * N_BEAMS

        if cfg["output"]:
            self._output(frames, fixtures, safe)
        self.prev = frames
        self._publish(now, dt, info, frames, fixtures, show, build, safe, safety)

    def _palette(self, scene, live):
        pal = [hex_rgb(c) for c in (live["colors"] if live.get("on") else scene.palette)]
        if self.color_swap and len(pal) > 1:
            pal[0], pal[1] = pal[1], pal[0]
        return pal

    def _smooth(self, frames, fixtures, dt):
        """Vector speed: run the motor only as fast as the move needs. CH1 has 2.1 degree steps,
        so at full speed a slow tilt is a jump and a pause on every step; matched to the move
        it glides. Hops and jitter still go at full speed (capped by the scene's own speed)."""
        for i, (f, fx) in enumerate(zip(frames, fixtures)):
            last = self.prev[i].pos if self.prev else None
            if f.spin or last is None or dt <= 0:
                self.move_rate[i] = 0.0
                continue
            asked = abs(f.pos - last) / dt
            self.move_rate[i] = max(asked, self.move_rate[i] * math.exp(-dt / 0.5))
            f.speed = min(f.speed, geometry.motor_speed_for(self.move_rate[i] * 1.5 + 1.0, fx))

    def _overlays(self, frames, ctx, info, show, build):
        # Build-up during a breakdown: whites strobe faster as the riser/snare roll grows.
        if build > 0.1:
            sub = 1 if build < 0.4 else 2 if build < 0.7 else 4
            hit = math.exp(-10 * ((ctx.rb * sub) % 1)) * build
            for f in frames:
                f.white = [max(w, hit) for w in f.white]
        # One beat of everything-on at the drop.
        if self.drop_beat is not None:
            since = info["beat"] - self.drop_beat
            if 0 <= since < 1:
                hit = math.exp(-3 * since)
                for f in frames:
                    f.white = [max(w, hit) for w in f.white]
                    f.beams = [(r, g, b, max(w, hit)) for r, g, b, w in f.beams]
        if self.strobe:  # held pad: hard white 16ths on both sides
            on = 1.0 if (info["beat"] * 4) % 1 < 0.35 else 0.0
            for f in frames:
                f.white = [on] * N_WHITE
                f.beams = [(0.0, 0.0, 0.0, on)] * N_BEAMS
        k = show["intensity"]
        for i, f in enumerate(frames):
            if self.blackout:
                frames[i] = f.dark()
            elif k < 0.999:
                f.beams = [tuple(v * k for v in b) for b in f.beams]
                f.strip = [tuple(v * k for v in s) for s in f.strip]
                f.white = [w * k for w in f.white]

    def _calib_frames(self, fixtures):
        frames = []
        for i in range(len(fixtures)):
            f = Frame(pos=self.prev[i].pos, speed=0.7)
            if i == self.calib["fixture"]:
                f.pos = self.calib["pos"]
                f.beams = [(0.0, 0.0, 0.0, 0.5)] * N_BEAMS   # plain white for aiming
                f.strip = [(0.0, 0.0, 0.25)] * len(f.strip)    # and a blue LED side, to tell them apart
            frames.append(f)
        return frames

    def _output(self, frames, fixtures, safe):
        universes = {}
        for i, (f, fx) in enumerate(zip(frames, fixtures)):
            ch = encode(f, fx)
            if self.test is not None and i == self.test["fixture"]:
                for idx, val in self.test["channels"].items():
                    if 0 <= idx < 83 and (safe[i][0] or idx not in BEAM_CHANNELS):   # never reset
                        ch[idx] = val
            slot = universes.setdefault((fx["ip"], int(fx["universe"])), [bytearray(512), 0])
            start = min(511, max(0, int(fx["address"]) - 1))
            slot[0][start:start + len(ch)] = ch[:512 - start]
            slot[1] = max(slot[1], min(512, start + len(ch)))   # highest channel in use
        for (ip, uni), (buf, used) in universes.items():
            self.app.sender.send(ip, uni, buf, used)

    def _publish(self, now, dt, info, frames, fixtures, show, build, safe, safety):
        fx_vis = []
        for i, (f, fx) in enumerate(zip(frames, fixtures)):
            g = self.guard
            if f.spin:
                self.spin_angle[i] = (self.spin_angle[i] + f.spin * 600.0 * dt) % 360.0
                shown = self.spin_angle[i]
            else:
                shown = geometry.elevation(fx, g.est(i))
                self.spin_angle[i] = shown
            fx_vis.append({
                "pos": int(round(f.pos)), "spin": f.spin,
                "elev": round(geometry.elevation(fx, f.pos), 1),   # where we asked the beams to point
                "shown": round(shown, 1),                           # where we think they point
                "spread": round(g.hi[i] - g.lo[i], 1),              # how unsure that is (CH1 steps)
                "safe": safe[i][0], "why": safe[i][1], "calibrated": fx["calibrated"],
                "beams": [rgb_hex((r + w, g + w, b + w)) for r, g, b, w in f.beams],
                "strip": [rgb_hex(s) for s in f.strip],
                "white": [round(w, 3) for w in f.white],
            })
        an = self.app.analyzer()
        src = self.app.source
        audio = an.status() if an else {"state": "off", "locked": False, "level_db": -120.0,
                                        "kick": 0.0, "build": 0.0, "bpm_est": None}
        audio.update(name=getattr(src, "name", None), error=getattr(src, "error", None))
        self.vis = {
            "clock": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in info.items()},
            "follow": self.app.clock.follow_audio,
            "audio": audio,
            "scene": self.scene, "auto": show["auto"], "state": self.director.state,
            "preview": self.preview is not None, "palette": show["palette"], "safety": safety,
            "strobe": self.strobe, "blackout": self.blackout, "intensity": show["intensity"],
            "speed": show["speed"], "set": show["set"], "strobe_freq": show["strobe_freq"],
            "swap": self.color_swap, "build": round(build, 3),
            "fixtures": fx_vis,
            "log": [{"text": e["text"], "by": e["by"], "ago": round(now - e["t"], 1)} for e in self.log],
            "calib": self.calib, "test": self.test is not None,
            "out": {"enabled": self.app.config["output"]["enabled"] and self.app.sender.enabled,
                    "packets": self.app.sender.packets,
                    "errors": {f"{ip}/{u}": e for (ip, u), e in self.app.sender.errors.items()}},
            "fps": round(self.fps, 1),
        }
