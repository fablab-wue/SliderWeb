#!/usr/bin/env python3
"""Local preview of board/www/ + mock /api/status (no Pico, no WebSocket).

    python tools/preview.py

Then open http://127.0.0.1:8080/
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
BOARD = ROOT / "board"
WWW = BOARD / "www"
os.chdir(BOARD)

MOCK = {
    "state": "I",
    "axes": 2,
    "unit": "mm",
    "pos": 12.3,
    "spd": 0.0,
    "acc": 0.0,
    "pos2": 45.0,
    "spd2": 0.0,
    "acc2": 0.0,
    "tgt": None,
    "tgt2": None,
    "ss": 40.0,
    "spd_min": 1.0,
    "max_speed": 100.0,
    "slider_min": 0.0,
    "slider_max": 600.0,
    "slider_min_2": 0.0,
    "slider_max_2": 360.0,
    "line1": "Ready",
    "line2": "",
    "warn": False,
    "enabled": True,
    "ax": 1,
    "wifi": {
        "mode": "preview",
        "ip": "127.0.0.1",
        "ap_ssid": "SliderWeb-preview",
        "hostname": "slider",
        "ssid": "",
        "has_password": False,
        "sta_configured": False,
    },
}

WIFI = {
    "mode": "preview",
    "ip": "127.0.0.1",
    "ap_ssid": "SliderWeb-preview",
    "hostname": "slider",
    "ssid": "",
    "has_password": False,
    "sta_configured": False,
}

_lock = threading.Lock()
_vel = 0.0
_vel2 = 0.0
_ss = float(MOCK["ss"])
_tgt = None
_tgt2 = None
_last_t = time.monotonic()


def _clamp(pos, lo, hi):
    if lo is not None and pos < lo:
        return lo, True
    if hi is not None and pos > hi:
        return hi, True
    return pos, False


def _sim_tick():
    """Integrate position from commanded velocity (ignore accel)."""
    global _vel, _vel2, _tgt, _tgt2, _last_t
    with _lock:
        now = time.monotonic()
        dt = now - _last_t
        if dt < 0:
            dt = 0
        _last_t = now

        # Seek absolute targets if set (MT).
        if _tgt is not None:
            err = _tgt - float(MOCK["pos"])
            if abs(err) < 0.05:
                MOCK["pos"] = float(_tgt)
                _vel = 0.0
                _tgt = None
                MOCK["tgt"] = None
            else:
                _vel = _ss if err > 0 else -_ss

        if _tgt2 is not None:
            err2 = _tgt2 - float(MOCK["pos2"])
            if abs(err2) < 0.05:
                MOCK["pos2"] = float(_tgt2)
                _vel2 = 0.0
                _tgt2 = None
                MOCK["tgt2"] = None
            else:
                _vel2 = _ss if err2 > 0 else -_ss

        if _vel != 0.0:
            pos = float(MOCK["pos"]) + _vel * dt
            pos, hit = _clamp(pos, MOCK.get("slider_min"), MOCK.get("slider_max"))
            MOCK["pos"] = round(pos, 3)
            if hit:
                _vel = 0.0
                _tgt = None
                MOCK["tgt"] = None

        if _vel2 != 0.0:
            pos2 = float(MOCK["pos2"]) + _vel2 * dt
            pos2, hit2 = _clamp(pos2, MOCK.get("slider_min_2"), MOCK.get("slider_max_2"))
            MOCK["pos2"] = round(pos2, 3)
            if hit2:
                _vel2 = 0.0
                _tgt2 = None
                MOCK["tgt2"] = None

        MOCK["spd"] = round(abs(_vel), 3)
        MOCK["spd2"] = round(abs(_vel2), 3)
        MOCK["ss"] = _ss
        if _vel != 0.0 or _vel2 != 0.0 or _tgt is not None or _tgt2 is not None:
            MOCK["state"] = "M"
            MOCK["line1"] = "Moving"
        else:
            MOCK["state"] = "I"
            MOCK["line1"] = "Ready"


def _apply_mc(line):
    """Apply a UART-style MC command to the mock motion state."""
    global _vel, _vel2, _ss, _tgt, _tgt2
    parts = str(line or "").strip().split()
    if not parts:
        return
    cmd = parts[0].upper()
    with _lock:
        if cmd == "MS":
            _vel = 0.0
            _vel2 = 0.0
            _tgt = None
            _tgt2 = None
            MOCK["tgt"] = None
            MOCK["tgt2"] = None
            MOCK["state"] = "I"
            MOCK["line1"] = "Stop"
            return
        if cmd == "H":
            _vel = 0.0
            _vel2 = 0.0
            _tgt = None
            _tgt2 = None
            MOCK["tgt"] = None
            MOCK["tgt2"] = None
            MOCK["state"] = "H"
            MOCK["line1"] = "Halt"
            return
        if cmd == "SE":
            en = True
            if len(parts) > 1:
                try:
                    en = int(float(parts[1])) != 0
                except ValueError:
                    en = True
            MOCK["enabled"] = en
            MOCK["line1"] = "Enabled" if en else "Disabled"
            if not en:
                _vel = 0.0
                _vel2 = 0.0
            return
        if cmd == "SS" and len(parts) > 1:
            try:
                _ss = abs(float(parts[1]))
            except ValueError:
                return
            MOCK["ss"] = _ss
            # Keep jogging speed in sync with SS while moving (no accel).
            if _vel != 0.0 and _tgt is None:
                _vel = _ss if _vel > 0 else -_ss
            if _vel2 != 0.0 and _tgt2 is None:
                _vel2 = _ss if _vel2 > 0 else -_ss
            return
        if cmd in ("ML", "MR"):
            sign = -1.0 if cmd == "ML" else 1.0
            axis = 1
            if len(parts) > 1:
                try:
                    axis = int(float(parts[1]))
                except ValueError:
                    axis = 1
            if axis == 2:
                _vel2 = sign * _ss
                _tgt2 = None
                MOCK["tgt2"] = None
            else:
                _vel = sign * _ss
                _tgt = None
                MOCK["tgt"] = None
            MOCK["state"] = "M"
            MOCK["line1"] = "Jog"
            return
        if cmd == "MT":
            # MT pos [pos2] — blanks / "_" skip an axis
            if len(parts) > 1 and parts[1] not in ("_", ""):
                try:
                    _tgt = float(parts[1])
                    MOCK["tgt"] = _tgt
                except ValueError:
                    pass
            if len(parts) > 2 and parts[2] not in ("_", ""):
                try:
                    _tgt2 = float(parts[2])
                    MOCK["tgt2"] = _tgt2
                except ValueError:
                    pass
            MOCK["state"] = "M"
            MOCK["line1"] = "Goto"
            return
        if cmd == "MH":
            _tgt = float(MOCK.get("slider_min") or 0.0)
            MOCK["tgt"] = _tgt
            MOCK["state"] = "H"
            MOCK["line1"] = "Home"
            return
        if cmd == "SL":
            if len(parts) > 1:
                try:
                    MOCK["slider_min"] = float(parts[1])
                except ValueError:
                    pass
            else:
                MOCK["slider_min"] = None
            return
        if cmd == "SR":
            if len(parts) > 1:
                try:
                    MOCK["slider_max"] = float(parts[1])
                except ValueError:
                    pass
            else:
                MOCK["slider_max"] = None
            return


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _text(self, code, text):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _safe(self, rel):
        rel = (rel or "").replace("\\", "/").lstrip("/")
        parts = [p for p in rel.split("/") if p and p != "."]
        if ".." in parts:
            return None
        if parts and parts[0] != "www":
            parts = ["www"] + parts
        path = BOARD.joinpath(*parts)
        try:
            path.resolve().relative_to(WWW)
        except ValueError:
            return None
        return path

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/status":
            _sim_tick()
            with _lock:
                self._json(dict(MOCK))
            return
        if u.path == "/api/config":
            self._json({"axis_count": 2, "axis2_use": "1", "max_speed": "100"})
            return
        if u.path == "/api/wifi":
            self._json(WIFI)
            return
        if u.path == "/api/files":
            qs = parse_qs(u.query)
            path = (qs.get("path") or [None])[0]
            if not path:
                files = []
                for p in sorted(WWW.rglob("*")):
                    if p.is_file():
                        files.append(p.relative_to(BOARD).as_posix())
                self._json({"files": files})
                return
            fp = self._safe(path)
            if fp is None or not fp.is_file():
                self._text(404, "not found")
                return
            self._json({"path": fp.relative_to(BOARD).as_posix(), "text": fp.read_text(encoding="utf-8")})
            return
        if u.path == "/" or u.path == "/index.html":
            self.path = "/www/index.html"
        elif not u.path.startswith("/www/") and not u.path.startswith("/api/"):
            self.path = "/www" + u.path
        return SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {}
        if u.path == "/api/wifi":
            if body.get("forget"):
                WIFI["ssid"] = ""
                WIFI["sta_configured"] = False
            else:
                WIFI["ssid"] = str(body.get("ssid") or "")
                WIFI["hostname"] = str(body.get("hostname") or WIFI["hostname"])
                WIFI["sta_configured"] = bool(WIFI["ssid"])
            self._json(WIFI)
            return
        if u.path == "/api/cmd":
            if "mc" in body:
                line = body.get("mc")
                sys.stderr.write("MC> %s\n" % line)
                _apply_mc(line)
            if "wdt" in body:
                pass
            self._json({"ok": True})
            return
        self._text(404, "not found")

    def do_PUT(self):
        u = urlparse(self.path)
        if u.path != "/api/files":
            self._text(404, "not found")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._text(400, "bad json")
            return
        fp = self._safe(body.get("path"))
        if fp is None:
            self._text(400, "bad path")
            return
        text = str(body.get("text") or "")
        fp.write_text(text, encoding="utf-8")
        self._json({"ok": True, "path": fp.relative_to(BOARD).as_posix()})


def main():
    port = int(os.environ.get("PORT", "8080"))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("SliderWeb preview  http://127.0.0.1:%d/" % port)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
