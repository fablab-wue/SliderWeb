# led_status — JKSlider RGB PWM (GP2/3/4) + last-octet IP Morse + WLAN overlays.
# WS2812 is disabled (PIN_NEOPIXEL = None).

import time

try:
    import uasyncio as asyncio
except ImportError:
    import asyncio

if not hasattr(asyncio, "sleep_ms"):
    async def _sleep_ms(ms):
        await asyncio.sleep(ms / 1000.0)

    asyncio.sleep_ms = _sleep_ms

import SW_config as cfg
from dbg import dbg

try:
    from machine import PWM, Pin
except ImportError:
    PWM = None
    Pin = None


def _clamp(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = 0
    if v < 0:
        return 0
    if v > 255:
        return 255
    return v


class LedStatus:
    """Drive a 3-pin RGB LED (PWM). Call tick() often, or start() the loop."""

    MODE_AP = "ap"
    MODE_STA_WAIT = "sta_wait"
    MODE_STA = "sta"
    MODE_GRACE = "grace"

    def __init__(self):
        self._pwm = (None, None, None)
        self._active_high = bool(getattr(cfg, "LED_ACTIVE_HIGH", True))
        hz = int(getattr(cfg, "LED_PWM_HZ", 1000))
        pins = (
            getattr(cfg, "PIN_LED_R", 2),
            getattr(cfg, "PIN_LED_G", 3),
            getattr(cfg, "PIN_LED_B", 4),
        )
        if PWM is not None and Pin is not None:
            out = []
            try:
                for p in pins:
                    pwm = PWM(Pin(int(p)))
                    pwm.freq(hz)
                    out.append(pwm)
                self._pwm = tuple(out)
            except Exception as exc:
                dbg(1, "RGB PWM init fail", exc)
                self._pwm = (None, None, None)
        self.wifi_mode = self.MODE_AP
        self.ip_last = None
        self._morse_i = 0
        self._morse_until = 0
        self._morse_on = False
        self._morse_seq = []
        self.panel = None
        self._last_rgb = None

    def set_wifi(self, mode, ip=None):
        prev = self.wifi_mode
        self.wifi_mode = mode
        octet = None
        if ip:
            try:
                octet = int(str(ip).split(".")[-1])
            except (TypeError, ValueError):
                octet = None
        if octet != self.ip_last:
            self.ip_last = octet
            self._morse_seq = self._build_morse(octet)
            self._morse_i = 0
            self._morse_until = 0
        if mode != prev:
            dbg(4, "LED wifi", mode, ip)

    def _build_morse(self, octet):
        if octet is None:
            return []
        s = "%d" % int(octet)
        seq = []
        on_ms = 90
        gap_ms = 90
        digit_gap = 280
        long0 = 320
        for i, ch in enumerate(s):
            d = int(ch)
            if d == 0:
                seq.append((True, long0))
                seq.append((False, digit_gap))
            else:
                for k in range(d):
                    seq.append((True, on_ms))
                    seq.append((False, gap_ms if k < d - 1 else digit_gap))
        seq.append((False, 1100))
        return seq

    def _morse_rgb(self, now):
        seq = self._morse_seq
        if not seq:
            return None
        if time.ticks_diff(self._morse_until, now) <= 0:
            on, ms = seq[self._morse_i]
            self._morse_on = on
            self._morse_until = time.ticks_add(now, ms)
            self._morse_i = (self._morse_i + 1) % len(seq)
        if self._morse_on:
            return (0, 180, 220)
        return (0, 0, 0)

    def _duty(self, level255):
        d = int(_clamp(level255) / 255.0 * 65535 + 0.5)
        if not self._active_high:
            d = 65535 - d
        return d

    def _set(self, r, g, b):
        rgb = (_clamp(r), _clamp(g), _clamp(b))
        if rgb == self._last_rgb:
            return
        self._last_rgb = rgb
        for pwm, level in zip(self._pwm, rgb):
            if pwm is None:
                continue
            try:
                pwm.duty_u16(self._duty(level))
            except Exception:
                pass

    def tick(self, now=None):
        if now is None:
            now = time.ticks_ms()
        p = self.panel
        if p is not None and p.is_drv_error():
            self._set(255, 0, 0)
            return
        if p is not None and p.is_hard_limit():
            if ((now // 80) % 2) == 0:
                self._set(255, 0, 0)
            else:
                self._set(0, 0, 0)
            return
        if p is not None and p.is_homing():
            if ((now // 250) % 2) == 0:
                self._set(255, 0, 0)
            else:
                self._set(0, 0, 0)
            return
        if p is not None and p.is_moving_cruise():
            if p.is_accel_decel():
                self._set(255, 255, 0)
            else:
                self._set(0, 255, 0)
            return

        if self.wifi_mode == self.MODE_STA_WAIT:
            if ((now // 300) % 2) == 0:
                self._set(255, 180, 0)
            else:
                self._set(0, 0, 0)
            return
        if self.wifi_mode == self.MODE_AP:
            phase = (now // 20) % 100
            if phase > 50:
                phase = 100 - phase
            c = 40 + phase * 3
            self._set(0, c, c)
            return
        if self.wifi_mode == self.MODE_STA and self.ip_last is not None:
            m = self._morse_rgb(now)
            if m is not None:
                r, g, b = m
                if p is not None and p.is_enabled():
                    if r == 0 and g == 0 and b == 0:
                        self._set(28, 28, 28)
                        return
                self._set(r, g, b)
                return

        if p is not None and p.is_enabled():
            r, g, b = 30, 30, 30
            if p.is_at_soft_limit():
                b = 255
            elif p.is_near_soft_limit():
                b = 80
            self._set(r, g, b)
            return
        self._set(30, 10, 0)

    async def run(self):
        while True:
            self.tick()
            await asyncio.sleep_ms(40)
