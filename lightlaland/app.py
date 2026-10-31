"""Wires everything together and handles commands from the UI."""
import collections
import re
import threading
import time

from . import geometry
from .analyzer import Analyzer
from .artnet import BROADCAST, Sender, discover
from .audio import AudioInput, WavInput
from .clock import BeatClock
from .config import Config
from .engine import GROUP_FOR_STATE, Engine
from .fixture import BEAMS, STRIP, WHITE
from .scenes import GROUPS, SET_NAMES, Scene, normalize, preset, slug, target_elevation, to_range

IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class App:
    def __init__(self, config_path, wav=None, no_audio=False, dry_run=False):
        self.config = Config(config_path)
        a = self.config["audio"]
        self.clock = BeatClock(bpm=124.0, phrase_bars=a["phrase_bars"])
        self.clock.follow_audio = a["follow_audio"]
        self.sender = Sender(enabled=not dry_run)
        self.engine = Engine(self)
        self.source = None
        if wav:
            self.source = WavInput(wav, self._make_analyzer)
        elif not no_audio:
            self.source = AudioInput(self._make_analyzer)
        self._offsets = collections.deque(maxlen=20)
        self._search_lock = threading.Lock()
        self.last_search = {"at": None, "mac": None, "nodes": [], "error": None}

    def search(self, seconds=2.5):
        """Ask every Art-Net node on the network to answer (ArtPoll). Blocks for `seconds`."""
        with self._search_lock:
            try:
                mac, nodes = discover(seconds)
                self.last_search = {"at": time.time(), "mac": mac, "nodes": nodes, "error": None}
            except OSError as exc:  # port 6454 busy, no network...
                self.last_search = {"at": time.time(), "mac": None, "nodes": [], "error": str(exc)}
            return self.last_search

    def _make_analyzer(self, sr):
        a = self.config["audio"]
        return Analyzer(sr, self.clock, a["bpm_min"], a["bpm_max"],
                        on_event=self.engine.push_event, song_detect=a["song_detect"])

    def analyzer(self):
        return self.source.analyzer if self.source is not None else None

    def start(self):
        if self.source is not None:
            self.source.start(self.config["audio"]["device"])
        self.engine.start()

    def stop(self):
        self.engine.stop()
        if self.source is not None:
            self.source.stop()

    # --- client time -> server time -----------------------------------------------------------
    def _event_time(self, body):
        """When the pad was actually pressed, removing network delay (the client sends its clock)."""
        now = time.perf_counter()
        ct = body.get("ct")
        if not isinstance(ct, (int, float)):
            return now
        self._offsets.append(now - ct / 1000.0)
        return min(now, ct / 1000.0 + min(self._offsets))

    # --- commands --------------------------------------------------------------------------------
    def command(self, body):
        cmd = body.get("cmd")
        handler = getattr(self, f"cmd_{cmd}", None)
        if handler is None:
            return {"ok": False, "error": f"unknown command {cmd!r}"}
        try:
            result = handler(body) or {}
        except (KeyError, ValueError, TypeError, IndexError, StopIteration) as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        result.setdefault("ok", True)
        if result.pop("saved", False):
            self.config.save()
            result["config"] = self.config.snapshot()
        return result

    def cmd_search(self, b):
        return {"search": self.search(float(b.get("seconds", 2.5)))}

    def scene_list(self):
        return [s.summary() for s in self.engine.library.values()]

    def cmd_scene(self, b):
        if b["name"] not in self.engine.library:
            raise ValueError("no such scene")
        self.engine.set_scene(b["name"])

    def cmd_group(self, b):
        """Groove / Break / Drop from the Perform screen: a random scene from that group, or a
        named one. The Drop pad itself also flashes on the nearest beat and restarts the phrase."""
        group, name = b["group"], b.get("name")
        lib = self.engine.library
        if group not in GROUPS:
            raise ValueError("group should be groove, calm, peak or ambient")
        if name is not None and (name not in lib or lib[name].group != group):
            raise ValueError("no such scene in that group")
        n = round(self.clock.beat(self._event_time(b)))
        an = self.analyzer()
        ev = {"beat": n, "by": "you", "scene": name, "pick": name is None}
        if group == "peak":
            if name is None:
                self.clock.phrase_start(n)
            ev.update(type="drop", flash=name is None)
        else:
            ev["type"] = {"calm": "breakdown", "groove": "groove", "ambient": "ambient"}[group]
        if an is not None:
            an.force("breakdown" if group == "calm" else "groove")
        self.engine.push_event(ev)

    def cmd_pad(self, b):
        pad = b["pad"]
        t = self._event_time(b)
        eng, clk, an = self.engine, self.clock, self.analyzer()
        if pad == "tap":
            clk.tap(t)
        elif pad == "one":
            clk.downbeat(t)
        elif pad == "drop":
            n = round(clk.beat(t))
            clk.phrase_start(n)
            if an is not None:
                an.force("groove")
            eng.push_event({"type": "drop", "beat": n, "by": "you"})
        elif pad == "break":
            n = round(clk.beat(t))
            if eng.director.state == "breakdown":
                eng.push_event({"type": "groove", "beat": n, "by": "you"})
            else:
                if an is not None:
                    an.force("breakdown")
                eng.push_event({"type": "breakdown", "beat": n, "by": "you"})
        elif pad == "phrase":   # this beat starts the phrase (bar 1), without a flash
            clk.phrase_start(round(clk.beat(t)))
        elif pad == "next":
            group = GROUP_FOR_STATE[eng.director.state]
            eng.set_scene(eng.director.pick(group))
        else:
            raise ValueError(f"unknown pad {pad}")

    def cmd_hold(self, b):
        on = bool(b["on"])
        if b["what"] == "strobe":
            self.engine.strobe = on
        elif b["what"] == "blackout":
            self.engine.blackout = on
        else:
            raise ValueError("hold what?")

    def cmd_nudge(self, b):
        self.clock.nudge(_clamp(float(b["beats"]), -0.5, 0.5))

    def cmd_tempo(self, b):
        """Manual tempo change (only sticks when not following the music)."""
        bpm = _clamp(self.clock.bpm + float(b["delta"]), 60.0, 200.0)
        self.clock.set_tempo(bpm, time.perf_counter())

    def cmd_follow(self, b):
        on = bool(b["on"])
        self.clock.follow_audio = on
        if on:
            self.clock.source = "internal"   # re-lock to the music on the next good estimate
        with self.config.lock:
            self.config.data["audio"]["follow_audio"] = on
        return {"saved": True}

    def cmd_show(self, b):
        if "set" in b and b["set"] not in SET_NAMES:
            raise ValueError("no such set")
        with self.config.lock:
            s = self.config.data["show"]
            if "auto" in b:
                s["auto"] = bool(b["auto"])
            if "intensity" in b:
                s["intensity"] = _clamp(float(b["intensity"]), 0.0, 1.0)
            if "speed" in b:
                s["speed"] = float(b["speed"]) if float(b["speed"]) in (0.5, 1.0, 2.0) else 1.0
            if "sweep_width" in b:
                s["sweep_width"] = _clamp(float(b["sweep_width"]), 5.0, 90.0)
            if "ambient_on_silence" in b:
                s["ambient_on_silence"] = bool(b["ambient_on_silence"])
            if "smooth" in b:
                s["smooth"] = bool(b["smooth"])
            if "strobe_freq" in b:
                s["strobe_freq"] = _clamp(float(b["strobe_freq"]), 0.0, 4.0)
            if "set" in b:
                s["set"] = b["set"]
        if "set" in b:   # a scene from another set stops at once (the window set must never play a room scene)
            eng = self.engine
            playing = eng.library.get(eng.scene)
            if playing is None or playing.set != b["set"]:
                eng.set_scene(eng.director.pick(GROUP_FOR_STATE[eng.director.state]))
        return {"saved": True}

    def cmd_palette(self, b):
        """The live palette: two colour ranges every scene uses (except those that keep their
        own colours). "save": false while dragging."""
        ranges = {k: to_range(b[k]) for k in ("primary", "secondary") if k in b}   # all valid, or nothing changes
        with self.config.lock:
            pal = self.config.data["show"]["palette"]
            pal.update(ranges)
            if "on" in b:
                pal["on"] = bool(b["on"])
        return {"saved": bool(b.get("save", True))}

    # --- scene maker ------------------------------------------------------------------------------
    def cmd_scene_save(self, b):
        """Save a spec. An existing name overwrites (for a preset: overrides it); "as_new" or
        an unknown name saves under a fresh name made from the label."""
        spec = normalize(b["spec"])
        lib = self.engine.library
        if b.get("as_new") or spec["name"] not in lib:
            base = slug(spec["label"])
            name, k = base, 2
            while name in lib:
                name, k = f"{base}-{k}", k + 1
            spec["name"] = name
        with self.config.lock:
            saved = [s for s in self.config.data["scenes"] if s.get("name") != spec["name"]]
            if spec != preset(spec["name"]):   # saving a preset unchanged just drops the override
                saved.append(spec)
            self.config.data["scenes"] = saved
        self.engine.reload_scenes()
        return {"saved": True, "name": spec["name"], "scenes": self.scene_list()}

    def cmd_scene_delete(self, b):
        """Delete a saved scene; for an edited preset this restores the original."""
        name = b["name"]
        with self.config.lock:
            saved = self.config.data["scenes"]
            if not any(s.get("name") == name for s in saved):
                raise ValueError("nothing saved under that name")
            self.config.data["scenes"] = [s for s in saved if s.get("name") != name]
            if preset(name) is None:
                self.config.data["rotation"].pop(name, None)
        self.engine.reload_scenes()
        return {"saved": True, "scenes": self.scene_list()}

    def cmd_preview(self, b):
        """Play a spec on the lights while it's being edited ({"spec": null} stops)."""
        spec = b.get("spec")
        self.engine.set_preview(None if spec is None else Scene(spec))

    def cmd_rotation(self, b):
        """Whether auto mode may pick a scene ("scene"), or several at once ("scenes")."""
        names = b["scenes"] if "scenes" in b else [b["scene"]]
        if not isinstance(names, list) or any(n not in self.engine.library for n in names):
            raise ValueError("no such scene")
        with self.config.lock:
            for name in names:
                self.config.data["rotation"][name] = bool(b["on"])
        return {"saved": True}

    # --- calibration -------------------------------------------------------------------------
    def cmd_calib(self, b):
        """The Mount tab: point one light by hand, mark straight up and which way is the room,
        and save aims at the targets."""
        action = b["action"]
        eng = self.engine
        if action == "start":
            i = int(b["fixture"])
            eng.test = None
            eng.calib = {"fixture": i, "pos": int(round(self.config["fixtures"][i]["up"]))}
            return {}
        if action == "stop":
            eng.calib = None
            return {}
        if eng.calib is None:
            raise ValueError("calibration not started")
        i = eng.calib["fixture"]
        with self.config.lock:
            fx = self.config.data["fixtures"][i]
            pos = eng.calib["pos"]
            if action == "set":
                eng.calib["pos"] = int(_clamp(int(b["pos"]), 0, 255))
                return {}
            if action == "go":
                s = self.config.data["safety"]
                pts = {p["id"]: p for p in self.config.data["points"]}
                elev = target_elevation(fx, self.config.data["room"], pts, b["target"])
                wins = geometry.safe_windows(fx, *geometry.safe_range(fx, s))
                eng.calib["pos"] = int(round(geometry.place(fx, elev, wins, pos)))
                return {}
            if action == "up":
                fx["up"], fx["up_set"] = pos, True
            elif action == "dir":   # after pressing +10 from straight up: did they lean to the room?
                fx["dir"] = 1 if b["toward_room"] else -1
                fx["calibrated"] = bool(fx["up_set"])
            elif action == "save":
                pid = b["target"]
                if not any(p["id"] == pid for p in self.config.data["points"]):
                    raise ValueError("no such target")
                fx["targets"][pid] = pos
            elif action == "clear":
                fx["targets"].pop(b["target"], None)
            elif action == "forget":   # start the calibration over (beams stay off until redone)
                fx["up_set"], fx["calibrated"], fx["targets"] = False, False, {}
            else:
                raise ValueError(f"unknown calibration action {action}")
        return {"saved": True}

    def cmd_safety(self, b):
        """Beam safety: the lowest and highest elevation beams may light at, and the master switch."""
        with self.config.lock:
            s = self.config.data["safety"]
            if "low" in b:
                s["low"] = int(_clamp(float(b["low"]), -20, 60))
            if "high" in b:   # down to 90: on a windowsill, beams mustn't tilt back into the house
                s["high"] = int(_clamp(float(b["high"]), 90, 270))
            if "on" in b:
                s["on"] = bool(b["on"])
        return {"saved": True}

    # --- layout ---------------------------------------------------------------------------------
    def cmd_point(self, b):
        action = b["action"]
        with self.config.lock:
            pts = self.config.data["points"]
            room = self.config.data["room"]
            if action != "move":
                raise ValueError("unknown point action")
            p = next(p for p in pts if p["id"] == b["id"])
            p["x"] = round(_clamp(float(b["x"]), 0, room["w"]), 2)
            p["y"] = round(_clamp(float(b["y"]), 0, room["d"]), 2)
            if p["kind"] == "cluster" and "w" in b:   # the ball cluster also has a size
                p["w"] = round(_clamp(float(b["w"]), 0.2, room["w"]), 2)
                p["h"] = round(_clamp(float(b["h"]), 0.2, room["d"]), 2)
            if "z" in b:   # height off the floor
                p["z"] = round(_clamp(float(b["z"]), 0.0, room["h"]), 2)
        return {"saved": True}

    FIXTURE_FIELDS = {
        "name": str, "ip": str, "universe": int, "address": int, "x": float, "y": float,
        "facing": float, "z": float, "length": float, "flip": bool, "spin_fwd": list, "spin_rev": list,
        "speed_fast": int, "speed_slow": int,
    }

    def cmd_fixture(self, b):
        i = int(b["index"])
        with self.config.lock:
            fx = self.config.data["fixtures"][i]
            room = self.config.data["room"]
            for key, value in b["fields"].items():
                if key == "limit":   # first CH1 step toward the room where beams would reach people
                    if value in (None, ""):
                        fx["limit"] = None
                        continue
                    value = int(_clamp(int(value), 0, 255))
                    if not -45 < geometry.elevation(fx, value) < 60:
                        raise ValueError("the limit should be the step just past level on the room side")
                    fx["limit"] = value
                    continue
                if key == "ball_span":   # CH1 range where the curtain hits the disco balls
                    fx["ball_span"] = None if not value else sorted(int(_clamp(int(v), 0, 255)) for v in value[:2])
                    continue
                kind = self.FIXTURE_FIELDS.get(key)
                if kind is None:
                    raise ValueError(f"can't set {key}")
                if kind is list:
                    value = [int(_clamp(int(value[0]), 0, 255)), int(_clamp(int(value[1]), 0, 255))]
                else:
                    value = kind(value)
                if key == "ip":
                    value = value.strip().lower()
                    if value != BROADCAST and not IP_RE.match(value):
                        raise ValueError("IP should look like 192.168.1.50, or be broadcast")
                if key == "universe":
                    value = int(_clamp(value, 0, 32767))
                if key == "address":
                    value = int(_clamp(value, 1, 512 - 83))
                if key in ("x", "y"):
                    value = round(_clamp(value, 0, room["w" if key == "x" else "d"]), 2)
                if key == "facing":
                    value = value % 360
                if key == "z":
                    value = round(_clamp(value, 0.3, room["h"]), 2)
                if key == "length":
                    value = round(_clamp(value, 0.3, 3.0), 2)
                fx[key] = value
        return {"saved": True}

    def cmd_room(self, b):
        with self.config.lock:
            room = self.config.data["room"]
            room["w"] = round(_clamp(float(b["w"]), 2.0, 30.0), 2)
            room["d"] = round(_clamp(float(b["d"]), 2.0, 30.0), 2)
            if "h" in b:
                room["h"] = round(_clamp(float(b["h"]), 1.8, 12.0), 2)
        return {"saved": True}

    def cmd_audio(self, b):
        restart = False
        with self.config.lock:
            a = self.config.data["audio"]
            if "device" in b:
                dev = b["device"]
                a["device"] = None if dev in (None, "", "default") else int(dev)
                restart = True
            if "bpm_min" in b or "bpm_max" in b:
                lo = _clamp(float(b.get("bpm_min", a["bpm_min"])), 60, 180)
                hi = _clamp(float(b.get("bpm_max", a["bpm_max"])), lo + 5, min(200, lo * 1.95))
                a["bpm_min"], a["bpm_max"] = lo, hi
                an = self.analyzer()
                if an is not None:
                    an.set_range(lo, hi)
            if "sync_ms" in b:
                a["sync_ms"] = int(_clamp(int(b["sync_ms"]), -100, 300))
            if "phrase_bars" in b:
                a["phrase_bars"] = int(b["phrase_bars"]) if int(b["phrase_bars"]) in (8, 16, 32) else 16
                self.clock.phrase_bars = a["phrase_bars"]
            if "song_detect" in b:
                a["song_detect"] = bool(b["song_detect"])
                an = self.analyzer()
                if an is not None:
                    an.song_detect = a["song_detect"]
        if restart and isinstance(self.source, AudioInput):
            self.source.start(self.config["audio"]["device"])
        return {"saved": True}

    def cmd_output(self, b):
        with self.config.lock:
            self.config.data["output"]["enabled"] = bool(b["on"])
        return {"saved": True}

    # --- channel test page ------------------------------------------------------------------------
    def cmd_test(self, b):
        action = b["action"]
        eng = self.engine
        if action == "stop":
            eng.test = None
            return {}
        if action == "start":
            i = int(b["fixture"])
            eng.calib = None
            eng.test = {"fixture": i, "pos": int(round(eng.prev[i].pos)), "channels": {}}
            return {}
        if eng.test is None:
            raise ValueError("test not started")
        ch = eng.test["channels"]
        if action == "set":
            idx = int(b["channel"]) - 1
            if not 0 <= idx < 83:
                raise ValueError("channel must be 1-83")
            if idx == 0:
                eng.test["pos"] = int(_clamp(int(b["value"]), 0, 255))
            else:
                ch[idx] = int(_clamp(int(b["value"]), 0, 255))
        elif action == "clear":
            ch.clear()
        elif action == "pattern":
            ch.clear()
            k, n = b["kind"], int(b["index"])
            if k == "beam":
                ch[BEAMS[n][3] - 1] = 255       # white die of beam group n
            elif k == "strip":
                for row in STRIP[n]:            # red of strip segment n, both rows
                    ch[row[0] - 1] = 255
            elif k == "white":
                ch[WHITE[n] - 1] = 255
            else:
                raise ValueError("pattern kind")
        else:
            raise ValueError("unknown test action")
        return {"test": {"fixture": eng.test["fixture"], "pos": eng.test["pos"],
                         "channels": {str(k + 1): v for k, v in ch.items()}}}
