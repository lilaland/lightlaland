"""Beat clock: the shared musical timeline (tempo, beat phase, bars, phrases).

The analyzer steers it from the audio; the Tap and Downbeat pads steer it by hand.
All times are time.perf_counter() seconds.
"""
import math
import threading
import time


class BeatClock:
    def __init__(self, bpm=124.0, phrase_bars=16, now=None):
        self._lock = threading.RLock()
        t = time.perf_counter() if now is None else now
        self.period = 60.0 / bpm
        self.t0 = t              # time of beat 0
        self.anchor = 0          # beat index where a phrase (and bar) starts
        self.phrase_bars = phrase_bars
        self.source = "internal"  # internal | audio | tap
        self.follow_audio = True
        self._taps = []

    @property
    def bpm(self):
        return 60.0 / self.period

    def beat(self, t):
        return (t - self.t0) / self.period

    def time_of(self, beat):
        return self.t0 + beat * self.period

    def info(self, t):
        with self._lock:
            b = self.beat(t)
            n = math.floor(b)
            bar, beat_in_bar = divmod(n - self.anchor, 4)
            phrase, bar_in_phrase = divmod(bar, self.phrase_bars)
            return {
                "beat": b, "beat_index": n, "phase": b - n, "anchor": self.anchor,
                "beat_in_bar": beat_in_bar, "bar": bar, "bar_in_phrase": bar_in_phrase,
                "phrase": phrase, "phrase_bars": self.phrase_bars,
                "bpm": self.bpm, "source": self.source,
            }

    # --- steering -----------------------------------------------------------
    def set_tempo(self, bpm, t):
        """Change tempo without moving the beat position at time t."""
        with self._lock:
            b = self.beat(t)
            self.period = 60.0 / bpm
            self.t0 = t - b * self.period

    def align(self, t, gain=1.0):
        """Pull the nearest grid beat towards t (gain 1 = snap)."""
        with self._lock:
            b = self.beat(t)
            self.t0 += (b - round(b)) * gain * self.period

    def nudge(self, beats):
        """Shift the grid later (+) or earlier (-) by a fraction of a beat."""
        with self._lock:
            self.t0 += beats * self.period

    def tap(self, t):
        """Tap tempo: median of recent tap intervals; each tap also snaps the phase."""
        with self._lock:
            if self._taps and t - self._taps[-1] > 2.0:
                self._taps = []
            self._taps = (self._taps + [t])[-8:]
            if len(self._taps) >= 3:
                gaps = sorted(b - a for a, b in zip(self._taps, self._taps[1:]))
                bpm = 60.0 / gaps[len(gaps) // 2]
                if 60 <= bpm <= 200:
                    self.set_tempo(bpm, t)
            self.align(t)
            self.source = "tap"

    def downbeat(self, t):
        """Mark 'this is a 1': fix bar alignment, keeping the phrase count as close as possible."""
        with self._lock:
            if self.source != "audio":
                self.align(t)
            n = round(self.beat(t))
            off = (n - self.anchor) % 4
            self.anchor += off if off <= 2 else off - 4

    def phrase_start(self, beat_index):
        with self._lock:
            self.anchor = int(beat_index)
