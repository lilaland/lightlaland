"""Listen to the mix: steer the BeatClock and detect breakdowns, drops and record changes.

Built for a vinyl house/techno mix fed in as line audio:

* The onset envelope is spectral flux, mostly from the kick band (35-150 Hz), normalised by a
  slow peak follower so the input level doesn't matter.
* Tempo comes from the envelope's autocorrelation, scored at 1-8 beat multiples for precision.
  The search range is kept under 2:1 (default 108-150 BPM, i.e. +/-8% pitch on 125 BPM
  records) so double/half-tempo mistakes can't happen.
* Phase comes from a comb filter over the last ~16 beats, then nudges the clock like a PLL,
  so slow pitch-fader drift and the odd platter nudge are followed smoothly.
* A breakdown is 6+ beats without a kick; the first strong kick after it is the drop, which
  also resets the phrase count (drops land on the 1 of a phrase).
* A record change is guessed from a jump in the 16-bar average spectrum. Treat it as a hint.
"""
import collections
import math
import threading

import numpy as np


class Analyzer:
    HOP = 512
    WIN = 2048
    EST_EVERY = 8           # hops between tempo/phase estimates (~85 ms at 48 kHz)
    HISTORY_S = 8.0
    TIME_OFFSET = -0.005    # s; maps a flux frame to the kick onset (calibrated by the tests)
    ACF_MIN = 0.12          # minimum tempo evidence to steer the clock
    SILENCE_DB = -70.0      # below this the needle is up / faders are down
    CONF_MIN = 2.0          # minimum phase evidence (z-score of the comb peak)
    MULTS = ((1, 1.0), (2, 0.9), (3, 0.5), (4, 0.8), (6, 0.4), (8, 0.6))

    def __init__(self, sr, clock, bpm_min=108.0, bpm_max=150.0, on_event=None, song_detect=True):
        self.sr = float(sr)
        self.clock = clock
        self.on_event = on_event or (lambda ev: None)
        self.song_detect = song_detect
        self.fr = self.sr / self.HOP
        self.set_range(bpm_min, bpm_max)

        self.window = np.hanning(self.WIN).astype(np.float32)
        self.buf = np.zeros(self.WIN, np.float32)
        self.pending = np.zeros(0, np.float32)
        f = np.fft.rfftfreq(self.WIN, 1.0 / self.sr)
        self.bands = np.stack([(f >= 35) & (f < 150), (f >= 150) & (f < 2000),
                               (f >= 2000) & (f < 12000)]).astype(np.float64)
        edges = np.geomspace(40.0, 16000.0, 9)
        self.tbands = np.stack([(f >= lo) & (f < hi)
                                for lo, hi in zip(edges[:-1], edges[1:])]).astype(np.float64)

        n = int(self.HISTORY_S * self.fr)
        self.n_hist = n
        self.h_flux = np.zeros((n, 3))
        self.h_time = np.zeros(n)
        self.h_tim = np.zeros((n, 8))
        self.h_i = 0
        self.h_count = 0
        self.e_ref = None
        self.prev_e = None
        self.hops = 0

        self.level_db = -120.0
        self.kick_vis = 0.0
        self.locked = False
        self.est = {"bpm": None, "acf": 0.0, "conf": 0.0}
        self.est_log = collections.deque(maxlen=20000)  # (beat_time, bpm, acf, conf, locked) for tests
        self._jump = None            # (bpm, since) while a big tempo change is being confirmed

        self.last_beat = None
        self.kicks = collections.deque(maxlen=128)   # kick strength on groove beats only
        self.hmd = collections.deque(maxlen=64)   # mid+high onset density per beat
        self.beat_tim = collections.deque(maxlen=4)
        self.bar_tim = collections.deque(maxlen=48)
        self.novelty = collections.deque(maxlen=64)
        self.state = "silent"        # silent | groove | breakdown
        self.absent = 0
        self.prev_present = False
        self.bd_hm0 = 0.0
        self.build = 0.0
        self.last_struct_beat = -10**9
        self.last_song_beat = -10**9
        self.lock = threading.Lock()

    def set_range(self, bpm_min, bpm_max):
        lo, hi = float(bpm_min), float(bpm_max)
        if hi / lo >= 2.0:  # keep the range under an octave so tempo can't double or halve
            hi = lo * 1.99
        self.bpm_min, self.bpm_max = lo, hi
        self.grid = np.arange(lo, hi + 1e-9, 0.05)
        self.lags = 60.0 * self.fr / self.grid

    # --- input -----------------------------------------------------------------
    def process(self, block, t_end):
        """Feed mono samples; t_end is the clock time of the block's last sample."""
        block = np.asarray(block, np.float32).ravel()
        data = np.concatenate([self.pending, block]) if self.pending.size else block
        n_hops = data.size // self.HOP
        for h in range(n_hops):
            hop = data[h * self.HOP:(h + 1) * self.HOP]
            self.buf = np.concatenate([self.buf[self.HOP:], hop])
            self._hop(t_end - (data.size - (h + 1) * self.HOP) / self.sr)
        self.pending = data[n_hops * self.HOP:].copy()

    def _hop(self, t_end):
        spec = np.abs(np.fft.rfft(self.buf * self.window)) ** 2
        e = self.bands @ spec
        if self.e_ref is None:
            self.e_ref = e + 1e-12
            self.prev_e = e
        self.e_ref = np.maximum(self.e_ref * 0.9995, e)          # peak follower, ~20 s release
        flux = np.maximum(0.0, e - self.prev_e) / (self.e_ref + 1e-12)
        self.prev_e = e

        rms = float(np.sqrt(np.mean(self.buf[-self.HOP:] ** 2)) + 1e-9)
        self.level_db = max(20 * math.log10(rms), self.level_db - 0.6)  # meter with fall-off
        self.kick_vis = max(float(flux[0]), self.kick_vis * 0.85)

        i = self.h_i
        self.h_flux[i] = flux
        self.h_time[i] = t_end - (self.WIN / 2) / self.sr + self.TIME_OFFSET
        self.h_tim[i] = np.log(self.tbands @ spec + 1e-10)
        # Spectrum display: each band relative to its own recent peak, ~26 dB of range.
        self.band_peak = np.maximum(getattr(self, "band_peak", self.h_tim[i]) - 0.002, self.h_tim[i])
        self.band_vis = np.maximum(np.clip(1.0 - (self.band_peak - self.h_tim[i]) / 6.0, 0.0, 1.0),
                                   getattr(self, "band_vis", 0.0) * 0.82)
        self.h_i = (i + 1) % self.n_hist
        self.h_count = min(self.h_count + 1, self.n_hist)
        self.hops += 1

        if self.hops % self.EST_EVERY == 0:
            res = self._estimate()
            if res is not None:
                self._steer(*res)
        self._beats(self.h_time[i])

    def _chrono(self, arr):
        """History in time order (oldest first)."""
        if self.h_count < self.n_hist:
            return arr[:self.h_count]
        return np.concatenate([arr[self.h_i:], arr[:self.h_i]])

    # --- tempo and phase ------------------------------------------------------------
    def _estimate(self):
        if self.h_count < int(4.0 * self.fr):
            return None
        flux = self._chrono(self.h_flux)
        times = self._chrono(self.h_time)
        o = flux[:, 0] + 0.25 * flux[:, 1]
        n = o.size
        x = o - o.mean()
        nfft = 1 << (2 * n - 1).bit_length()
        spec = np.fft.rfft(x, nfft)
        r = np.fft.irfft(spec * np.conj(spec), nfft)[:n]
        if r[0] <= 1e-12:
            return None
        r /= r[0]
        idx = np.arange(n, dtype=np.float64)
        score = np.zeros_like(self.grid)
        wsum = 0.0
        for k, w in self.MULTS:
            lag = self.lags * k
            ok = lag < n - 1
            score[ok] += w * np.interp(lag[ok], idx, r)
            wsum += w
        i = int(np.argmax(score))
        bpm = self.grid[i]
        if 0 < i < score.size - 1:
            a, b, c = score[i - 1], score[i], score[i + 1]
            den = a - 2 * b + c
            if den < 0:
                bpm += 0.5 * (a - c) / den * (self.grid[1] - self.grid[0])
        acf = float(score[i] / wsum)

        period = 60.0 * self.fr / bpm
        env = np.convolve(o, (0.25, 0.5, 0.25), mode="same")
        k = max(1, min(16, int((n - 1) // period)))
        phis = np.arange(0.0, period, 0.25)
        pos = (n - 1) - phis[:, None] - period * np.arange(k)[None, :]
        s = np.interp(pos, idx, env) @ (0.9 ** np.arange(k))
        j = int(np.argmax(s))
        conf = float((s[j] - np.median(s)) / (s.std() + 1e-9))
        beat_time = float(times[-1] - phis[j] / self.fr)
        self.est = {"bpm": float(bpm), "acf": acf, "conf": conf}
        return float(bpm), beat_time, acf, conf

    def _steer(self, bpm, beat_time, acf, conf):
        clk = self.clock
        self.locked = (acf > self.ACF_MIN and conf > self.CONF_MIN
                       and self.state == "groove" and self.level_db > self.SILENCE_DB)
        self.est_log.append((beat_time, bpm, acf, conf, self.locked))
        if not self.locked or not clk.follow_audio:
            return  # flywheel: keep the current grid running
        if clk.source != "audio":
            clk.set_tempo(bpm, beat_time)
            clk.align(beat_time, 1.0)
            clk.source = "audio"
            return
        cur = clk.bpm
        if abs(bpm - cur) / cur < 0.015:
            clk.set_tempo(cur + 0.2 * (bpm - cur), beat_time)
            self._jump = None
        else:
            # Big disagreement (new record at a different pitch, or a glitch): believe it
            # only after it holds steady for 2 s.
            if self._jump is None or abs(bpm - self._jump[0]) / bpm > 0.01:
                self._jump = (bpm, beat_time)
                return
            if beat_time - self._jump[1] < 2.0:
                return
            clk.set_tempo(bpm, beat_time)
            clk.align(beat_time, 1.0)
            self._jump = None
            return
        b = clk.beat(beat_time)
        err = b - round(b)
        clk.align(beat_time, 0.3 if abs(err) < 0.2 else 0.15)

    # --- per-beat structure ---------------------------------------------------------
    def _beats(self, t_latest):
        clk = self.clock
        n_now = math.floor(clk.beat(t_latest - 0.075))
        if self.last_beat is None or n_now - self.last_beat > 8:
            self.last_beat = n_now
            return
        while self.last_beat < n_now:
            self.last_beat += 1
            self._on_beat(self.last_beat)

    def _emit(self, kind, beat, **extra):
        ev = {"type": kind, "beat": int(beat), "t": self.clock.time_of(beat), **extra}
        self.on_event(ev)

    def _on_beat(self, n):
        clk = self.clock
        tb = clk.time_of(n)
        period = clk.period
        times = self._chrono(self.h_time)
        flux = self._chrono(self.h_flux)
        win = 0.07 if self.state == "breakdown" else 0.045
        near = (times >= tb - win) & (times <= tb + win)
        prev = (times >= tb - period) & (times < tb)
        if not near.any() or not prev.any():
            return
        kick = float(flux[near, 0].max())
        self.hmd.append(float(flux[prev, 1].mean() + flux[prev, 2].mean()))
        self.beat_tim.append(self._chrono(self.h_tim)[prev].mean(axis=0))

        if self.level_db < self.SILENCE_DB:
            if self.state != "silent":
                self.state = "silent"
                self._emit("silence", n)
            return
        if self.state == "silent":
            self.state = "groove"
            self.absent = 0
            self._emit("music", n)
        if clk.follow_audio and clk.source != "audio":
            return  # the grid isn't from the music yet, so per-beat kick readings mean nothing
        if self.state != "breakdown":
            self.kicks.append(kick)
        if len(self.kicks) < 16:
            return
        ref = float(np.percentile(np.asarray(self.kicks), 80))
        present = ref > 0.05 and kick > 0.35 * ref

        if self.state == "groove":
            self.absent = 0 if present else self.absent + 1
            if self.absent >= 6:
                self.state = "breakdown"
                self.bd_hm0 = float(np.mean(list(self.hmd)[-6:])) + 1e-6
                self.build = 0.0
                self.prev_present = False
                self.last_struct_beat = n
                self._emit("breakdown", n - 5)
        elif self.state == "breakdown":
            self.build = float(np.clip((np.mean(list(self.hmd)[-4:]) / self.bd_hm0 - 1.0) / 1.5,
                                       0.0, 1.0))
            if present:
                on_the_one = (n - clk.anchor) % 4 == 0
                if kick > 0.6 * ref and on_the_one:
                    self._drop(n)
                elif self.prev_present:
                    self._drop(n - 1)
                else:
                    self.prev_present = True
            else:
                self.prev_present = False

        if (n - clk.anchor) % 4 == 3:
            self._bar_done(n)

    def _drop(self, n):
        self.state = "groove"
        self.absent = 0
        self.build = 0.0
        self.last_struct_beat = n
        self.clock.phrase_start(n)
        self._emit("drop", n)

    def _bar_done(self, n):
        if len(self.beat_tim) < 4:
            return
        self.bar_tim.append(np.mean(np.asarray(self.beat_tim), axis=0))
        if not self.song_detect or self.state != "groove" or len(self.bar_tim) < 32:
            return
        if n - self.last_struct_beat < 32 * 4:
            return  # both 16-bar windows must be clear of the last breakdown/drop
        bars = np.asarray(self.bar_tim)
        d = float(np.mean(np.abs(bars[-16:].mean(axis=0) - bars[-32:-16].mean(axis=0))))
        # Baseline from 16+ bars ago, so a slow blend can't raise its own threshold.
        older = list(self.novelty)[:-16]
        typical = float(np.median(older)) if len(older) >= 8 else 0.1
        self.novelty.append(d)
        if n - self.last_song_beat > 32 * 4 and d > max(0.3, 3.0 * typical):
            self.last_song_beat = n
            self._emit("song", n, novelty=round(d, 3))

    def force(self, state):
        """The DJ overrode the structure with a pad (Drop / Break)."""
        self.state = state
        self.absent = 0
        self.prev_present = False
        self.build = 0.0
        if self.last_beat is not None:
            self.last_struct_beat = self.last_beat
        if state == "breakdown":
            self.bd_hm0 = float(np.mean(list(self.hmd)[-6:])) + 1e-6 if self.hmd else 1.0

    # --- UI ------------------------------------------------------------------------------
    def status(self):
        return {"state": self.state, "locked": self.locked, "level_db": round(self.level_db, 1),
                "kick": round(min(1.0, self.kick_vis), 3), "build": round(self.build, 3),
                "bpm_est": None if self.est["bpm"] is None else round(self.est["bpm"], 2),
                "acf": round(self.est["acf"], 3), "conf": round(self.est["conf"], 2),
                "bands": [round(float(v), 2) for v in getattr(self, "band_vis", np.zeros(8))]}
