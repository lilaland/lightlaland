"""Wide tempo range: with the analyzer listening from 92 to 183 BPM, check it locks to the
right tempo (not 2:3, 3:2 or 4:3 of it) and beat on slow house, hip-hop and drum & bass, and
follows quick blends between records at different tempos without calling a breakdown.
Synthetic records from tests/tempo_sim.py.

    python -m tests.test_tempo_range          (from the project folder, ~10 s)
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lightlaland.analyzer import Analyzer  # noqa: E402
from lightlaland.clock import BeatClock  # noqa: E402
from tests.tempo_sim import SR, make_blend, make_record  # noqa: E402

BPM_MIN, BPM_MAX = 92.0, 183.0   # the listening range under test
TEMPO_TOL = 0.02                 # clock tempo within 2% of the record's
PHASE_TOL_MS = 35.0              # and the beat within 35 ms, to count as locked


def check(cond, what):
    if not cond:
        raise AssertionError(what)
    print(f"  ok  {what}")


def run(mix, truth, block=1024):
    """Feed the audio; at every true beat note the clock's beat error (ms) and its tempo
    relative to the record's. Returns those rows and the analyzer's event types."""
    clock = BeatClock(bpm=120.0, now=0.0)
    events = []
    an = Analyzer(SR, clock, BPM_MIN, BPM_MAX, on_event=events.append)
    rows, bi = [], 0
    for start in range(0, mix.size - block, block):
        t_end = (start + block - 1) / SR
        an.process(mix[start:start + block], t_end)
        while bi < len(truth) and truth[bi][0] <= t_end:
            t_b, rec = truth[bi]
            same = bi + 1 < len(truth) and truth[bi + 1][1] == rec
            period = truth[bi + 1][0] - t_b if same else t_b - truth[bi - 1][0]
            b = clock.beat(t_b)
            rows.append((t_b, (b - round(b)) * clock.period * 1000, clock.bpm * period / 60.0))
            bi += 1
    return rows, [ev["type"] for ev in events]


def lock(rows, t0):
    """When the clock locked for good after t0 (every later beat on tempo and on the beat),
    and the beats from then on. (None, []) if it never did."""
    sel = [r for r in rows if r[0] >= t0]
    k = len(sel)
    while k > 0 and abs(sel[k - 1][2] - 1) < TEMPO_TOL and abs(sel[k - 1][1]) < PHASE_TOL_MS:
        k -= 1
    if k == len(sel):
        return None, []
    return sel[k][0], sel[k:]


def check_lock(name, rows, t0, within_s):
    t, after = lock(rows, t0)
    took = "never" if t is None else f"after {t - t0:.1f} s"
    check(t is not None and t - t0 < within_s and len(after) >= 32,
          f"{name}: on the right tempo and beat {took} (< {within_s:g} s) and held to the end")
    ms = np.array([r[1] for r in after])
    mean, p95, worst = ms.mean(), np.percentile(np.abs(ms), 95), np.abs(ms).max()
    check(abs(mean) < 5 and p95 < 8 and worst < 12,
          f"{name}: beat error mean {mean:+.1f} ms, p95 {p95:.1f} ms, max {worst:.1f} ms")


def records():
    """Single records from the start (the analyzer needs 4 s before its first estimate)."""
    for kind, bpm, bars, what in (("house", 100.0, 16, "house 100 (four on the floor)"),
                                  ("hiphop", 96.0, 16, "hip-hop 96 (kick 1 and the and of 3)"),
                                  ("dnb", 174.0, 24, "drum & bass 174 (two-step)")):
        mix, truth = make_record(kind, bpm, bars=bars)
        rows, events = run(mix, truth)
        check_lock(what, rows, 0.0, 10.0)
        check("breakdown" not in events, f"{what}: no false breakdown")


def blends():
    """Record A at 124 crossfaded over 8 s into B at its own tempo; follow B once it's louder."""
    for kind_b, bpm_b, b_bars, what in (("dnb", 174.0, 24, "124 house -> 174 drum & bass"),
                                        ("house", 100.0, 16, "124 house -> 100 house"),
                                        ("hiphop", 96.0, 16, "124 house -> 96 hip-hop")):
        mix, truth, fade = make_blend("house", 124.0, kind_b, bpm_b, a_bars=8, b_bars=b_bars)
        rows, events = run(mix, truth)
        switch = next(t for t, rec in truth if rec == "B")
        a_rows = [r for r in rows if r[0] < fade]
        check(lock(a_rows, 0.0)[0] is not None, f"{what}: locked to the 124 record first")
        check_lock(what, rows, switch, 10.0)
        check("breakdown" not in events and "drop" not in events,
              f"{what}: no breakdown or drop called during the blend")


def main():
    records()
    blends()


if __name__ == "__main__":
    main()
