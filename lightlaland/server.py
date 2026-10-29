"""Tiny standard-library web server: the UI page, a JSON command API and a live state stream."""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import __version__, geometry
from .audio import list_inputs
from .fixture import N_BEAMS, N_CH, N_STRIP, N_WHITE, describe
from .scenes import GROUPS, SCHEMA, SETS

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
STREAM_HZ = 30


def meta(app):
    return {
        "version": __version__,
        "config": app.config.snapshot(),
        "scenes": app.scene_list(),
        "schema": SCHEMA,
        "groups": list(GROUPS),
        "sets": SETS,
        "devices": list_inputs(),
        "search": app.last_search,
        "channels": [describe(c) for c in range(1, N_CH + 1)],
        "layout": {"beams": N_BEAMS, "strip": N_STRIP, "white": N_WHITE},
        "geometry": {"deg_per_step": geometry.DEG_PER_STEP, "steps_per_turn": geometry.STEPS_PER_TURN},
    }


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"lightlaland/{__version__}"

        def log_message(self, fmt, *args):  # keep the terminal quiet
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                with open(os.path.join(WEB, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path == "/api/meta":
                self._send(200, meta(app))
            elif path == "/api/state":
                self._send(200, app.engine.vis_json())
            elif path == "/api/stream":
                self._stream()
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if urlparse(self.path).path != "/api/cmd":
                self._send(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._send(400, {"ok": False, "error": "bad JSON"})
                return
            result = app.command(body)
            self._send(200 if result.get("ok") else 400, result)

        def _stream(self):
            """Server-sent events: the engine's latest visual state, ~30 times a second."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b"data: " + app.engine.vis_json() + b"\n\n")
                    self.wfile.flush()
                    time.sleep(1.0 / STREAM_HZ)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return

    return Handler


def serve(app, host, port):
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    return server
