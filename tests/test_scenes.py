"""Scene engine checks: every scene renders valid DMX, specs are sanitised, the scene maker's
save/delete/preview commands work, sets keep auto mode in the chosen set, and strobe bursts
follow Strobe freq. Uses a scratch config and never sends Art-Net.

    python -m tests.test_scenes          (from the project folder, ~3 s)
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np  # noqa: E402
from lightlaland.app import App  # noqa: E402
from lightlaland.config import Config  # noqa: E402
from lightlaland.fixture import STRIP, encode  # noqa: E402
from lightlaland.scenes import DEFAULT_PALETTE, GROUPS, PRESETS, h01, normalize, preset  # noqa: E402


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
        two_rows = set()
        for name in eng.library:
            eng.set_scene(name)
            same = eng.library[name].spec["strip"]["rows"] == "same"
            for k in range(80):   # two bars at 120 BPM
                eng.step(t + k * 0.05, 0.05)
                for f, fx in zip(eng.prev, fxs):
                    ch = encode(f, fx)
                    assert len(ch) == 84 and ch[83] == 0, name
                    differ = any([ch[c - 1] for c in rows[0]] != [ch[c - 1] for c in rows[1]] for rows in STRIP)
                    assert not (same and differ), f"{name}: the strip rows differ though the scene says same"
                    if differ:
                        two_rows.add(name)
        check(len(eng.library) == len(PRESETS), f"all {len(PRESETS)} presets render 84 valid channels, reset always 0")
        check(len(two_rows) >= 10, f"{len(two_rows)} scenes drive the strip's two rows differently")

        junk = normalize({"label": "  ", "move": {"mode": "warp", "jitter": 99}, "beams": {"level": -3},
                          "palette": {"primary": {"h": "red"}}, "tags": ["beams", "lasers"], "evil": 1})
        check(junk["move"]["mode"] == "aim" and junk["move"]["jitter"] == 8 and junk["beams"]["level"] == 0
              and junk["palette"] == DEFAULT_PALETTE and junk["tags"] == ["beams"] and "evil" not in junk,
              "bad specs are sanitised")
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

        green = {"h": 120, "s": 1, "r": 0.1}
        cmd(cmd="palette", on=True, primary=green, secondary={"h": 240, "s": 0.5, "r": 0})
        live = app.config["show"]["palette"]
        check(eng._colours(eng.library["cross"], live)[0]["h"] == 120, "the live palette replaces a scene's ranges")
        keeps = next(s for s in eng.library.values() if s.spec["own_colors"])
        check(eng._colours(keeps, live)[0] == keeps.palette["primary"],
              f"but not in a scene that keeps its own colours ({keeps.label})")
        check(not app.command({"cmd": "palette", "primary": {"h": "red", "s": 1, "r": 0.1}})["ok"]
              and live["primary"]["h"] == 120, "bad palettes are refused, and nothing changes")

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
        five = normalize({"palette": ["#0000ff", "#ffff00", "#ff0000", "#00ff00", "#ffffff"], "beams": {"color": "c"}})
        check(old["palette"]["primary"]["h"] == 0 and old["palette"]["secondary"]["h"] == 120
              and five["palette"]["primary"]["h"] == 240 and five["beams"]["color"] == "b"
              and old["move"]["target"] == "crowd" and old["move"]["face"] == "leds",
              "specs saved by the old version still load")

        # Sets: auto mode and the pads only pick from the chosen set.
        cmd(cmd="show", set="halloween")
        check(eng.library[eng.scene].set == "halloween", "choosing a set swaps out a scene from another set")
        picks = {eng.director.pick(g) for g in GROUPS for _ in range(30)}
        check(all(eng.library[n].set == "halloween" for n in picks) and len(picks) > 5,
              f"auto mode picks only from the Halloween set ({len(picks)} different scenes)")
        cmd(cmd="show", set="window")
        picks = {eng.director.pick(g) for g in GROUPS for _ in range(30)}
        check(eng.library[eng.scene].set == "window" and all(eng.library[n].set == "window" for n in picks),
              f"the window set never plays a room scene, whatever the group ({len(picks)} window scenes picked)")
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

        # One at a time: exactly one beam lit, each step a new colour; scatter never lights one twice in a row.
        # (Safety off for these: the test bars aren't calibrated, and here it's the renderer under test.)
        cmd(cmd="safety", on=False)
        for pat in ("step", "pingpong", "scatter"):
            cmd(cmd="preview", spec={"beams": {"pattern": pat, "color": "step", "rate": 1.5}})
            lit_counts, seq, colours = set(), [], set()
            for k in range(160):
                eng.step(t + 300 + k * 0.025, 0.025)
                on = [g for g, b in enumerate(eng.prev[0].beams) if max(b) > 0.01]
                lit_counts.add(len(on))
                if on and (not seq or seq[-1] != on[0]):
                    seq.append(on[0])
                    colours.add(tuple(round(v, 2) for v in eng.prev[0].beams[on[0]][:3]))
            repeats = sum(a == b for a, b in zip(seq, seq[1:]))
            check(lit_counts == {1} and len(colours) > 3 and repeats == 0,
                  f"{pat}: one beam at a time, {len(set(seq))} different beams, {len(colours)} colours")
        # The strip's second row: split shows the other range, alt takes turns.
        cmd(cmd="preview", spec={"strip": {"pattern": "solid", "rows": "split"}, "own_colors": True,   # (the live palette is on)
                                 "palette": {"primary": {"h": 0, "s": 1, "r": 0}, "secondary": {"h": 240, "s": 1, "r": 0}}})
        eng.step(t + 400, 0.025)
        f = eng.prev[0]
        check(f.strip[0][0] > 0.9 and f.strip2[0][2] > 0.9, "split rows: row 1 from the primary range, row 2 from the secondary")
        cmd(cmd="preview", spec={"strip": {"pattern": "solid", "rows": "alt"}})
        both = set()
        for k in range(80):
            eng.step(t + 401 + k * 0.025, 0.025)
            f = eng.prev[0]
            both.add((max(map(max, f.strip)) > 0.01, max(map(max, f.strip2)) > 0.01))
        check(both == {(True, False), (False, True)}, "alt rows take turns, never both lit")
        # Fills: in the last beat of every bar, one beam races down the line.
        cmd(cmd="preview", spec={"beams": {"pattern": "solid"}, "fx": {"fill": 1, "fill_len": 1}})
        filled, other = set(), set()
        for k in range(160):
            eng.step(t + 500 + k * 0.025, 0.025)
            n_lit = sum(max(b) > 0.01 for b in eng.prev[0].beams)
            beat = eng.vis["clock"]["beat_in_bar"]
            (filled if beat == 3 else other).add(n_lit)
        check(filled == {1} and other == {6}, "a fill races one beam down the line in the last beat of the bar")
        cmd(cmd="preview", spec=None)
        cmd(cmd="safety", on=True)

        # Tags: worked out for scenes saved without them, kept when given; auto mode in bulk.
        hop = normalize({"move": {"mode": "hop"}, "beams": {"pattern": "solid"}, "white": {"pattern": "solid"}})
        check(hop["tags"] == ["beams", "strobes", "discoball"] and normalize(dict(hop, tags=["washers"]))["tags"] == ["washers"],
              "tags are worked out when missing and kept when given")
        names = [s.name for s in eng.library.values() if "discoball" in s.tags]
        cmd(cmd="rotation", scenes=names, on=False)
        check(all(app.config["rotation"][n] is False for n in names) and not app.command(
            {"cmd": "rotation", "scenes": ["nope"], "on": True})["ok"], f"auto mode off for all {len(names)} disco ball scenes at once")
        cmd(cmd="rotation", scenes=names, on=True)

        # The hash behind every random pattern: (a, b) and (a + 1, b - 1) must not look alike
        # (they did when its weights were equal mod 2 pi, and pairs of eyes moved in step).
        a = np.array([h01(s, 7) for s in range(3000)])
        b = np.array([h01(s + 1, 6) for s in range(3000)])
        check(abs(np.corrcoef(a, b)[0, 1]) < 0.08, "the random patterns' hash doesn't repeat itself in disguise")

        # A config saved before colour ranges: the live palette's first two colours become the ranges.
        old_cfg = os.path.join(tmp, "old.json")
        for audio in ({"bpm_min": 115, "bpm_max": 150}, {"bpm_min": 120, "bpm_max": 135}):
            with open(old_cfg, "w") as fh:
                json.dump({"version": 4, "audio": audio,
                           "show": {"palette": {"on": True, "colors": ["#00ff00", "#0000ff", "#fff", "#000", "#f00"]}}}, fh)
            cfg = Config(old_cfg)
            pal, bpm = cfg["show"]["palette"], (cfg["audio"]["bpm_min"], cfg["audio"]["bpm_max"])
            if audio["bpm_min"] == 115:
                check(pal["on"] and pal["primary"]["h"] == 120 and pal["secondary"]["h"] == 240 and "colors" not in pal
                      and bpm == (92, 183), "an older config: the live palette turns into two ranges, 115-150 BPM widens to 92-183")
            else:
                check(bpm == (120, 135), "but a listening range someone chose is kept")
    print("scenes: all good")


if __name__ == "__main__":
    main()
