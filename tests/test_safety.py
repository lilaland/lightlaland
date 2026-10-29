"""Beam safety: run every scene through the real engine against a simulated head and check
that no beam is ever lit while the head points below the lowest allowed angle.

The simulated head moves at the measured motor speed (faster than the engine's cautious
guess), starts somewhere random, and after a spin stops at a random place, so it is always
ahead of what the engine assumes. Uses a scratch config and never sends Art-Net.

    python -m tests.test_safety          (from the project folder, ~8 s)
"""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lightlaland import geometry  # noqa: E402
from lightlaland.app import App  # noqa: E402
from lightlaland.engine import BeamGuard  # noqa: E402
from lightlaland.fixture import Frame, encode  # noqa: E402

DT = 0.025


def real_speed(ch3):
    """Steps per second the head really turns (the measured curve, no safety margin)."""
    return geometry.steps_per_second(ch3) * geometry.SLOW_MARGIN


class Head:
    def __init__(self, rng):
        self.rng, self.pos, self.spinning = rng, rng.uniform(0, 255), False

    def follow(self, ch):
        if ch[1]:                       # CH2 spin: position unknown until it stops
            self.spinning = True
            return
        if self.spinning:               # stopped somewhere we couldn't see
            self.spinning, self.pos = False, self.rng.uniform(0, 255)
        step = real_speed(ch[2]) * DT
        self.pos += max(-step, min(step, ch[0] - self.pos))


JUMPS = []   # (scene, fixture, steps): the head sent the long way round within one scene


def run(app, heads, seconds, now, violations, label, low, high=None):
    """Play the current scene; record every frame a beam was lit outside low..high (the arc
    may run past 180, down the wall side) or while spinning."""
    high = app.config["safety"]["high"] if high is None else high
    eng, fxs = app.engine, app.config["fixtures"]
    lit_frames, last = 0, [None, None]
    for k in range(int(seconds / DT)):
        now += DT
        eng.step(now, DT)
        for i, (f, fx) in enumerate(zip(eng.prev, fxs)):
            ch = encode(f, fx)
            if last[i] is not None and k > 40 and abs(ch[0] - last[i]) > 85 and not ch[1]:
                JUMPS.append((label, i, ch[0] - last[i]))
            last[i] = ch[0]
            heads[i].follow(ch)
            lit = any(ch[3:27])
            if lit:
                lit_frames += 1
                elev = geometry.elevation(fx, heads[i].pos)
                if heads[i].spinning or not geometry.in_arc(elev, low - 1.0, high + 1.0):
                    violations.append((label, i, round(now, 2), round(elev, 1), heads[i].spinning))
    return now, lit_frames


def main():
    rng = random.Random(7)
    with tempfile.TemporaryDirectory() as tmp:
        app = App(os.path.join(tmp, "test.json"), no_audio=True, dry_run=True)
        eng = app.engine
        app.command({"cmd": "show", "auto": False})   # so each run really is the named scene
        heads = [Head(rng), Head(rng)]
        now = eng.t0
        violations = []
        low = app.config["safety"]["low"]

        # Not calibrated: beams must stay off whatever plays.
        lit_total = 0
        for name in eng.library:
            eng.set_scene(name)
            now, lit = run(app, heads, 2.0, now, violations, f"uncalibrated {name}", low)
            lit_total += lit
        assert lit_total == 0, "beams lit before calibration"
        print("  ok  no beams before the tilt is calibrated")

        for i in range(2):   # calibrate both through the Mount commands
            assert app.command({"cmd": "calib", "action": "start", "fixture": i})["ok"]
            assert app.command({"cmd": "calib", "action": "set", "pos": 42})["ok"]
            assert app.command({"cmd": "calib", "action": "up"})["ok"]
            assert app.command({"cmd": "calib", "action": "dir", "toward_room": True})["ok"]
            assert app.command({"cmd": "calib", "action": "stop"})["ok"]
        assert all(fx["calibrated"] for fx in app.config["fixtures"])

        lit_by_scene = {}
        names = list(eng.library)
        order = names + [rng.choice(names) for _ in range(40)]   # every scene, then random hops
        for name in order:
            eng.set_scene(name)
            app.command({"cmd": "show", "speed": rng.choice([0.5, 1, 2])})
            now, lit = run(app, heads, rng.uniform(1.0, 8.0), now, violations, name, low)
            lit_by_scene[name] = lit_by_scene.get(name, 0) + lit
        assert not violations, f"beams lit while pointing low: {violations[:5]}"
        assert not JUMPS, f"the head was sent more than half a turn within a scene: {JUMPS[:5]}"
        print("  ok  no scene sends the head the long way round")
        print(f"  ok  {len(order)} scene runs, no beam lit while the simulated head pointed below {low} degrees")
        lit = [n for n, k in lit_by_scene.items() if k]
        print(f"  ok  beams did light (safely) in {len(lit)} scenes: {', '.join(sorted(lit))}")
        assert len(lit) >= 10, "safety is so strict that beam scenes never light"

        # The user's measured calibration: up at 40 for both, beams dead from 81 (A) / 82 (B)
        # toward the room, to straight down at 125. Past that (down the wall side) is allowed.
        app.command({"cmd": "safety", "low": 0, "high": 270})
        for i, (limit, span) in enumerate(((81, [76, 81]), (82, [77, 81]))):
            fx = app.config["fixtures"][i]
            fx.update(up=40, dir=1)
            assert app.command({"cmd": "fixture", "index": i, "fields": {"limit": limit, "ball_span": span}})["ok"]
        safety = app.config["safety"]
        for i, last in ((0, 80), (1, 81)):
            fx = app.config["fixtures"][i]
            wins = geometry.safe_windows(fx, *geometry.safe_range(fx, safety))
            guard = BeamGuard(1, 0.0)
            allowed = []
            for ch in range(256):   # a head parked at each position
                guard.lo[0] = guard.hi[0] = float(ch)
                if guard.check(0, fx, Frame(pos=ch), 1.0, 0.0, safety, wins)[0]:
                    allowed.append(ch)
            assert allowed == list(range(1, last + 1)) + list(range(126, last + 171)), (fx["id"], allowed)
        print("  ok  A lights CH1 1-80 and 126-250, B 1-81 and 126-251: dark exactly from the limit to straight down")
        violations.clear()
        lows = [geometry.safe_range(fx, safety)[0] for fx in app.config["fixtures"]]
        for name in names:
            eng.set_scene(name)
            now, _ = run(app, heads, 3.0, now, violations, name, min(lows))
        late = [v for v in violations if v[4] or not geometry.in_arc(v[3], lows[v[1]] - 1.0, 271.0)]
        assert not late, f"beams lit past a light's own limit: {late[:5]}"
        print("  ok  every scene keeps each light inside its own limit")

        # A raised lowest angle is honoured too.
        app.command({"cmd": "safety", "low": 30})
        violations.clear()
        for name in names:
            eng.set_scene(name)
            now, _ = run(app, heads, 3.0, now, violations, name, 30)
        assert not violations, f"beams lit below 30 degrees: {violations[:5]}"
        print("  ok  a raised lowest angle (30 degrees) is respected")

        # On the windowsill: beams only between 25 degrees (over the houses across the street)
        # and 95 (never tilting back into the house). The window scene still has to light.
        app.command({"cmd": "safety", "low": 25, "high": 95})
        violations.clear()
        lit_window = 0
        for name in names:
            eng.set_scene(name)
            now, lit = run(app, heads, 3.0, now, violations, name, 25, 95)
            lit_window += lit if name == "window" else 0
        assert not violations, f"beams lit outside 25-95 degrees: {violations[:5]}"
        assert lit_window > 0, "the window scene's beams never lit inside 25-95 degrees"
        print(f"  ok  a windowsill deadzone (beams only 25-95 degrees) is respected; the window scene lit {lit_window} frames")
    print("safety: all good")


if __name__ == "__main__":
    main()
