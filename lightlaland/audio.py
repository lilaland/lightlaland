"""Audio sources feeding the analyzer: a sound-card input, or a WAV file played in real time."""
import collections
import queue
import threading
import time
import wave

import numpy as np

try:
    import sounddevice as sd
    SD_ERROR = None
except Exception as exc:  # package missing, or no PortAudio library
    sd = None
    SD_ERROR = str(exc)


def list_inputs():
    if sd is None:
        return []
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            out.append({"index": i, "name": d["name"], "channels": d["max_input_channels"],
                        "sr": d["default_samplerate"]})
    return out


class AudioInput:
    """Sound-card input. Analysis runs on a worker thread, never in the audio callback."""

    def __init__(self, make_analyzer):
        self.make_analyzer = make_analyzer  # sample rate -> Analyzer
        self.analyzer = None
        self.stream = None
        self.q = queue.Queue(maxsize=400)
        self.running = False
        self.error = None if sd else f"sounddevice unavailable: {SD_ERROR}"
        self.name = None
        self.sr = None
        self._offsets = collections.deque(maxlen=400)
        self._frames = 0
        self.dropped = 0

    def start(self, device=None):
        self.stop()
        if sd is None:
            return False
        try:
            info = sd.query_devices(device, "input")
            self.sr = int(info["default_samplerate"])
            channels = max(1, min(2, int(info["max_input_channels"])))
            self.analyzer = self.make_analyzer(self.sr)
            self._frames = 0
            self._offsets.clear()
            self.stream = sd.InputStream(device=device, channels=channels, samplerate=self.sr,
                                         blocksize=512, dtype="float32", latency="low",
                                         callback=self._callback)
            self.running = True
            threading.Thread(target=self._work, daemon=True, name="analyzer").start()
            self.stream.start()
            self.name = info["name"]
            self.error = None
            return True
        except Exception as exc:
            self.running = False
            self.error = str(exc)
            return False

    def _callback(self, indata, frames, time_info, status):
        now = time.perf_counter()
        self._frames += frames
        # Timestamp of the newest sample: the callback fires a variable time after the
        # audio was captured, so use the earliest arrival seen recently (de-jittered).
        self._offsets.append(now - self._frames / self.sr)
        t_end = min(self._offsets) + self._frames / self.sr
        mono = indata.mean(axis=1)
        try:
            self.q.put_nowait((mono.copy(), t_end))
        except queue.Full:
            self.dropped += 1

    def _work(self):
        while self.running:
            try:
                block, t_end = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            self.analyzer.process(block, t_end)

    def stop(self):
        self.running = False
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None


def read_wav(path):
    """WAV (16/24/32-bit PCM) -> mono float32, sample rate."""
    with wave.open(path, "rb") as w:
        sr, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        x = (np.where(v >= 1 << 23, v - (1 << 24), v)).astype(np.float32) / float(1 << 23)
    elif width == 4:
        x = np.frombuffer(raw, "<i4").astype(np.float32) / float(1 << 31)
    else:
        raise ValueError(f"unsupported WAV sample width: {width} bytes")
    return x.reshape(-1, ch).mean(axis=1), sr


class WavInput:
    """Plays a WAV file (if an output device exists) and feeds it to the analyzer in real time."""

    def __init__(self, path, make_analyzer):
        self.path = path
        self.make_analyzer = make_analyzer
        self.analyzer = None
        self.running = False
        self.error = None
        self.name = f"file: {path}"
        self.sr = None
        self.dropped = 0

    def start(self, device=None):
        self.stop()
        try:
            data, self.sr = read_wav(self.path)
        except Exception as exc:
            self.error = str(exc)
            return False
        self.analyzer = self.make_analyzer(self.sr)
        self.running = True
        threading.Thread(target=self._feed, args=(data,), daemon=True, name="wav").start()
        return True

    def _feed(self, data):
        if sd is not None:
            try:
                sd.play(data, self.sr)
            except Exception as exc:  # no output device: analyse silently
                self.error = f"playback: {exc}"
        hop = 512
        start = time.perf_counter()
        for i in range(0, data.size - hop, hop):
            if not self.running:
                break
            t_end = start + (i + hop) / self.sr
            delay = t_end - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            self.analyzer.process(data[i:i + hop], t_end)
        self.running = False

    def stop(self):
        self.running = False
        if sd is not None:
            try:
                sd.stop()
            except Exception:
                pass
