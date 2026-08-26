# wifi_portal — STA / AP / captive DNS. Sticky: short hiccups do not flap the radio.

try:
    import uasyncio as asyncio
except ImportError:
    import asyncio

if not hasattr(asyncio, "sleep_ms"):
    async def _sleep_ms(ms):
        await asyncio.sleep(ms / 1000.0)

    asyncio.sleep_ms = _sleep_ms

import json
import socket
import time
import gc

import SW_config as cfg
from dbg import dbg, dump_ring
from led_status import LedStatus

try:
    import network
except ImportError:
    network = None

try:
    import machine
except ImportError:
    machine = None


def _mac_suffix():
    if machine is None:
        return "0000"
    try:
        uid = machine.unique_id()
        return "%02x%02x" % (uid[-2], uid[-1])
    except Exception:
        return "0000"


def _ap_ssid():
    prefix = str(getattr(cfg, "AP_SSID_PREFIX", "SWeb") or "SWeb")
    if prefix == "SliderWeb":
        prefix = "SWeb"
    return "%s-%s" % (prefix, _mac_suffix())


def _load_wifi():
    path = str(getattr(cfg, "WIFI_JSON", "wifi.json"))
    try:
        with open(path, "r") as f:
            data = json.loads(f.read())
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def save_wifi(ssid, password, hostname=None):
    path = str(getattr(cfg, "WIFI_JSON", "wifi.json"))
    data = {
        "ssid": "" if ssid is None else str(ssid),
        "password": "" if password is None else str(password),
        "hostname": str(hostname or getattr(cfg, "HOSTNAME", "slider")),
    }
    with open(path, "w") as f:
        f.write(json.dumps(data))
    return data


def _wanted_ap_ip():
    return str(getattr(cfg, "AP_IP", "192.168.4.1") or "192.168.4.1")


def _sane_ipv4(s):
    parts = str(s or "").split(".")
    if len(parts) != 4:
        return False
    try:
        o = [int(p) for p in parts]
    except ValueError:
        return False
    if any(n < 0 or n > 255 for n in o):
        return False
    if o[0] == 10:
        return True
    if o[0] == 172 and 16 <= o[1] <= 31:
        return True
    if o[0] == 192 and o[1] == 168:
        return True
    return False


class WifiPortal:
    def __init__(self, led):
        self.led = led
        self.sta = None
        self.ap = None
        self._cfg = _load_wifi()
        self.hostname = str(
            self._cfg.get("hostname") or getattr(cfg, "HOSTNAME", "slider")
        )
        self._sta_down_ms = None
        self._ap_started = False
        self._dns_sock = None
        self._ap_clients = -1
        self._join_ms = None
        self._hold_log_ms = 0
        self._saw_dns = False
        self._saw_http = False
        self._dns_logged = 0
        self._hb_ms = 0
        self.mode = "ap"
        self.ip = _wanted_ap_ip()
        self.ap_ip = self.ip
        self.want_sta = bool(str(self._cfg.get("ssid") or "").strip())

    def public_status(self):
        ssid = str(self._cfg.get("ssid") or "")
        return {
            "mode": self.mode,
            "ip": self.ip,
            "ap_ssid": _ap_ssid(),
            "hostname": self.hostname,
            "ssid": ssid,
            "has_password": bool(str(self._cfg.get("password") or "")),
            "sta_configured": bool(ssid.strip()),
        }

    def _pm_none(self, wlan):
        try:
            wlan.config(pm=network.WLAN.PM_NONE)
            return
        except Exception:
            pass
        try:
            wlan.config(pm=0)
        except Exception:
            pass

    def _set_hostname(self, wlan):
        for key in ("dhcp_hostname", "hostname"):
            try:
                wlan.config(**{key: self.hostname})
                return
            except Exception:
                pass

    def _sleep_ms(self, ms):
        try:
            time.sleep_ms(int(ms))
        except AttributeError:
            time.sleep(ms / 1000.0)

    def _stop_sta(self, settle_ms=500):
        """Stop STA only if we started it."""
        sta = self.sta
        if sta is None:
            return
        try:
            sta.disconnect()
        except Exception:
            pass
        try:
            sta.active(False)
        except Exception:
            pass
        self.sta = None
        gc.collect()
        self._sleep_ms(settle_ms)

    def _set_country(self):
        code = str(getattr(cfg, "WIFI_COUNTRY", "DE") or "DE")
        try:
            network.country(code)
        except Exception:
            pass

    def _auth_wpa2(self):
        for name in ("AUTH_WPA2_PSK",):
            v = getattr(network, name, None)
            if v is not None:
                return int(v)
        w = getattr(network, "WLAN", None)
        if w is not None:
            v = getattr(w, "SEC_WPA2", None)
            if v is not None:
                return int(v)
        return 3

    def _apply_ap_config(self, ssid, pw, channel):
        attempts = []
        extra = dict(channel=channel)
        if pw:
            auth = self._auth_wpa2()
            attempts.append(dict(essid=ssid, password=pw, **extra))
            attempts.append(dict(ssid=ssid, password=pw, **extra))
            attempts.append(dict(essid=ssid, key=pw, **extra))
            attempts.append(dict(ssid=ssid, key=pw, security=auth, **extra))
            attempts.append(dict(essid=ssid, password=pw, authmode=auth, **extra))
        else:
            attempts.append(dict(essid=ssid, **extra))
            attempts.append(dict(ssid=ssid, **extra))
        last = None
        for kw in attempts:
            try:
                self.ap.config(**kw)
                return True
            except Exception as exc:
                last = exc
        dbg(1, "AP config fail", last)
        return False

    def _stop_ap(self):
        if self.ap is None:
            return
        try:
            self.ap.active(False)
        except Exception:
            pass
        self._ap_started = False
        gc.collect()

    def _start_ap(self):
        self._set_country()
        if self.sta is not None:
            self._stop_sta(200)
        if self.ap is None:
            self.ap = network.WLAN(network.AP_IF)
        if self._ap_started and self.ap.active():
            return
        ssid = _ap_ssid()
        pw = str(getattr(cfg, "AP_PASSWORD", "") or "") or "sliderweb"
        channel = int(getattr(cfg, "AP_CHANNEL", 6))
        try:
            if self.ap.active():
                self.ap.active(False)
                self._sleep_ms(100)
        except Exception:
            pass
        self._apply_ap_config(ssid, pw, channel)
        self.ap.active(True)
        t0 = time.ticks_ms()
        while not self.ap.active():
            if time.ticks_diff(time.ticks_ms(), t0) > 2000:
                dbg(1, "AP active timeout")
                break
            self._sleep_ms(50)
        self._pm_none(self.ap)
        ip = _wanted_ap_ip()
        self.ap_ip = ip
        self.ip = ip
        try:
            ifcfg = self.ap.ifconfig()
            dbg(3, "AP ifconfig", ifcfg, "want", ip)
            got = ifcfg[0] if ifcfg else ""
            if _sane_ipv4(got):
                self.ap_ip = got
                self.ip = got
        except Exception as exc:
            dbg(2, "AP ifconfig get", exc)
        self._ap_started = True
        dbg(3, "AP", ssid, self.ap_ip, "wpa2", "pw", pw)
        dbg(3, "AP DHCP (cyw43)")
        gc.collect()
        dbg(3, "heap AP", self._heap())

    def _heap(self):
        try:
            return gc.mem_free()
        except Exception:
            return "?"

    def _manual_ip_hint(self):
        ip = self.ap_ip or _wanted_ap_ip()
        parts = str(ip).split(".")
        if len(parts) == 4:
            parts[-1] = "2"
            client = ".".join(parts)
        else:
            client = "192.168.4.2"
        return "%s / 255.255.255.0 gw+dns %s" % (client, ip)

    def _fmt_stations(self, st):
        out = []
        for item in st or []:
            mac = item[0] if isinstance(item, (tuple, list)) and item else item
            try:
                if isinstance(mac, (bytes, bytearray)) and len(mac) >= 6:
                    out.append("%02x:%02x:%02x:%02x:%02x:%02x" % tuple(mac[:6]))
                    continue
            except Exception:
                pass
            out.append(str(item))
        return out

    def _start_sta(self):
        ssid = str(self._cfg.get("ssid") or "").strip()
        if not ssid:
            return False
        if self.sta is None:
            self.sta = network.WLAN(network.STA_IF)
        if not self.sta.active():
            self.sta.active(True)
        self._pm_none(self.sta)
        self._set_hostname(self.sta)
        try:
            if not self.sta.isconnected():
                pw = str(self._cfg.get("password") or "")
                self.sta.connect(ssid, pw)
                dbg(3, "STA connect", ssid)
        except Exception as exc:
            dbg(1, "STA connect fail", exc)
        return True

    async def start(self):
        if network is None:
            dbg(1, "network module missing — preview/host?")
            self.mode = "ap"
            self.ip = _wanted_ap_ip()
            self.led.set_wifi(LedStatus.MODE_AP, self.ip)
            return

        self.want_sta = bool(str(self._cfg.get("ssid") or "").strip())
        if self.want_sta:
            self._start_sta()
            self.mode = "sta_wait"
            self.led.set_wifi(LedStatus.MODE_STA_WAIT)
            timeout_ms = int(float(getattr(cfg, "STA_CONNECT_S", 12.0)) * 1000)
            deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
            while time.ticks_diff(deadline, time.ticks_ms()) > 0:
                if self.sta is not None and self.sta.isconnected():
                    self._on_sta_up()
                    self._bind_dns()
                    return
                await asyncio.sleep_ms(200)
            dbg(2, "STA timeout — AP fallback")
            self._start_ap()
            self.mode = "ap"
            self.ip = self.ap_ip
            self.led.set_wifi(LedStatus.MODE_AP, self.ip)
        else:
            self._start_ap()
            self.mode = "ap"
            self.ip = self.ap_ip
            self.led.set_wifi(LedStatus.MODE_AP, self.ip)
        self._bind_dns()

    def _on_sta_up(self):
        try:
            ip = self.sta.ifconfig()[0]
        except Exception:
            ip = "0.0.0.0"
        self.ip = ip
        self.mode = "sta"
        self._sta_down_ms = None
        self.led.set_wifi(LedStatus.MODE_STA, ip)
        dbg(3, "STA up", ip)

    def _bind_dns(self):
        if self._dns_sock is not None:
            return
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setblocking(False)
            s.bind(("0.0.0.0", 53))
            self._dns_sock = s
            dbg(3, "captive DNS :53")
        except Exception as exc:
            dbg(2, "DNS bind fail", exc)
            self._dns_sock = None

    def _dns_ip(self):
        # Captive DNS is for AP phones; STA clients already have this host's IP.
        if self._ap_started:
            ip = self.ap_ip or _wanted_ap_ip()
            return ip if _sane_ipv4(ip) else _wanted_ap_ip()
        if self.mode == "sta" and self.sta is not None and self.sta.isconnected():
            try:
                ip = self.sta.ifconfig()[0]
                if _sane_ipv4(ip):
                    return ip
            except Exception:
                pass
        ip = self.ap_ip or _wanted_ap_ip()
        return ip if _sane_ipv4(ip) else _wanted_ap_ip()

    def _dns_reply(self, req):
        if req is None or len(req) < 12:
            return None
        try:
            parts = self._dns_ip().split(".")
            ipb = bytes([int(p) for p in parts])
        except Exception:
            return None
        i = 12
        n = len(req)
        while i < n:
            lab = req[i]
            if lab == 0:
                i += 1
                break
            if lab & 0xC0 == 0xC0:
                i += 2
                break
            i += lab + 1
            if i >= n:
                return None
        if i + 4 > n:
            return None
        qtype = (req[i] << 8) | req[i + 1]
        i += 4
        question = req[12:i]
        # A=1 — answer our IP. AAAA=28 / HTTPS=65 — empty so iOS falls back to A.
        if qtype == 1:
            out = req[0:2] + b"\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00"
            out += question
            out += b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x1e\x00\x04" + ipb
            return out
        out = req[0:2] + b"\x81\x80\x00\x01\x00\x00\x00\x00\x00\x00"
        out += question
        return out

    def _dns_qinfo(self, req):
        if req is None or len(req) < 12:
            return "?", 0
        labels = []
        i = 12
        n = len(req)
        while i < n:
            lab = req[i]
            if lab == 0:
                i += 1
                break
            if lab & 0xC0 == 0xC0:
                break
            i += 1
            if i + lab > n:
                break
            try:
                labels.append(bytes(req[i : i + lab]).decode())
            except Exception:
                labels.append("?")
            i += lab
        qtype = 0
        if i + 2 <= n:
            qtype = (req[i] << 8) | req[i + 1]
        names = {1: "A", 12: "PTR", 16: "TXT", 28: "AAAA", 33: "SRV", 65: "HTTPS"}
        return ".".join(labels) or "?", names.get(qtype, str(qtype))

    def _poll_dns(self):
        s = self._dns_sock
        if s is None:
            return
        limit = 24 if self._ap_clients > 0 else 12
        for _ in range(limit):
            try:
                data, addr = s.recvfrom(512)
            except Exception:
                return
            if not data:
                return
            name, qtype = self._dns_qinfo(data)
            reply = self._dns_reply(data)
            nrep = len(reply) if reply else 0
            self._saw_dns = True
            self._dns_logged += 1
            if self._dns_logged <= 3 or "captive" in name or "detectportal" in name:
                dbg(3, "DNS", addr, qtype, name, "q", len(data), "r", nrep)
            else:
                dbg(4, "DNS", addr, qtype, name, "q", len(data), "r", nrep)
            if reply:
                try:
                    s.sendto(reply, addr)
                except Exception as exc:
                    dbg(2, "DNS send fail", exc)

    def _log_ap_clients(self):
        if self.ap is None or not self._ap_started:
            return
        try:
            st = self.ap.status("stations")
        except Exception as exc:
            dbg(2, "AP stations err", exc)
            return
        n = len(st) if st else 0
        now = time.ticks_ms()
        if n == self._ap_clients:
            if n > 0 and time.ticks_diff(now, self._hold_log_ms) >= 2000:
                self._hold_log_ms = now
                held = 0
                if self._join_ms is not None:
                    held = time.ticks_diff(now, self._join_ms)
                dbg(3, "AP hold", n, "ms", held, "heap", self._heap())
            return
        prev = self._ap_clients
        self._ap_clients = n
        macs = self._fmt_stations(st)
        if n > 0 and prev <= 0:
            self._join_ms = now
            self._hold_log_ms = now
            self._saw_dns = False
            self._dns_logged = 0
            gc.collect()
            dbg(3, "AP join", n, "mac", macs, "heap", self._heap(), "raw", st)
        elif n == 0 and prev > 0:
            held = 0
            if self._join_ms is not None:
                held = time.ticks_diff(now, self._join_ms)
            self._join_ms = None
            why = "had DNS" if self._saw_dns else "no DNS — DHCP likely failed"
            dbg(3, "AP leave after", held, "ms", why, "heap", self._heap())
            if not self._saw_dns:
                hint = self._manual_ip_hint()
                dbg(3, "set iPhone IPv4 manual", hint)
            dump_ring()
        else:
            dbg(3, "AP clients", n, "mac", macs, "heap", self._heap())

    def tick(self):
        self._poll_dns()
        self._log_ap_clients()
        self._poll_dns()
        if not self.want_sta or self.sta is None:
            return
        now = time.ticks_ms()
        grace = int(getattr(cfg, "STA_GRACE_MS", 10000))
        if self.sta.isconnected():
            if self.mode != "sta":
                self._on_sta_up()
            return
        # disconnected — do not tear STA down
        if self._sta_down_ms is None:
            self._sta_down_ms = now
            if self.mode == "sta":
                self.mode = "grace"
                dbg(4, "STA hiccup — grace")
            return
        if time.ticks_diff(now, self._sta_down_ms) < grace:
            return
        if not self._ap_started:
            dbg(2, "STA still down — start AP fallback")
            self._start_ap()
        self.mode = "ap"
        self.ip = self.ap_ip
        self.led.set_wifi(LedStatus.MODE_AP, self.ip)

    async def apply_saved(self, ssid, password, hostname):
        """User-initiated (Config tab). The one intentional radio change."""
        data = save_wifi(ssid, password, hostname)
        self._cfg = data
        self.hostname = str(data.get("hostname") or self.hostname)
        self.want_sta = bool(str(data.get("ssid") or "").strip())
        self._sta_down_ms = None
        if self.want_sta:
            self._stop_ap()
            gc.collect()
            if self.sta is None and network is not None:
                self.sta = network.WLAN(network.STA_IF)
            if self.sta is not None:
                if not self.sta.active():
                    self.sta.active(True)
                self._pm_none(self.sta)
                self._set_hostname(self.sta)
                try:
                    pw = str(data.get("password") or "")
                    self.sta.connect(str(data.get("ssid") or ""), pw)
                except Exception as exc:
                    dbg(1, "STA reconnect fail", exc)
            self.mode = "sta_wait"
            self.led.set_wifi(LedStatus.MODE_STA_WAIT)
        else:
            self._start_ap()
            self.mode = "ap"
            self.ip = self.ap_ip
            self.led.set_wifi(LedStatus.MODE_AP, self.ip)
        dbg(3, "wifi saved", "sta" if self.want_sta else "ap")

    async def forget(self):
        await self.apply_saved("", "", self.hostname)

    async def run(self):
        while True:
            self.tick()
            now = time.ticks_ms()
            if time.ticks_diff(now, self._hb_ms) >= 5000:
                self._hb_ms = now
                gc.collect()
                dbg(3, "hb AP", max(self._ap_clients, 0), "heap", self._heap())
            await asyncio.sleep_ms(20)
