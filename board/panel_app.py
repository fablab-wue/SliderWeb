# panel_app — thin WLAN↔UART bridge: {"mc":"SS 40"} + {"wdt":"alive"}.
# Panel business logic lives in www/js; Pico relays and WDT-stops.

try:
    import uasyncio as asyncio
except ImportError:
    import asyncio

if not hasattr(asyncio, "sleep_ms"):
    async def _sleep_ms(ms):
        await asyncio.sleep(ms / 1000.0)

    asyncio.sleep_ms = _sleep_ms

import time

import SW_config as cfg
from button_state import allow_move_out_of_soft_limit
from dbg import dbg
from virt_buttons import VirtualButton

_WARN = (
    "Hard limit",
    "Soft limit",
    "Halt",
    "Homing abort",
    "Disabled",
    "DRV error",
    "Set SPEED",
)


class PanelApp:
    def __init__(self, mc, led, sim=False):
        self.mc = mc
        self.led = led
        self.sim = bool(sim)
        self._sim_n = 0
        if led is not None:
            led.panel = self
        tap = int(getattr(cfg, "SW_MOVE_TAP_MS", 333))
        long_ms = int(getattr(cfg, "SW_LONG_PRESS_MS", 1000))
        halt_ms = int(getattr(cfg, "SW_STOP_HALT_MS", 1000))
        dis_ms = int(getattr(cfg, "SW_STOP_DISABLE_MS", 2000))
        self.tap_ms = tap
        self.halt_ms = halt_ms
        self.disable_ms = dis_ms
        self.move_l = VirtualButton(long_ms=tap)
        self.move_r = VirtualButton(long_ms=tap)
        self.fast_l = VirtualButton(long_ms=tap)
        self.fast_r = VirtualButton(long_ms=tap)
        self.stop_btn = VirtualButton(long_ms=halt_ms, extra_long_ms=dis_ms)
        self.axis_mask = 1
        self.cruise_dir = 0  # -1 / +1 / 0
        self.cruise_locked = False
        self.hold_to_run = False
        self.fast_dir = 0
        self.mode = "idle"  # idle|cruise|fast|homing
        self.line1 = "Ready"
        self.line2 = ""
        self._flash = None
        self._flash_until = 0
        self.last_client_ms = time.ticks_ms()
        self._wdt_armed = False
        self._wdt_tripped = False
        self._cmd_spd = float(getattr(cfg, "SW_SPEED_MIN_MM_S", 1.0))
        self._act = {
            "state": "?",
            "pos": None,
            "spd": None,
            "acc": None,
            "tgt": None,
            "pos2": None,
            "spd2": None,
            "acc2": None,
            "tgt2": None,
        }
        self.bind_mc(mc)

    def bind_mc(self, mc):
        self.mc = mc
        if mc is None:
            return
        mc.set_status2_callback(self._on_status2)
        mc.set_status_callback(self._on_status)
        mc.set_error_callback(self._on_error)
        if getattr(mc, "_speed_mm_s", None) is not None:
            self._cmd_spd = float(mc._speed_mm_s)

    def _on_error(self, code, text):
        dbg(1, "MC !E", code, text)
        self.flash("DRV error")

    def _on_status(self, state, pos, speed, accel, target):
        self._act["state"] = state
        self._act["pos"] = pos
        self._act["spd"] = speed
        self._act["acc"] = accel
        self._act["tgt"] = target

    def _on_status2(
        self, state, pos, pos2, speed, speed2, accel, accel2, target, target2
    ):
        self._act["state"] = state
        self._act["pos"] = pos
        self._act["pos2"] = pos2
        self._act["spd"] = speed
        self._act["spd2"] = speed2
        self._act["acc"] = accel
        self._act["acc2"] = accel2
        self._act["tgt"] = target
        self._act["tgt2"] = target2
        if state == "H":
            self.mode = "homing"
        elif state == "L":
            self.flash("Hard limit")
            self._idle_motion()
        elif state == "D":
            if self.mode != "idle":
                self._idle_motion()

    def flash(self, msg):
        self._flash = str(msg)
        self._flash_until = time.ticks_add(
            time.ticks_ms(), int(getattr(cfg, "SW_FLASH_MS", 1500))
        )

    def is_drv_error(self):
        mc = self.mc
        return bool(mc is not None and mc.isDRVErrorActive())

    def is_hard_limit(self):
        mc = self.mc
        return bool(mc is not None and mc.isAtHardLimit())

    def is_homing(self):
        mc = self.mc
        return bool(mc is not None and mc.isHoming())

    def is_moving_cruise(self):
        mc = self.mc
        if mc is None:
            return False
        return bool(mc.isMoving() and not mc.isHoming())

    def is_accel_decel(self):
        mc = self.mc
        if mc is None:
            return False
        return bool(mc.isDecelerating() or getattr(mc, "_accelerating", False))

    def is_enabled(self):
        if self.sim:
            return True
        mc = self.mc
        if mc is None:
            return False
        en = getattr(mc, "_enabled", None)
        return bool(en)

    def is_at_soft_limit(self):
        mc = self.mc
        return bool(mc is not None and mc.isAtSoftLimit())

    def is_near_soft_limit(self):
        mc = self.mc
        return bool(mc is not None and mc.isNearSoftLimit())

    def _signed_speed(self, positive, speed):
        left_neg = bool(getattr(cfg, "SW_LEFT_IS_NEGATIVE", True))
        mag = abs(float(speed))
        if positive:
            return mag if not left_neg else mag
        return -mag

    def _dir_speed(self, direction):
        """direction -1 left / +1 right → signed mm/s for mc.move()."""
        spd = self._cmd_spd
        left_neg = bool(getattr(cfg, "SW_LEFT_IS_NEGATIVE", True))
        if direction < 0:
            return -spd if left_neg else spd
        return spd if left_neg else -spd

    def _enable(self):
        mc = self.mc
        if mc is None:
            return
        if mc.isDRVErrorActive():
            return
        mc.enable(True)

    def _idle_motion(self):
        self.cruise_dir = 0
        self.cruise_locked = False
        self.hold_to_run = False
        self.fast_dir = 0
        if self.mode != "homing":
            self.mode = "idle"

    def _can_move(self, direction):
        mc = self.mc
        if mc is None:
            return True
        if mc.isDRVErrorActive():
            return False
        if not mc.isAtSoftLimit():
            return True
        return allow_move_out_of_soft_limit(
            mc.getPosition(),
            direction if getattr(cfg, "SW_LEFT_IS_NEGATIVE", True) else -direction,
            mc.slider_min,
            mc.slider_max,
        )

    def _jog(self, direction, speed=None, fast=False):
        if self.sim:
            return
        mc = self.mc
        if mc is None:
            self.flash("No MC")
            return
        if not self._can_move(direction):
            self.flash("Soft limit")
            return
        self._enable()
        if speed is None:
            speed = self._max_spd() if fast else self._cmd_spd
        signed = self._dir_speed(direction)
        # _dir_speed already signed; pass magnitude via move()
        mag = abs(float(speed))
        signed = mag if signed > 0 else -mag
        mc.move(signed, self.axis_mask)
        if fast:
            self.mode = "fast"
            self.fast_dir = direction
            self.cruise_dir = 0
            self.cruise_locked = False
        else:
            self.mode = "cruise"
            self.cruise_dir = direction
            self.fast_dir = 0

    def _stop(self):
        if self.sim:
            self._idle_motion()
            return
        mc = self.mc
        if mc is not None:
            mc.stop()
        self._idle_motion()

    def _halt(self):
        if self.sim:
            self._idle_motion()
            self.flash("Halt")
            return
        mc = self.mc
        if mc is not None:
            mc.halt()
        self._idle_motion()
        self.flash("Halt")

    def _disable(self):
        if self.sim:
            self._idle_motion()
            self.flash("Disabled")
            return
        mc = self.mc
        if mc is not None:
            mc.enable(False)
        self._idle_motion()
        self.flash("Disabled")

    def _max_spd(self):
        mc = self.mc
        cap = float(getattr(cfg, "SW_SPEED_MAX_MM_S", 100.0))
        if mc is not None and mc.max_speed is not None:
            try:
                cap = min(cap, float(mc.max_speed))
            except (TypeError, ValueError):
                pass
        return cap

    def _min_spd(self):
        return float(getattr(cfg, "SW_SPEED_MIN_MM_S", 1.0))

    def set_speed(self, mm_s):
        lo = self._min_spd()
        hi = self._max_spd()
        try:
            v = float(mm_s)
        except (TypeError, ValueError):
            return
        if v < lo:
            v = lo
        if v > hi:
            v = hi
        hyst = float(getattr(cfg, "SW_SS_HYST_MM_S", 0.05))
        if abs(v - self._cmd_spd) < hyst:
            return
        self._cmd_spd = v
        if self.sim:
            return
        mc = self.mc
        if mc is None:
            return
        mc.setSpeed(v)
        if self.cruise_dir != 0 and self.mode == "cruise":
            self._jog(self.cruise_dir, v, fast=False)

    def set_axis_mask(self, mask):
        try:
            m = int(mask)
        except (TypeError, ValueError):
            m = 1
        if m not in (0, 1, 2):
            m = 1
        self.axis_mask = m

    def _echo_mc(self, line):
        if not bool(getattr(cfg, "SW_MC_USB_ECHO", True)):
            return
        dbg(2, "MC>", line)

    def _mc_line_ok(self, line):
        """Allow printable ASCII command lines only (no CR/LF inside)."""
        if not line or len(line) > int(getattr(cfg, "SW_MC_LINE_MAX", 80)):
            return False
        for ch in line:
            o = ord(ch)
            if o < 32 or o > 126:
                return False
        return True

    def send_mc_line(self, line):
        """Relay one SliderMC command line (JS → UART). Returns True if sent."""
        line = str(line or "").strip()
        if not self._mc_line_ok(line):
            dbg(2, "mc reject", repr(line)[:40])
            return False
        self._echo_mc(line)
        if self.sim:
            return True
        mc = self.mc
        if mc is None:
            return False
        try:
            mc._write_line(line)
        except Exception as exc:
            dbg(1, "mc write fail", exc)
            return False
        return True

    def on_ws_msg(self, obj):
        """Thin bridge: ``{"mc":"SS 40"}`` and ``{"wdt":"alive"}`` only."""
        if not isinstance(obj, dict):
            return
        if "wdt" in obj:
            self.last_client_ms = time.ticks_ms()
            self._wdt_armed = True
            self._wdt_tripped = False
        if "mc" in obj:
            self.send_mc_line(obj.get("mc"))

    def watchdog_tick(self):
        """If WDT packets stop for SW_WDT_TIMEOUT_MS, send MS (stop) to MC."""
        if not getattr(self, "_wdt_armed", False):
            return
        if getattr(self, "_wdt_tripped", False):
            return
        wd = int(getattr(cfg, "SW_WDT_TIMEOUT_MS", 2500))
        if wd < 500:
            wd = 500
        if time.ticks_diff(time.ticks_ms(), self.last_client_ms) < wd:
            return
        dbg(2, "wdt timeout — MS")
        self._wdt_tripped = True
        self.send_mc_line("MS")
        self._idle_motion()

    def _dispatch_btn(self, name, ev, ms):
        """Legacy panel buttons — unused after thin-bridge cut-over."""
        return

    def _refresh_lines(self):
        now = time.ticks_ms()
        if self._flash is not None and time.ticks_diff(self._flash_until, now) > 0:
            self.line1 = self._flash
        elif self.sim:
            self.line1 = "Sim"
        elif self.is_drv_error():
            self.line1 = "DRV error"
        elif not self.is_enabled() and self._act.get("state") in ("D", None, "?"):
            if self._act.get("state") == "D":
                self.line1 = "Disabled"
            elif self.mc is None:
                self.line1 = "No MC"
            else:
                self.line1 = "Ready"
        elif self.is_homing():
            self.line1 = "Homing..."
        elif self.mode == "fast":
            self.line1 = "Fast L" if self.fast_dir < 0 else "Fast R"
        elif self.mode == "cruise":
            side = "L" if self.cruise_dir < 0 else "R"
            self.line1 = "Cruising " + side
        elif self._act.get("state") == "M":
            self.line1 = "Moving..."
        elif self._act.get("state") == "A":
            self.line1 = "Moving..."
        elif self._act.get("state") == "B":
            self.line1 = "Moving..."
        else:
            self.line1 = "Ready"

        extras = []
        if self.is_hard_limit():
            extras.append("Hard limit")
        elif self.is_at_soft_limit():
            extras.append("Soft limit")
        elif self.is_near_soft_limit():
            extras.append("Near limit")
        self.line2 = extras[0] if extras else ""

    def _n(self, v, nd=1):
        if v is None:
            return None
        try:
            return round(float(v), nd)
        except (TypeError, ValueError):
            return None

    def status_dict(self):
        self._refresh_lines()
        mc = self.mc
        axes = 1
        unit = "mm"
        smin = smax = smin2 = smax2 = None
        max_spd = self._max_spd()
        if mc is not None:
            axes = int(mc.axis_count or 1)
            if mc.unit_name:
                unit = mc.unit_name
            smin = mc.slider_min
            smax = mc.slider_max
            smin2 = mc.slider_min_2
            smax2 = mc.slider_max_2
            if mc.max_speed is not None:
                max_spd = self._max_spd()
        elif self.sim:
            axes = 2
            smin = 0.0
            smax = 600.0
            smin2 = 0.0
            smax2 = 360.0
        st = self._act.get("state") or "?"
        if len(str(st)) != 1:
            st = "?"
        warn = self.line1 in _WARN or self.line2 in _WARN
        out = {
            "state": st,
            "axes": axes,
            "unit": unit,
            "pos": self._n(self._act.get("pos")),
            "spd": self._n(self._act.get("spd")),
            "acc": self._n(self._act.get("acc")),
            "tgt": self._n(self._act.get("tgt")),
            "ss": self._n(self._cmd_spd),
            "spd_min": self._n(self._min_spd()),
            "max_speed": self._n(max_spd),
            "slider_min": self._n(smin),
            "slider_max": self._n(smax),
            "line1": self.line1,
            "line2": self.line2,
            "warn": warn,
            "enabled": self.is_enabled(),
            "sim": bool(self.sim),
            "ax": self.axis_mask,
        }
        if self.sim:
            out["n"] = self._sim_n
        if axes >= 2:
            out["pos2"] = self._n(self._act.get("pos2"))
            out["spd2"] = self._n(self._act.get("spd2"))
            out["acc2"] = self._n(self._act.get("acc2"))
            out["tgt2"] = self._n(self._act.get("tgt2"))
            out["slider_min_2"] = self._n(smin2)
            out["slider_max_2"] = self._n(smax2)
        return out

    def config_dict(self):
        mc = self.mc
        if mc is None:
            return {
                "axis_count": 2 if self.sim else 1,
                "axis2_use": 1 if self.sim else 0,
            }
        cfg_map = dict(mc.mc_config) if mc.mc_config else {}
        cfg_map["axis_count"] = int(mc.axis_count or 1)
        return cfg_map

    def _sim_push(self):
        """Dummy verbose as if `#I 0 0 0 0` / 2-axis zeros arrived."""
        self._sim_n = (self._sim_n + 1) & 0xFFFF
        self._on_status("I", 0.0, 0.0, 0.0, 0.0)
        self._on_status2("I", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    async def run(self):
        sim_hz = float(getattr(cfg, "SW_MC_SIM_HZ", 10))
        if sim_hz < 1:
            sim_hz = 1
        sim_ms = int(1000.0 / sim_hz)
        if sim_ms < 20:
            sim_ms = 20
        while True:
            self.watchdog_tick()
            if self.sim:
                self._sim_push()
            self._refresh_lines()
            await asyncio.sleep_ms(sim_ms if self.sim else 50)
