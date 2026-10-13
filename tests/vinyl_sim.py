"""Synthetic two-record vinyl mix with known beats, for testing the analyzer offline.

Record A: 126 BPM drifting to 126.4 (pitch fader), groove / 16-bar breakdown with riser and
snare roll / drop / outro. Record B is beat-matched by ear (0.1 BPM fast, 12 ms late),
crossfaded in over 16 bars, has a different sound (claps, brighter hats, new bassline) and
later gets its pitch ridden up to 128.5 BPM. Rumble, crackle and hiss on top.
"""
import numpy as np

SR = 48000


def _env(n, decay):
    t = np.arange(n) / SR
    return np.exp(-t * decay)


def kick():
    n = int(0.35 * SR)
    t = np.arange(n) / SR
    f = 48 + 110 * np.exp(-t * 35)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 7.5) * (1 - np.exp(-t * 2000))
    click = np.zeros(n)
    m = int(0.003 * SR)
    click[:m] = np.random.default_rng(1).normal(0, 0.25, m)
    return 0.9 * body + click


def hat(bright=False):
    n = int(0.06 * SR)
    x = np.random.default_rng(2 + bright).normal(0, 1, n)
    x = np.diff(x, prepend=0)
    if bright:
        x = np.diff(x, prepend=0) * 0.6
    return (0.25 if bright else 0.18) * x * _env(n, 70) / 2


def clap():
    n = int(0.12 * SR)
    x = np.random.default_rng(4).normal(0, 1, n)
    x = np.convolve(np.diff(x, prepend=0), np.ones(6) / 6, mode="same")
    e = np.zeros(n)
    for off in (0.0, 0.011, 0.022):
        i = int(off * SR)
        e[i:] += _env(n - i, 45)
    return 0.3 * x * e / 3


def snare():
    n = int(0.15 * SR)
    t = np.arange(n) / SR
    x = np.random.default_rng(5).normal(0, 1, n) * 0.5 + np.sin(2 * np.pi * 200 * t)
    return 0.22 * x * _env(n, 30)


def bass(freq):
    n = int(0.2 * SR)
    t = np.arange(n) / SR
    return 0.35 * np.tanh(2 * np.sin(2 * np.pi * freq * t)) * _env(n, 9)


def beat_times(start, n_beats, bpm_at):
    """Beat times when the tempo is a function of time (pitch fader moves)."""
    out, t = [], start
    for k in range(n_beats):
        out.append(t)
        t += 60.0 / bpm_at(t, k)
    return np.array(out)


def place(buf, sample, t, gain=1.0):
    i = int(round(t * SR))
    if i >= buf.size or gain <= 0:
        return
    j = min(buf.size, i + sample.size)
    buf[i:j] += gain * sample[:j - i]


def make_mix():
    a_beats = beat_times(0.5, 80 * 4, lambda t, k: 126.0 + 0.4 * k / 320)
    b_start = a_beats[64 * 4] + 0.012                      # cued 12 ms late
    b_beats = beat_times(b_start, 48 * 4,
                         lambda t, k: 126.5 + 2.0 * np.clip((k - 128) / 32, 0, 1))
    dur = b_beats[-1] + 2.0
    mix = np.zeros(int(dur * SR) + 1)

    K, H, HB, C, S = kick(), hat(), hat(True), clap(), snare()
    BA, BB = bass(55.0), bass(49.0)

    a_gain = lambda bar: 1.0 if bar < 64 else max(0.0, 1 - (bar - 64) / 16)
    for k, t in enumerate(a_beats):
        bar, beat = divmod(k, 4)
        g = a_gain(bar)
        nxt = a_beats[k + 1] if k + 1 < a_beats.size else t + 60 / 126
        off = t + (nxt - t) / 2
        if 16 <= bar < 32:                                    # breakdown
            if bar >= 28:                                     # snare roll, getting denser
                div = 2 ** min(3, bar - 28)
                for s in range(div):
                    place(mix, S, t + (nxt - t) * s / div, 0.5 + 0.5 * (bar - 28) / 4)
            continue
        place(mix, K, t, g)
        place(mix, H, off, g)
        place(mix, BA, off, g)

    # pads + riser across A's breakdown
    t0, t1 = a_beats[16 * 4], a_beats[32 * 4]
    i0, i1 = int(t0 * SR), int(t1 * SR)
    tt = np.arange(i1 - i0) / SR
    pad = sum(np.sin(2 * np.pi * f * tt) for f in (220.0, 277.18, 329.63)) * 0.06
    rng = np.random.default_rng(7)
    ramp = np.clip((tt - (t1 - t0) / 2) / ((t1 - t0) / 2), 0, 1)
    riser = np.diff(rng.normal(0, 1, tt.size), prepend=0) * 0.12 * ramp ** 2
    mix[i0:i1] += pad * (0.6 + 0.4 * np.sin(2 * np.pi * 0.25 * tt)) + riser

    b_gain = lambda bar: min(1.0, 0.3 + 0.7 * bar / 8)
    for k, t in enumerate(b_beats):
        bar, beat = divmod(k, 4)
        g = b_gain(bar)
        nxt = b_beats[k + 1] if k + 1 < b_beats.size else t + 60 / 127
        off = t + (nxt - t) / 2
        place(mix, K, t, g)
        place(mix, HB, off, g)
        place(mix, HB, t + (nxt - t) * 0.75, 0.6 * g)
        place(mix, BB, t + (nxt - t) * 0.25, g)
        if beat in (1, 3):
            place(mix, C, t, g)

    rng = np.random.default_rng(9)
    t_all = np.arange(mix.size) / SR
    mix += 0.02 * np.sin(2 * np.pi * 18 * t_all)                  # rumble
    mix += rng.normal(0, 0.003, mix.size)                         # hiss
    clicks = rng.integers(0, mix.size, int(dur * 20))              # crackle
    mix[clicks] += rng.normal(0, 0.08, clicks.size)
    mix *= 0.5 / np.max(np.abs(mix))
    truth = {
        "a_beats": a_beats, "b_beats": b_beats,
        "a_breakdown": a_beats[16 * 4], "a_drop": a_beats[32 * 4],
        "a_gain": a_gain, "b_gain": b_gain,
    }
    return mix.astype(np.float32), truth
