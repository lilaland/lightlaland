"""Scene engine checks: every scene renders valid DMX, specs are sanitised, the scene maker's
save/delete/preview commands work, sets keep auto mode in the chosen set, and strobe bursts
follow Strobe freq. Uses a scratch config and never sends Art-Net.

    python -m tests.test_scenes          (from the project folder, ~3 s)
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np  # noqa: E402
from lightlaland.app import App  # noqa: E402
from lightlaland.fixture import STRIP, encode  # noqa: E402
from lightlaland.scenes import GROUPS, PRESETS, h01, normalize, preset  # noqa: E402


def check(cond, what):
    if not cond:
        raise AssertionError(what)
    print(f"  ok  {what}")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        app = App(os.path.join(tmp, "test.json"), no_audio=True, dry_run=True)
        eng, fxs = app.engine, app.config["fixtures"]

        def cmd(**body):
            r = app.command(body)
            assert r["ok"], r
            return r

        t = time.perf_counter()
        for name in eng.library:
            eng.set_scene(name)
            for k in range(80):   # two bars at 120 BPM
                eng.step(t + k * 0.05, 0.05)
                for f, fx in zip(eng.prev, fxs):
                    ch = encode(f, fx)
                    assert len(ch) == 84 and ch[83] == 0, name
                    for rows in STRIP:   # both RGB rows of a position always match
                        assert [ch[c - 1] for c in rows[0]] == [ch[c - 1] for c in rows[1]], name
        check(len(eng.library) == len(PRESETS), f"all {len(PRESETS)} presets render 84 valid channels, reset always 0")

        junk = normalize({"label": "  ", "move": {"mode": "warp", "jitter": 99}, "beams": {"level": -3},
                          "palette": {"a": "red"}, "evil": 1})
        check(junk["move"]["mode"] == "aim" and junk["move"]["jitter"] == 8 and junk["beams"]["level"] == 0
              and len(junk["palette"]) == 5 and "evil" not in junk, "bad specs are sanitised")
        check(all(normalize(p) == p for p in PRESETS), "presets are already normal")

        r = cmd(cmd="scene_save", spec=dict(preset("cross"), label="Mine"), as_new=True)
        check(r["name"] == "mine" and not eng.library["mine"].builtin, "save as new makes a custom scene")
        spec = preset("updown")
        spec["strip"]["width"] = 0.3
        cmd(cmd="scene_save", spec=spec)
        check(eng.library["updown"].modified, "saving an edited preset overrides it")
        cmd(cmd="scene_delete", name="updown")
        check(not eng.library["updown"].modified, "deleting the override restores the preset")
        cmd(cmd="scene", name="mine")
        cmd(cmd="scene_delete", name="mine")
        check("mine" not in eng.library and eng.scene in eng.library, "deleting the playing scene falls back")

        cmd(cmd="preview", spec={"move": {"mode": "spin"}})
        eng.step(t + 10, 0.05)
        check(eng.vis["preview"] and eng.prev[0].spin > 0, "preview plays an unsaved spec")
        cmd(cmd="scene", name="cross")
        check(eng.preview is None, "picking a scene ends the preview")

        cmd(cmd="group", group="peak")
        eng.step(t + 11, 0.05)
        check(eng.director.state == "drop" and eng.library[eng.scene].group == "peak", "Drop pad: random peak scene")
        cmd(cmd="group", group="calm", name="drift")
        eng.step(t + 11.1, 0.05)
        check(eng.scene == "drift" and eng.director.state == "breakdown", "picking a calm scene by name")

        cmd(cmd="palette", on=True, colors=["#102030", "#ffffff", "#ff0000", "#00ff00", "#0000ff"])
        check(eng._palette(eng.library["cross"], app.config["show"]["palette"])[0][0] < 0.1, "live palette overrides")
        check(not app.command({"cmd": "palette", "colors": ["#fff"]})["ok"], "bad palettes are refused")

        cmd(cmd="group", group="ambient")
        eng.step(t + 12, 0.05)
        check(eng.director.state == "ambient" and eng.library[eng.scene].group == "ambient", "Ambient pad")
        cmd(cmd="show", auto=True)
        eng.push_event({"type": "music", "beat": 0})
        eng.step(t + 12.1, 0.05)
        check(eng.director.state == "ambient", "music coming back doesn't end an ambient you chose")
        cmd(cmd="group", group="groove")
        eng.push_event({"type": "silence", "beat": 0})
        eng.step(t + 12.2, 0.05)
        check(eng.director.state == "ambient", "silence goes ambient (auto)")
        eng.push_event({"type": "music", "beat": 0})
        eng.step(t + 12.3, 0.05)
        check(eng.director.state == "groove", "and music brings it back")

        old = normalize({"palette": {"a": "#ff0000", "b": "#00ff00"}, "move": {"target": "back"}})
        check(len(old["palette"]) == 5 and old["move"]["target"] == "crowd" and old["move"]["face"] == "leds",
              "specs saved by the old version still load")

        # Sets: auto mode and the pads only pick from the chosen set.
        cmd(cmd="show", set="halloween")
        check(eng.library[eng.scene].set == "halloween", "choosing a set swaps out a scene from another set")
        picks = {eng.director.pick(g) for g in GROUPS for _ in range(30)}
        check(all(eng.library[n].set == "halloween" for n in picks) and len(picks) > 5,
              f"auto mode picks only from the Halloween set ({len(picks)} different scenes)")
        cmd(cmd="show", set="window")
        picks = {eng.director.pick(g) for g in GROUPS for _ in range(10)}
        check(eng.scene == "window" and picks == {"window"}, "the window set never plays a room scene, whatever the group")
        cmd(cmd="show", set="")
        picks = {eng.director.pick(g) for g in GROUPS for _ in range(30)}
        check(all(eng.library[n].set == "" for n in picks), "everyday picks leave the Halloween scenes out")
        check(not app.command({"cmd": "show", "set": "xmas"})["ok"], "unknown sets are refused")

        # Random strobe bursts: none at Off, more at 4x than 1x, about the asked-for rate.
        def lit_frames(freq, spec, seconds=60.0):
            cmd(cmd="show", strobe_freq=freq)
            cmd(cmd="preview", spec=spec)
            lit = 0
            for k in range(int(seconds / 0.025)):
                eng.step(t + 100 + k * 0.025, 0.025)
                lit += any(w > 0 for w in eng.prev[0].white)
            return lit
        bursts = {"white": {"pattern": "off"}, "fx": {"strobe": 30}}
        off, one, four = (lit_frames(f, bursts) for f in (0, 1, 4))
        # 30 bursts a minute x 4 flashes, each lit for 40% of a 16th (~2 frames at 124 BPM)
        check(off == 0 and 120 < one < 400 and four > 2 * one,
              f"strobe bursts: none when off, ~240 lit frames a minute at 1x, more at 4x ({off} / {one} / {four})")
        storm = {"white": {"pattern": "off"}, "fx": {"lightning": 20}}
        check(lit_frames(0, storm) == 0 < lit_frames(1, storm), "Strobe freq Off also stops lightning")
        cmd(cmd="show", strobe_freq=1)

        # The spooky patterns move: different segments lit at different times.
        for pat in ("eyes", "wander", "glitch"):
            cmd(cmd="preview", spec={"strip": {"pattern": pat, "rate": 1, "density": 0.4}})
            states = set()
            for k in range(160):
                eng.step(t + 200 + k * 0.025, 0.025)
                states.add(tuple(round(max(rgb), 1) for rgb in eng.prev[0].strip))
            check(len(states) > 10, f"the {pat} pattern changes along the bar and over time ({len(states)} looks in 4 s)")
        cmd(cmd="preview", spec=None)

        # The hash behind every random pattern: (a, b) and (a + 1, b - 1) must not look alike
        # (they did when its weights were equal mod 2 pi, and pairs of eyes moved in step).
        a = np.array([h01(s, 7) for s in range(3000)])
        b = np.array([h01(s + 1, 6) for s in range(3000)])
        check(abs(np.corrcoef(a, b)[0, 1]) < 0.08, "the random patterns' hash doesn't repeat itself in disguise")
    print("scenes: all good")


if __name__ == "__main__":
    main()
