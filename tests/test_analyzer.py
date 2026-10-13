"""Run the analyzer over the synthetic vinyl mix and score it against the known beats.

    python -m tests.test_analyzer          (from the project folder)
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lightlaland.analyzer import Analyzer  # noqa: E402
from lightlaland.clock import BeatClock   # noqa: E402
from tests.vinyl_sim import SR, make_mix  # noqa: E402


def dominant_beats(truth):
    """The beats a listener follows: record A until B is louder, then B."""
    out = []
    for k, t in enumerate(truth["a_beats"]):
        bar = k // 4
        b_bar = None
        if t >= truth["b_beats"][0]:
            b_bar = int(np.searchsorted(truth["b_beats"], t)) // 4
        if b_bar is None or truth["a_gain"](bar) >= truth["b_gain"](b_bar):
            out.append((t, "A"))
    last_a = out[-1][0]
    for k, t in enumerate(truth["b_beats"]):
        if t > last_a + 0.2:
            out.append((t, "B"))
    return out


def run(mix, truth, block=1024):
    clock = BeatClock(bpm=124.0, now=0.0)
    events = []
    an = Analyzer(SR, clock, on_event=events.append)
    beats = dominant_beats(truth)
    bi = 0
    errs = []  # (time, error ms, true bpm, clock bpm, record)
    for start in range(0, mix.size - block, block):
        t_end = (start + block - 1) / SR
        an.process(mix[start:start + block], t_end)
        while bi < len(beats) and beats[bi][0] <= t_end:
            t_b, rec = beats[bi]
            nxt = beats[bi + 1][0] if bi + 1 < len(beats) else t_b + 0.47
            b = clock.beat(t_b)
            errs.append((t_b, (b - round(b)) * clock.period * 1000, 60 / (nxt - t_b),
                         clock.bpm, rec))
            bi += 1
    return clock, an, events, errs


def summarise(errs, t_from, t_to):
    sel = [e for e in errs if t_from <= e[0] < t_to]
    if not sel:
        return None
    ms = np.array([e[1] for e in sel])
    bpm_err = np.array([e[3] - e[2] for e in sel])
    return {"n": len(sel), "mean_ms": ms.mean(), "p95_abs_ms": np.percentile(np.abs(ms), 95),
            "max_abs_ms": np.abs(ms).max(), "bpm_err_mean": bpm_err.mean(),
            "bpm_err_p95": np.percentile(np.abs(bpm_err), 95)}


def main():
    mix, truth = make_mix()
    clock, an, events, errs = run(mix, truth)
    a, b = truth["a_beats"], truth["b_beats"]
    sections = {
        "A groove (bars 4-15)": (a[16], a[64]),
        "A after drop (bars 33-63)": (a[33 * 4], a[64 * 4]),
        "crossfade A->B": (a[64 * 4], b[16 * 4]),
        "B solo (bars 16-31)": (b[16 * 4], b[32 * 4]),
        "B pitch ride 126.5->128.5": (b[32 * 4], b[40 * 4]),
        "B after ride (bars 41-47)": (b[41 * 4], b[-1]),
    }
    print(f"audio: {mix.size / SR:.1f} s")
    for name, (t0, t1) in sections.items():
        s = summarise(errs, t0, t1)
        print(f"{name:30s} n={s['n']:3d}  phase mean {s['mean_ms']:+6.1f} ms  "
              f"p95 |err| {s['p95_abs_ms']:5.1f} ms  max {s['max_abs_ms']:5.1f} ms  "
              f"bpm err mean {s['bpm_err_mean']:+.2f} p95 {s['bpm_err_p95']:.2f}")
    print("events:")
    for ev in events:
        print(f"   {ev['type']:10s} at t={ev['t']:7.2f}s  beat {ev['beat']}  {ev}")
    print(f"truth: breakdown at {truth['a_breakdown']:.2f}s, drop at {truth['a_drop']:.2f}s, "
          f"B starts {b[0]:.2f}s (B solo from {b[16 * 4]:.2f}s)")
    return errs, events, truth, sections


if __name__ == "__main__":
    main()
