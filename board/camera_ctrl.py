# camera_ctrl — CAMERA_CTRL GPIO pulse helper (timelapse tasks).

import time

import SW_config as cfg
from dbg import dbg

try:
    from machine import Pin
except ImportError:
    Pin = None


class CameraCtrl:
    """Active-high camera trigger on PIN_CAMERA_CTRL."""

    def __init__(self, pin=None, pulse_ms=None):
        if pin is None:
            pin = getattr(cfg, "PIN_CAMERA_CTRL", None)
        if pulse_ms is None:
            pulse_ms = int(getattr(cfg, "CAMERA_PULSE_MS", 100))
        self.pulse_ms = max(1, int(pulse_ms))
        self._pin = None
        self._level = 0
        self._pulse_until_ms = 0
        if pin is None or Pin is None:
            dbg(3, "camera_ctrl disabled (no pin / no machine.Pin)")
            return
        try:
            self._pin = Pin(int(pin), Pin.OUT)
            self._pin.value(0)
            dbg(3, "camera_ctrl GP%s" % int(pin))
        except Exception as exc:
            dbg(1, "camera_ctrl init fail", exc)
            self._pin = None

    def _now_ms(self):
        if hasattr(time, "ticks_ms"):
            return time.ticks_ms()
        return int(time.monotonic() * 1000)

    def _ticks_diff(self, end, start):
        if hasattr(time, "ticks_diff"):
            return time.ticks_diff(end, start)
        return end - start

    def _ticks_add(self, base, delta):
        if hasattr(time, "ticks_add"):
            return time.ticks_add(base, delta)
        return base + delta

    def set(self, on):
        self._level = 1 if on else 0
        if self._pin is not None:
            self._pin.value(self._level)

    def off(self):
        self._pulse_until_ms = 0
        self.set(False)

    def is_active(self):
        return bool(self._level)

    def pulse_remaining_ms(self):
        if not self.is_active() or not self._pulse_until_ms:
            return 0
        rem = self._ticks_diff(self._pulse_until_ms, self._now_ms())
        return rem if rem > 0 else 0

    def start_pulse_s(self, duration_s):
        """Non-blocking pulse; release via tick()."""
        try:
            dur_s = float(duration_s)
        except (TypeError, ValueError):
            dur_s = 0.01
        if dur_s < 0.01:
            dur_s = 0.01
        ms = max(10, int(dur_s * 1000 + 0.5))
        self._pulse_until_ms = self._ticks_add(self._now_ms(), ms)
        self.set(True)

    def start_pulse_ms(self, ms=None):
        if ms is None:
            ms = self.pulse_ms
        ms = max(1, int(ms))
        self._pulse_until_ms = self._ticks_add(self._now_ms(), ms)
        self.set(True)

    def tick(self):
        if not self.is_active() or not self._pulse_until_ms:
            return
        if self._ticks_diff(self._pulse_until_ms, self._now_ms()) >= 0:
            self.off()

    def pulse(self, ms=None):
        """Drive high for pulse_ms (blocking)."""
        dur = self.pulse_ms if ms is None else max(1, int(ms))
        self.set(True)
        if hasattr(time, "sleep_ms"):
            time.sleep_ms(dur)
        else:
            time.sleep(dur / 1000.0)
        self.set(False)
