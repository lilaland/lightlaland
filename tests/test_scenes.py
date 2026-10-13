"""Scene engine checks: every scene renders valid DMX, specs are sanitised, and the scene
maker's save/delete/preview commands work. Uses a scratch config and never sends Art-Net.

    python -m tests.test_scenes          (from the project folder, takes a second)
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lightlaland.app import App  # noqa: E402
from lightlaland.fixture import STRIP, encode  # noqa: E402
from lightlaland.scenes import PRESETS, normalize, preset  # noqa: E402


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
    print("scenes: all good")


if __name__ == "__main__":
    main()
