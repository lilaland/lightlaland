"""Synthetic records away from house tempo, with known beats, for testing the analyzer's tempo
range offline: slow house, hip-hop, drum & bass, and quick blends between records at different
tempos. Uses vinyl_sim's drum sounds; each record drifts a little (pitch fader) and gets vinyl
rumble, hiss and crackle.
"""
import numpy as np

from tests.vinyl_sim import SR, bass, beat_times, clap, hat, kick, place, snare


def sub(freq, dur):
    """Drum & bass sub note, held for dur seconds."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    return 0.3 * np.sin(2 * np.pi * freq * t) * np.minimum(1.0, t / 0.01) * np.exp(-t * 1.5)


def house(bpm):
    """Four on the floor: kick every beat, off-beat hat and bass, clap on 2 and 4."""
    K, H, C, B = kick(), hat(), clap(), bass(55.0)
    bar = ([(K, b, 1.0) for b in range(4)] + [(H, b + 0.5, 1.0) for b in range(4)]
           + [(B, b + 0.5, 1.0) for b in range(4)] + [(C, 1, 1.0), (C, 3, 1.0)])
    return lambda n: bar


def hiphop(bpm):
    """Kick on 1 and the "and" of 3, snare on 2 and 4, swung 8th hats, bass under the kicks."""
    K, H, S = kick(), hat(), snare()
    basses = [bass(f) for f in (49.0, 49.0, 55.0, 41.2)]
    hats = [(H, b + o, g) for b in range(4) for o, g in ((0.0, 1.0), (0.58, 0.6))]
    return lambda n: ([(K, 0, 1.0), (K, 2.5, 0.9), (S, 1, 1.3), (S, 3, 1.3)] + hats
                      + [(basses[n % 4], 0, 1.0), (basses[n % 4], 2.5, 0.8)])


def dnb(bpm):
    """Two-step: kick on 1 and the "and" of 3, big snare on 2 and 4, 16th hats, sub bass under
    the kicks."""
    K, H, HB, S = kick(), hat(), hat(True), snare()
    beat = 60.0 / bpm
    subs = [(sub(f, 2.5 * beat), sub(f * 0.75, 1.5 * beat)) for f in (55.0, 49.0, 43.65, 41.2)]
    hats = [(H, s / 4, 1.0 if s % 2 == 0 else 0.5) for s in range(16)]
    hats += [(HB, b + 0.5, 0.5) for b in range(4)]
    return lambda n: ([(K, 0, 1.0), (K, 2.5, 1.0), (S, 1, 1.6), (S, 3, 1.6)] + hats
                      + [(subs[n % 4][0], 0, 1.0), (subs[n % 4][1], 2.5, 1.0)])


STYLES = {"house": house, "hiphop": hiphop, "dnb": dnb}   # tempo -> (bar number -> hits)


def record_beats(bpm, bars, start, drift):
    """Beat times of a record whose pitch drifts by `drift` BPM over its length (one extra beat
    closes the last bar)."""
    n = bars * 4
    return beat_times(start, n + 1, lambda t, k: bpm + drift * k / n)


def render(style, beats, size):
    """Place every bar's hits (sound, position in beats, gain) on the beat times."""
    out = np.zeros(size)
    grid = np.arange(beats.size)
    for bar in range((beats.size - 1) // 4):
        for sound, pos, g in style(bar):
            place(out, sound, float(np.interp(4 * bar + pos, grid, beats)), g)
    return out


def vinyl(mix, seed):
    """Add rumble, hiss and crackle, and set the peak to -6 dBFS."""
    rng = np.random.default_rng(seed)
    t = np.arange(mix.size) / SR
    mix = mix + 0.02 * np.sin(2 * np.pi * 18 * t) + rng.normal(0, 0.003, mix.size)
    clicks = rng.integers(0, mix.size, int(mix.size / SR * 20))
    mix[clicks] += rng.normal(0, 0.08, clicks.size)
    return (mix * 0.5 / np.max(np.abs(mix))).astype(np.float32)


def make_record(kind, bpm, bars=24, drift=0.3, seed=11):
    """One record from the start. Returns (audio, [(beat time, "A")])."""
    beats = record_beats(bpm, bars, 0.5, drift)
    mix = render(STYLES[kind](bpm), beats, int((beats[-1] + 1.0) * SR))
    return vinyl(mix, seed), [(t, "A") for t in beats[:-1]]


def make_blend(kind_a, bpm_a, kind_b, bpm_b, a_bars=12, fade_s=8.0, b_bars=24, seed=12):
    """Record A, then an equal-power crossfade of fade_s seconds into record B at its own tempo
    (not beat-matched). The truth follows A until B is the louder one, then B.
    Returns (audio, [(beat time, "A" or "B")], fade start time)."""
    a = record_beats(bpm_a, a_bars + int(np.ceil(fade_s * bpm_a / 240)) + 1, 0.5, 0.2)
    fade_start = a[a_bars * 4]
    b = record_beats(bpm_b, b_bars, fade_start + 0.137, -0.2)
    size = int((b[-1] + 1.0) * SR)
    t = np.arange(size) / SR
    x = np.clip((t - fade_start) / fade_s, 0.0, 1.0)
    mix = (render(STYLES[kind_a](bpm_a), a, size) * np.cos(0.5 * np.pi * x)
           + render(STYLES[kind_b](bpm_b), b, size) * np.sin(0.5 * np.pi * x))
    switch = fade_start + fade_s / 2
    truth = ([(tb, "A") for tb in a[:-1] if tb < switch]
             + [(tb, "B") for tb in b[:-1] if tb >= switch])
    return vinyl(mix, seed), truth, fade_start
