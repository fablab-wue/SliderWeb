# web_app — Microdot HTTP + WebSocket + captive probes + www/ editor.

try:
    import uasyncio as asyncio
except ImportError:
    import asyncio

if not hasattr(asyncio, "sleep_ms"):
    async def _sleep_ms(ms):
        await asyncio.sleep(ms / 1000.0)

    asyncio.sleep_ms = _sleep_ms

try:
    import ujson as json
except ImportError:
    import json
import os
import gc

import SW_config as cfg
from dbg import dbg
from microdot.microdot import Microdot, Request, Response, send_file, redirect
from microdot.websocket import with_websocket, WebSocketError

Request.max_body_length = 8192
Request.max_content_length = 65536

try:
    Response.default_content_type = "text/plain"
except Exception:
    pass

_MIME = {
    "html": "text/html; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "js": "application/javascript; charset=utf-8",
    "json": "application/json",
    "svg": "image/svg+xml",
    "png": "image/png",
    "ico": "image/x-icon",
    "txt": "text/plain; charset=utf-8",
}

_CAPTIVE = (
    "generate_204",
    "gen_204",
    "hotspot-detect.html",
    "library/test/success.html",
    "ncsi.txt",
    "connecttest.txt",
    "fwlink",
    "canonical.html",
    "success.txt",
    "redirect",
    "wpad.dat",
    "kindle-wifi/wifistub.html",
)

_CAPTIVE_HOST = (
    "captive.apple.com",
    "detectportal.firefox.com",
    "connectivitycheck.gstatic.com",
    "connectivitycheck.android.com",
    "clients3.google.com",
    "www.msftconnecttest.com",
    "www.msftncsi.com",
    "neverssl.com",
)


def _ctype(path):
    if "." not in path:
        return "application/octet-stream"
    ext = path.rsplit(".", 1)[-1].lower()
    return _MIME.get(ext, "application/octet-stream")


def _portal_ip(wifi):
    ip = getattr(wifi, "ap_ip", None) or getattr(cfg, "AP_IP", None) or "192.168.4.1"
    parts = str(ip).split(".")
    if len(parts) != 4:
        return "192.168.4.1"
    try:
        o = [int(p) for p in parts]
    except ValueError:
        return "192.168.4.1"
    if o[0] == 192 and o[1] == 168:
        return ip
    if o[0] == 10:
        return ip
    return "192.168.4.1"


def _safe_rel(rel):
    if rel is None:
        return None
    rel = str(rel).replace("\\", "/").lstrip("/")
    parts = []
    for p in rel.split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            return None
        parts.append(p)
    if not parts:
        return None
    if parts[0] != "www":
        parts = ["www"] + parts
    return "/".join(parts)


def _list_www(base="www"):
    out = []
    try:
        listing = os.ilistdir(base)
    except AttributeError:
        try:
            names = os.listdir(base)
        except OSError:
            return out
        for name in names:
            path = base + "/" + name
            try:
                if os.path.isdir(path):
                    out.extend(_list_www(path))
                else:
                    out.append(path)
            except OSError:
                out.append(path)
        return out
    except OSError:
        return out
    for item in listing:
        name = item[0]
        typ = item[1] if len(item) > 1 else 0x8000
        path = base + "/" + name
        if typ == 0x4000:
            out.extend(_list_www(path))
        else:
            out.append(path)
    return out


class WebApp:
    def __init__(self, panel, wifi):
        self.panel = panel
        self.wifi = wifi
        self.clients = []
        self.app = Microdot()
        self._install_routes()

    def _status_payload(self):
        d = self.panel.status_dict()
        d["wifi"] = self.wifi.public_status()
        return d

    def _install_routes(self):
        app = self.app
        web = self

        @app.before_request
        async def _log_req(req):
            host = ""
            try:
                host = req.headers.get("Host") or req.headers.get("host") or ""
            except Exception:
                pass
            dbg(3, "HTTP", req.method, req.path, "host", host)
            if req.path in ("/", "/index.html"):
                web.wifi._saw_http = True
            return None

        @app.route("/")
        async def index(req):
            return send_file("www/index.html", content_type=_ctype("index.html"), max_age=0)

        @app.route("/api/status")
        async def api_status(req):
            return Response(
                body=json.dumps(web._status_payload()),
                headers={"Content-Type": "application/json"},
            )

        @app.route("/api/hello")
        async def api_hello(req):
            try:
                await web.panel.refresh_hello()
            except Exception as exc:
                dbg(2, "api hello refresh", exc)
            return Response(
                body=json.dumps(web.panel.hello_dict()),
                headers={"Content-Type": "application/json"},
            )

        @app.route("/api/config")
        async def api_config(req):
            return Response(
                body=json.dumps(web.panel.config_dict()),
                headers={"Content-Type": "application/json"},
            )

        def _json_body(req):
            body = None
            try:
                body = req.json
            except Exception:
                body = None
            if body is None and getattr(req, "body", None):
                try:
                    raw = req.body
                    if isinstance(raw, bytes):
                        raw = raw.decode()
                    body = json.loads(raw)
                except Exception:
                    body = None
            return body if isinstance(body, dict) else {}

        @app.route("/api/cmd", methods=["POST"])
        async def api_cmd(req):
            web.panel.on_ws_msg(_json_body(req))
            return Response(
                body='{"ok":true}',
                headers={"Content-Type": "application/json"},
            )

        @app.route("/api/wifi", methods=["GET", "POST"])
        async def api_wifi(req):
            if req.method == "POST":
                body = _json_body(req)
                if body.get("forget"):
                    await web.wifi.forget()
                else:
                    await web.wifi.apply_saved(
                        body.get("ssid"), body.get("password"), body.get("hostname")
                    )
            return Response(
                body=json.dumps(web.wifi.public_status()),
                headers={"Content-Type": "application/json"},
            )

        @app.route("/api/files", methods=["GET", "PUT"])
        async def api_files(req):
            if req.method == "GET":
                path = None
                if req.args:
                    path = req.args.get("path")
                if not path:
                    files = _list_www("www")
                    return Response(
                        body=json.dumps({"files": files}),
                        headers={"Content-Type": "application/json"},
                    )
                rel = _safe_rel(path)
                if rel is None:
                    return Response("bad path", status_code=400)
                try:
                    with open(rel, "r") as f:
                        text = f.read()
                except OSError:
                    return Response("not found", status_code=404)
                return Response(
                    body=json.dumps({"path": rel, "text": text}),
                    headers={"Content-Type": "application/json"},
                )
            body = _json_body(req)
            rel = _safe_rel(body.get("path"))
            if rel is None:
                return Response("bad path", status_code=400)
            text = body.get("text")
            if text is None:
                return Response("no text", status_code=400)
            text = str(text)
            cap = int(getattr(cfg, "FILE_MAX_BYTES", 49152))
            if len(text) > cap:
                return Response("too large", status_code=413)
            try:
                with open(rel, "w") as f:
                    f.write(text)
            except OSError as exc:
                dbg(1, "file write fail", rel, exc)
                return Response("write fail", status_code=500)
            dbg(3, "saved", rel)
            return Response(
                body=json.dumps({"ok": True, "path": rel}),
                headers={"Content-Type": "application/json"},
            )

        @app.route("/ws")
        @with_websocket
        async def ws_handler(req, ws):
            web.clients.append(ws)
            dbg(4, "ws +", len(web.clients))
            try:
                try:
                    await web.panel.refresh_hello()
                except Exception as exc:
                    dbg(2, "hello refresh", exc)
                try:
                    await ws.send(json.dumps(web.panel.hello_dict()))
                except Exception:
                    pass
                await ws.send(json.dumps(web._status_payload()))
                while True:
                    msg = await ws.receive()
                    if msg is None:
                        break
                    if isinstance(msg, bytes):
                        try:
                            msg = msg.decode()
                        except Exception:
                            continue
                    try:
                        obj = json.loads(msg)
                    except Exception:
                        continue
                    web.panel.on_ws_msg(obj)
                    # Optional ack for old clients; WDT needs no reply.
                    if isinstance(obj, dict) and (
                        obj.get("t") == "ping" or "wdt" in obj
                    ):
                        try:
                            await ws.send('{"t":"pong"}')
                        except Exception:
                            break
            except WebSocketError:
                pass
            except OSError:
                pass
            finally:
                try:
                    web.clients.remove(ws)
                except ValueError:
                    pass
                dbg(4, "ws -", len(web.clients))

        @app.route("/<path:path>")
        async def static_or_captive(req, path):
            low = str(path).lstrip("/").lower()
            host = ""
            try:
                host = (req.headers.get("Host") or req.headers.get("host") or "")
            except Exception:
                pass
            hlow = str(host).split(":")[0].strip().lower()
            probe = False
            for name in _CAPTIVE:
                if low == name or low.endswith("/" + name):
                    probe = True
                    break
            if (not probe) and hlow in _CAPTIVE_HOST:
                probe = True
            if probe:
                loc = "http://%s/" % _portal_ip(web.wifi)
                return redirect(loc)
            rel = _safe_rel(path)
            if rel is None:
                return Response("bad path", status_code=400)
            try:
                os.stat(rel)
            except OSError:
                return Response("not found", status_code=404)
            return send_file(rel, content_type=_ctype(rel), max_age=0)

    async def _broadcaster(self):
        period = 1.0 / float(getattr(cfg, "SW_STATUS_HZ", 12))
        last = None
        while True:
            if not self.clients:
                last = None
                await asyncio.sleep(period)
                continue
            payload = json.dumps(self._status_payload())
            if payload != last:
                dead = []
                for ws in self.clients:
                    try:
                        await ws.send(payload)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    try:
                        self.clients.remove(ws)
                    except ValueError:
                        pass
                last = payload
            await asyncio.sleep(period)

    async def run(self):
        port = int(getattr(cfg, "HTTP_PORT", 80))
        gc.collect()
        dbg(3, "HTTP", port, "heap", gc.mem_free())
        asyncio.create_task(self._broadcaster())
        while True:
            try:
                await self.app.start_server(host="0.0.0.0", port=port)
                return
            except OSError as exc:
                dbg(1, "HTTP listen", exc)
                await asyncio.sleep_ms(200)
