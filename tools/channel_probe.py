"""Webcam channel probe: light one DMX channel at a time and record what the camera sees.

The light is driven through the running app's channel-test API, so the app stays the only
Art-Net sender (the show pauses and the other light goes dark). Frames come from the Mac's
camera through OpenCV.

macOS only lets apps with camera permission open the camera, and Claude Code's shell has
none. So run `serve` once from a terminal that has it (Terminal.app); every other command
then fetches frames from that server when it's up, or opens the camera itself when not.

    .venv/bin/python tools/channel_probe.py serve                   # hold the camera (Terminal.app)
    .venv/bin/python tools/channel_probe.py cams                    # a still from each camera
    .venv/bin/python tools/channel_probe.py snap                    # one still (app not needed)
    .venv/bin/python tools/channel_probe.py snap 28=40 29=40        # light channels: still + diff
    .venv/bin/python tools/channel_probe.py scan --from 4 --to 83   # every channel, one at a time
    .venv/bin/python tools/channel_probe.py sweep 1 0:255:15 --light 76=30
    .venv/bin/python tools/channel_probe.py stop                    # leave test mode (show resumes)

Output goes to probe/<A|B>/...: per-channel diff images, contact.png (everything on one
labelled sheet) and results.json. CH84 (reset) is never sent; the app refuses it anyway.
"""
import argparse
import io
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

try:
    import cv2
except ImportError:
    sys.exit("needs OpenCV:  .venv/bin/pip install opencv-python")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAST_CH = 83          # CH84 is reset
FIRST_SCAN = 4        # CH1-3 move the bar; probe those with sweep
MIN_AREA = 25         # pixels; smaller diffs count as not visible
SHEET_W = 1800
SERVER = "http://127.0.0.1:8091"
STATS = {"median": np.median, "max": np.max}


# --- the light, through the app's test mode ----------------------------------------------
class Light:
    """One fixture held in the app's channel-test mode."""

    def __init__(self, url, fixture, base=()):
        self.url = url.rstrip("/") + "/api/cmd"
        self.base = list(base)   # (channel, value) kept on through every clear, e.g. a position
        self.cmd(action="start", fixture=fixture)

    def cmd(self, **body):
        data = json.dumps(dict(cmd="test", **body)).encode()
        req = urllib.request.Request(self.url, data, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise SystemExit(f"the app refused {body}: {e.read().decode()}")
        except urllib.error.URLError as e:
            raise SystemExit(f"can't reach the app at {self.url} ({e.reason}). Is it running?")

    def set(self, channel, value):
        if not 1 <= channel <= LAST_CH:
            raise SystemExit(f"channel {channel} is outside 1-{LAST_CH}")
        self.cmd(action="set", channel=channel, value=int(value))

    def clear(self):
        self.cmd(action="clear")   # channels 2-83 back to 0; CH1 stays where it is
        for ch, v in self.base:
            self.set(ch, v)


# --- the camera ------------------------------------------------------------------------------
class Camera:
    """Reads frames in a thread so a grab gets fresh frames, never a stale buffered one."""

    def __init__(self, index, width=1280, height=720, warmup=1.5):
        self.cap = None
        for attempt in range(30):   # the first open fails while macOS asks for permission
            cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            if cap.isOpened() and cap.read()[0]:
                self.cap = cap
                break
            cap.release()
            if attempt == 0:
                print("waiting for camera access (click Allow if macOS asks)...", file=sys.stderr)
            time.sleep(1.0)
        if self.cap is None:
            raise SystemExit(f"camera {index} didn't open. "
                             "Check System Settings > Privacy & Security > Camera.")
        self.lock = threading.Lock()
        self.frame, self.count, self.running = None, 0, True
        threading.Thread(target=self._read, daemon=True).start()
        time.sleep(warmup)   # auto exposure settles after opening

    def _read(self):
        while self.running:
            ok, f = self.cap.read()
            if ok:
                with self.lock:
                    self.frame, self.count = f, self.count + 1

    def grab(self, n=5, stat="median", timeout=5.0):
        """Median of the next n frames (float32), which hides room-light flicker. "max" keeps
        strobe flashes that a median would throw away."""
        frames, seen, end = [], self.count, time.time() + timeout
        while len(frames) < n and time.time() < end:
            with self.lock:
                if self.count != seen:
                    seen = self.count
                    frames.append(self.frame)
            time.sleep(0.003)
        if not frames:
            raise SystemExit("the camera stopped delivering frames")
        return STATS[stat](np.stack(frames), axis=0).astype(np.float32)

    def close(self):
        self.running = False
        time.sleep(0.1)
        self.cap.release()


class RemoteCamera:
    """Frames from `serve`, running in a terminal that has camera permission."""

    def __init__(self, url):
        self.url = url.rstrip("/")
        self.grab(1)

    def grab(self, n=5, stat="median", timeout=10.0):
        with urllib.request.urlopen(f"{self.url}/grab?n={n}&stat={stat}", timeout=timeout) as r:
            return np.load(io.BytesIO(r.read()))

    def close(self):
        pass


def open_camera(spec):
    """"auto": the camera server if it's up, else the local camera 0."""
    if spec == "auto":
        try:
            return RemoteCamera(SERVER)
        except OSError:
            return Camera(0)
    if spec.startswith("http"):
        return RemoteCamera(spec)
    return Camera(int(spec))


# --- image analysis --------------------------------------------------------------------------
def colour_name(rgb):
    """Normalised diff colour -> R, G, B, W or a mix like RG. Ratios, not hue: auto white
    balance shifts hue when only one colour is lit."""
    strong = "".join(c for c, v in zip("RGB", rgb) if v > 0.6)
    return "W" if strong == "RGB" else strong


def analyse(lit, dark, thr):
    """What lit up between two frames: area, centroid and colour of (lit - dark)."""
    diff = np.clip(lit - dark, 0, None)
    lum = diff.mean(axis=2)
    mask = cv2.morphologyEx((lum > thr).astype(np.uint8), cv2.MORPH_OPEN,
                            np.ones((3, 3), np.uint8)).astype(bool)
    area = int(mask.sum())
    res = {"visible": area >= MIN_AREA, "area": area, "peak": round(float(lum.max()), 1)}
    if not res["visible"]:
        return res, diff, mask
    ys, xs = np.nonzero(mask)
    w = lum[mask]
    unclipped = mask & (lit.max(axis=2) < 250)   # clipped pixels read as white whatever the colour
    sel = unclipped if unclipped.sum() >= 20 else mask
    bgr = diff[sel].mean(axis=0)
    rgb = bgr[::-1] / max(float(bgr.max()), 1e-6)
    res.update(
        x=round(float((xs * w).sum() / w.sum()), 1), y=round(float((ys * w).sum() / w.sum()), 1),
        energy=round(float(w.sum()), 0), rgb=[round(float(v), 2) for v in rgb],
        colour=colour_name(rgb), clipped=round(1 - float(unclipped.sum()) / area, 2),
    )
    return res, diff, mask


def boosted(diff, mask=None, max_gain=12.0):
    """A diff scaled up so dim channels show; invisible ones stay honest noise."""
    lum = diff.max(axis=2)
    ref = float(np.percentile(lum[mask], 98)) if mask is not None and mask.any() else float(lum.max())
    gain = min(max_gain, 230.0 / max(ref, 1.0))
    return np.clip(diff * gain, 0, 255).astype(np.uint8)


def crop_box(mask, shape, margin=40):
    if not mask.any():
        return 0, 0, shape[1], shape[0]
    ys, xs = np.nonzero(mask)
    return (max(0, xs.min() - margin), max(0, ys.min() - margin),
            min(shape[1], xs.max() + margin + 1), min(shape[0], ys.max() + margin + 1))


def contact_sheet(tiles, box, path):
    """tiles: [(label, uint8 BGR image, swatch BGR or None)] cropped to box, on one sheet."""
    x0, y0, x1, y1 = box
    aspect = (x1 - x0) / max(1, y1 - y0)
    tw = int(min(SHEET_W, max(60, math.sqrt(SHEET_W * SHEET_W / max(1, len(tiles)) * aspect))))
    th = max(30, int(tw / aspect))
    cols = max(1, SHEET_W // (tw + 4))
    lab = 20
    rows = math.ceil(len(tiles) / cols)
    sheet = np.full((rows * (th + lab + 4), cols * (tw + 4), 3), 40, np.uint8)
    for k, (label, img, swatch) in enumerate(tiles):
        r, c = divmod(k, cols)
        ox, oy = c * (tw + 4), r * (th + lab + 4)
        sheet[oy + lab:oy + lab + th, ox:ox + tw] = cv2.resize(img[y0:y1, x0:x1], (tw, th),
                                                               interpolation=cv2.INTER_AREA)
        if swatch is not None:
            sheet[oy + 3:oy + lab - 3, ox + tw - 20:ox + tw - 4] = swatch
        cv2.putText(sheet, label, (ox + 3, oy + 15), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (230, 230, 230), 1, cv2.LINE_AA)
    cv2.imwrite(path, sheet)


def principal_axis(points, weights):
    """Main direction through the centroids, pointing right (or down for a vertical bar)."""
    p, w = np.asarray(points, float), np.asarray(weights, float)
    mean = (p * w[:, None]).sum(0) / w.sum()
    q = (p - mean) * np.sqrt(w)[:, None]
    v = np.linalg.svd(q, full_matrices=False)[2][0]
    if (abs(v[0]) >= abs(v[1]) and v[0] < 0) or (abs(v[0]) < abs(v[1]) and v[1] < 0):
        v = -v
    return mean, v


def group_channels(chans, tol):
    """Runs of consecutive channels at the same spot on the bar -> likely one segment."""
    groups = []
    for ch, r in chans:
        g = groups[-1] if groups else None
        if g and ch == g["channels"][-1] + 1 and abs(r["t"] - g["t"]) < tol \
                and r["colour"] not in g["colours"]:
            g["channels"].append(ch)
            g["colours"].append(r["colour"])
            g["t"] = round((g["t"] * (len(g["channels"]) - 1) + r["t"]) / len(g["channels"]), 3)
        else:
            groups.append({"channels": [ch], "colours": [r["colour"]], "t": r["t"]})
    return groups


# --- commands --------------------------------------------------------------------------------
def fixture_letter(a):
    return "AB"[a.fixture]


def out_dir(a, kind):
    path = os.path.join(ROOT, "probe", fixture_letter(a), f"{kind}-{time.strftime('%H%M%S')}")
    os.makedirs(path, exist_ok=True)
    return path


def move(light, pos, wait):
    light.set(1, pos)
    time.sleep(wait)   # motor travel


def parse_pairs(items):
    pairs = []
    for it in items:
        ch, _, v = it.partition("=")
        pairs.append((int(ch), int(v)))
    return pairs


def cmd_cams(a):
    os.makedirs(os.path.join(ROOT, "probe"), exist_ok=True)
    for i in range(4):
        cap = cv2.VideoCapture(i, cv2.CAP_AVFOUNDATION)
        ok = cap.isOpened()
        if ok:
            time.sleep(1.0)
            for _ in range(10):
                ok, frame = cap.read()
        cap.release()
        if ok:
            path = os.path.join(ROOT, "probe", f"camera{i}.png")
            cv2.imwrite(path, frame)
            print(f"camera {i}: {frame.shape[1]}x{frame.shape[0]} -> {path}")
        else:
            print(f"camera {i}: none")


def cmd_snap(a):
    pairs = parse_pairs(a.channels)
    cam = open_camera(a.camera)
    light = Light(a.url, a.fixture, parse_pairs(a.base)) if pairs or a.pos is not None or a.base else None
    try:
        if light:
            light.clear()
            if a.pos is not None:
                move(light, a.pos, a.move_wait)
            elif a.base:
                time.sleep(a.move_wait)   # the base may include a move
        time.sleep(a.settle)
        dark = cam.grab(a.frames)
        dark_max = cam.grab(a.flash_frames, "max") if pairs else None
        os.makedirs(os.path.join(ROOT, "probe", "snaps"), exist_ok=True)
        stem = os.path.join(ROOT, "probe", "snaps", time.strftime("%H%M%S"))
        if not pairs:
            cv2.imwrite(stem + "-still.png", dark.astype(np.uint8))
            print(stem + "-still.png")
            return
        for ch, v in pairs:
            light.set(ch, v)
        time.sleep(a.settle)
        lit = cam.grab(a.frames)
        peak = cam.grab(a.flash_frames, "max")
        res, diff, mask = analyse(lit, dark, a.threshold)
        res_max, diff_max, mask_max = analyse(peak, dark_max, a.threshold)
        cv2.imwrite(stem + "-lit.png", lit.astype(np.uint8))
        cv2.imwrite(stem + "-max.png", peak.astype(np.uint8))
        cv2.imwrite(stem + "-diff.png", boosted(diff, mask))
        cv2.imwrite(stem + "-maxdiff.png", boosted(diff_max, mask_max))
        print(stem + "-lit.png  (median: steady light)")
        print(stem + "-max.png  (max: includes flashes)")
        print("median", json.dumps(res))
        print("max   ", json.dumps(res_max))
    finally:
        if light and not a.hold:
            light.clear()
        cam.close()


def cmd_scan(a):
    chans = list(range(a.first, a.last + 1))
    if chans[0] < FIRST_SCAN or chans[-1] > LAST_CH:
        raise SystemExit(f"scan covers CH{FIRST_SCAN}-{LAST_CH}; CH1-3 move the bar (use sweep)")
    out = out_dir(a, "scan")
    cam = open_camera(a.camera)
    light = Light(a.url, a.fixture, parse_pairs(a.base))
    results, tiles = {}, []
    thr, box = a.threshold, None
    try:
        light.clear()
        if a.pos is not None:
            move(light, a.pos, a.move_wait)
        elif a.base:
            time.sleep(a.move_wait)
        time.sleep(a.settle)
        d1, d2 = cam.grab(a.frames), cam.grab(a.frames)
        noise = np.abs(d1 - d2).mean(axis=2)
        # Median + MAD: a hand moving through the frame mustn't raise the threshold.
        med = float(np.median(noise))
        thr = a.threshold or max(8.0, med + 8 * 1.4826 * float(np.median(np.abs(noise - med))))
        cv2.imwrite(os.path.join(out, "dark.png"), d1.astype(np.uint8))

        # Everything at once: shows the whole bar and sets the crop for the sheet.
        for ch in chans:
            light.set(ch, a.level)
        time.sleep(a.settle)
        all_lit = cam.grab(a.frames)
        light.clear()
        _, all_diff, all_mask = analyse(all_lit, d1, thr)
        cv2.imwrite(os.path.join(out, "all.png"), all_lit.astype(np.uint8))
        box = crop_box(all_mask, all_lit.shape)
        tiles.append(("all", boosted(all_diff, all_mask), None))
        print(f"threshold {thr:.1f}, crop {box}, {len(chans)} channels at level {a.level}")

        for ch in chans:
            time.sleep(a.settle)
            dark = cam.grab(3)   # a fresh dark per channel absorbs auto-exposure drift
            # A max over frames always beats a median (noise), so flashes compare max to max.
            dark_max = cam.grab(a.flash_frames, "max")
            light.set(ch, a.level)
            time.sleep(a.settle)
            lit = cam.grab(a.frames)
            peak = cam.grab(a.flash_frames, "max")
            light.clear()
            res, diff, mask = analyse(lit, dark, thr)
            res_max, diff_max, mask_max = analyse(peak, dark_max, thr)
            if res_max["visible"] and res_max["area"] > 2 * res["area"]:
                # Only seen in some frames: a strobe, or a flash the median threw away.
                res = dict(res_max, flashing=True)
                diff, mask = diff_max, mask_max
            results[ch] = res
            img = boosted(diff, mask)
            cv2.imwrite(os.path.join(out, f"ch{ch:02d}.png"), img)
            swatch = None
            if res["visible"]:
                swatch = np.array([255 * v for v in res["rgb"][::-1]], np.uint8)
            flash = " flash" if res.get("flashing") else ""
            label = f"CH{ch} " + (f"{res['colour'] or '?'} {res['area']}px{flash}" if res["visible"] else "-")
            tiles.append((label, img, swatch))
            print(f"  CH{ch:2d}  " + (f"{res['colour'] or '?':3s} area {res['area']:6d}  "
                                      f"at ({res['x']:.0f},{res['y']:.0f})  rgb {res['rgb']}"
                                      f"  clipped {res['clipped']:.0%}{flash}"
                                      if res["visible"] else "not visible"), flush=True)
    except KeyboardInterrupt:
        print("stopped")
    finally:
        light.clear()
        cam.close()

    vis = [(ch, r) for ch, r in results.items() if r["visible"]]
    summary = {"fixture": fixture_letter(a), "level": a.level, "threshold": round(thr, 1),
               "crop": [int(v) for v in box] if box else None,
               "channels": {str(k): v for k, v in results.items()}}
    if len(vis) >= 2:
        mean, v = principal_axis([(r["x"], r["y"]) for _, r in vis], [r["energy"] for _, r in vis])
        ts = {ch: float(np.dot((r["x"] - mean[0], r["y"] - mean[1]), v)) for ch, r in vis}
        lo, hi = min(ts.values()), max(ts.values())
        for ch, r in vis:
            r["t"] = round((ts[ch] - lo) / max(hi - lo, 1e-6), 3)
        summary["axis"] = {"centre": [round(float(c), 1) for c in mean],
                           "dir": [round(float(c), 3) for c in v],
                           "t0": "left end in the image" if abs(v[0]) >= abs(v[1]) else "top end"}
        summary["groups"] = group_channels(vis, a.group_tol)
        print(f"\nposition t runs 0..1 from the {summary['axis']['t0']}")
        for g in summary["groups"]:
            span = f"CH{g['channels'][0]}" + (f"-{g['channels'][-1]}" if len(g["channels"]) > 1 else "")
            print(f"  {span:10s} t={g['t']:.3f}  {' '.join(g['colours'])}")
    hidden = [ch for ch, r in results.items() if not r["visible"]]
    if hidden:
        print(f"not visible: {compress(hidden)}")
    with open(os.path.join(out, "results.json"), "w") as f:
        json.dump(summary, f, indent=1)
    if box:
        contact_sheet(tiles, box, os.path.join(out, "contact.png"))
        print(f"\n{out}/contact.png")
    print(f"{out}/results.json")


def cmd_sweep(a):
    """Step one channel through values (usually CH1) and keep a still of each."""
    start, stop, step = (int(v) for v in a.values.split(":"))
    out = out_dir(a, f"sweep{a.channel}")
    cam = open_camera(a.camera)
    light = Light(a.url, a.fixture, parse_pairs(a.base))
    tiles, log = [], []
    try:
        light.clear()
        for ch, v in parse_pairs(a.light):
            light.set(ch, v)
        for value in range(start, stop + 1, step):
            light.set(a.channel, value)
            time.sleep(a.dwell)
            frame = cam.grab(a.frames)
            lum = float(frame.mean())
            log.append({"value": value, "mean": round(lum, 2), "t": round(time.time(), 2)})
            img = frame.astype(np.uint8)
            cv2.imwrite(os.path.join(out, f"v{value:03d}.png"), img)
            tiles.append((f"CH{a.channel}={value}", img, None))
            print(f"  CH{a.channel}={value:3d}  mean {lum:.1f}", flush=True)
    except KeyboardInterrupt:
        print("stopped")
    finally:
        light.clear()   # also stops a CH2 spin
        cam.close()
    with open(os.path.join(out, "sweep.json"), "w") as f:
        json.dump(log, f, indent=1)
    if tiles:
        h, w = tiles[0][1].shape[:2]
        contact_sheet(tiles, (0, 0, w, h), os.path.join(out, "contact.png"))
        print(f"\n{out}/contact.png")


def cmd_serve(a):
    cam = Camera(0 if a.camera == "auto" else int(a.camera))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            url = urlparse(self.path)
            if url.path != "/grab":
                self.send_error(404)
                return
            q = parse_qs(url.query)
            n = max(1, min(60, int(q.get("n", ["5"])[0])))
            stat = q.get("stat", ["median"])[0]
            if stat not in STATS:
                self.send_error(400)
                return
            buf = io.BytesIO()
            np.save(buf, cam.grab(n, stat))
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    port = urlparse(SERVER).port
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"camera ready on {SERVER} (this Mac only). Leave this window open; Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        cam.close()


def cmd_stop(a):
    Light(a.url, a.fixture, parse_pairs(a.base)).cmd(action="stop")
    print("test mode off: the show is running again")


def compress(nums):
    runs, start = [], None
    for i, n in enumerate(nums):
        if start is None:
            start = n
        if i + 1 == len(nums) or nums[i + 1] != n + 1:
            runs.append(f"{start}" if start == n else f"{start}-{n}")
            start = None
    return ", ".join(runs)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8080", help="the running app")
    ap.add_argument("--fixture", default="A", type=lambda s: {"A": 0, "B": 1, "0": 0, "1": 1}[s.upper()],
                    help="A or B (default A)")
    ap.add_argument("--camera", default="auto",
                    help="camera index (see 'cams'), a serve URL, or auto (server, else camera 0)")
    ap.add_argument("--frames", type=int, default=5, help="frames per median")
    ap.add_argument("--flash-frames", type=int, default=15, help="frames for the max (strobe) capture")
    ap.add_argument("--settle", type=float, default=0.5, help="seconds after a DMX change")
    ap.add_argument("--pos", type=int, help="set CH1 first (turns the bar)")
    ap.add_argument("--move-wait", type=float, default=3.0, help="seconds to let a CH1 move finish")
    ap.add_argument("--base", default="", type=lambda v: [x for x in v.split(",") if x],
                    help="channels held on through every test, e.g. 36=84,38=0 (re-set after each clear)")
    ap.add_argument("--threshold", type=float, default=0.0, help="diff threshold (0 = from noise)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("cams", help="save a still from each camera index")
    p = sub.add_parser("snap", help="a still; with CH=V pairs, also lit + diff")
    p.add_argument("channels", nargs="*", help="e.g. 28=40 29=40")
    p.add_argument("--hold", action="store_true", help="leave the channels on afterwards")
    p = sub.add_parser("scan", help="light each channel in turn and analyse")
    p.add_argument("--from", dest="first", type=int, default=FIRST_SCAN)
    p.add_argument("--to", dest="last", type=int, default=LAST_CH)
    p.add_argument("--level", type=int, default=40, help="DMX value for each channel (keep it low)")
    p.add_argument("--group-tol", type=float, default=0.02, help="same-segment tolerance along the bar")
    p = sub.add_parser("sweep", help="step one channel through values, a still each")
    p.add_argument("channel", type=int)
    p.add_argument("values", help="start:stop:step, e.g. 0:255:15")
    p.add_argument("--light", nargs="*", default=[], help="channels to keep on, e.g. 76=30 77=30")
    p.add_argument("--dwell", type=float, default=1.5)
    sub.add_parser("stop", help="leave test mode")
    sub.add_parser("serve", help="hold the camera and serve frames to the other commands")
    a = ap.parse_args()
    if a.cmd == "snap" and a.threshold == 0.0:
        a.threshold = 8.0
    {"cams": cmd_cams, "snap": cmd_snap, "scan": cmd_scan, "sweep": cmd_sweep, "stop": cmd_stop,
     "serve": cmd_serve}[a.cmd](a)


if __name__ == "__main__":
    main()
