# MC_client — UART client for SliderMC (MicroPython + uasyncio).
#
# MC_API surface (duck-typed): start, send/query (up to 6 packed slots), motion
# (optional extra-axis pos / home(axis)), config setters, getters
# (motors/servos; packed axis_count from CG axis; getPosition2, …),
# set_axis_status_callback (per-axis 6-arg). Canonical copy for SliderHost /
# SliderWeb / ___SliderCtrl.
# Wire protocol: https://github.com/fablab-wue/SliderDoc/blob/main/contract/protocol.md

try:
    import uasyncio as asyncio
except ImportError:
    import asyncio

import math
import time

try:
    from machine import UART, Pin
except ImportError:
    UART = None
    Pin = None

import MC_config as cfg

try:
    from dbg import dbg
except ImportError:
    def dbg(*_a, **_k):
        pass


class MC_Client:
    """Standalone UART interface to a SliderMC motion controller (MC_API)."""

    # Match SliderMC McState (motion_api.h).
    MC_STATE_DISABLED = 0
    MC_STATE_IDLE = 1
    MC_STATE_ACCELERATING = 2
    MC_STATE_MOVING = 3
    MC_STATE_DECELERATING = 4
    MC_STATE_HOMING = 5
    MC_STATE_HARD_LIMIT = 6
    MC_STATE_ERROR = 7
    MC_STATE_LOCKED = 8
    # Index = McState; '?' = LOCKED (not emitted on UART status).
    MC_STATE_CHARS = ("D", "I", "A", "M", "B", "H", "L", "E", "P", "?")

    def __init__(self, uart_id=None, tx=None, rx=None, baud=None, uart=None):
        """``uart`` is a duck-typed stream (``.write`` / ``.any`` / ``.read``).

        Pass a pyserial wrapper from the PC host; omit it on Pico to use
        ``machine.UART``.
        """
        if uart is not None:
            self._uart = uart
            self._uart_tx = int(tx) if tx is not None else -1
            self._uart_rx = int(rx) if rx is not None else -1
            dbg(3, "UART host-stream", baud or getattr(cfg, "UART_BAUD", 115200))
        else:
            if UART is None:
                raise RuntimeError("machine.UART not available")
            if uart_id is None:
                uart_id = int(getattr(cfg, "UART_ID", 0))
            if tx is None:
                tx = getattr(cfg, "PIN_UART_TX", 16)
            if rx is None:
                rx = getattr(cfg, "PIN_UART_RX", 17)
            if baud is None:
                baud = int(getattr(cfg, "UART_BAUD", 115_200))
            self._uart_tx = int(tx)
            self._uart_rx = int(rx)
            kw = dict(
                baudrate=int(baud),
                tx=Pin(int(tx)),
                rx=Pin(int(rx)),
                bits=8,
                parity=None,
                stop=1,
            )
            try:
                self._uart = UART(uart_id, rxbuf=2048, **kw)
            except TypeError:
                self._uart = UART(uart_id, **kw)
            dbg(3, "UART", uart_id, "TX", int(tx), "RX", int(rx), baud)
        self._rx_task = None
        self._started = False
        self.linked = False  # True after welcome banner
        self._rx_buf = b""

        self._banner_event = asyncio.Event()
        self._waiters = {}  # TAG -> list of (Event, result_box)
        self._cg_collect = None  # dict while collecting bare CG dump

        # Assignable callbacks (composition; no subclass required).
        self._axis_status_cb = None
        self._error_cb = None
        self._answer_cb = None

        # Session / status cache
        self._speed_mm_s = None
        self._accel_mm_s2 = None
        self._max_speed_mm_s = None
        self._soft_min = None
        self._soft_max = None
        self._soft_min_2 = None
        self._soft_max_2 = None
        self._soft_min_3 = None
        self._soft_max_3 = None
        self._soft_min_4 = None
        self._soft_max_4 = None
        self._soft_min_5 = None
        self._soft_max_5 = None
        self._soft_min_6 = None
        self._soft_max_6 = None
        self._enabled = None

        self._motors = 1
        self._servos = 0
        self._axis = 1  # packed motors+servos, 1..6
        self._state = None  # I/M/H/L/E/D/...
        self._pos_mm = None
        self._pos_mm_2 = None
        self._pos_mm_3 = None
        self._pos_mm_4 = None
        self._pos_mm_5 = None
        self._pos_mm_6 = None
        self._act_speed_mm_s = None
        self._act_speed_mm_s_2 = None
        self._act_speed_mm_s_3 = None
        self._act_speed_mm_s_4 = None
        self._act_speed_mm_s_5 = None
        self._act_speed_mm_s_6 = None
        self._target_mm = None
        self._target_mm_2 = None
        self._target_mm_3 = None
        self._target_mm_4 = None
        self._target_mm_5 = None
        self._target_mm_6 = None

        self._moving = False
        self._homing = False
        self._at_soft_limit = False
        self._near_soft_limit = False
        self._at_hard_limit = False
        self._drv_error_active = False
        self._act_vel_mm_s = 0.0
        self._prev_act_speed_abs = None
        self._decelerating = False
        self._accelerating = False

        # Public MC config (filled by fetchConfig after banner).
        self.mc_config = {}
        self.max_speed = None
        self.max_accel = None
        self.motors = 1
        self.servos = 0
        # Envelope aliases of packed channels (channel 1 = slider_min).
        self.slider_min = None
        self.slider_max = None
        self.slider_min_2 = None
        self.slider_max_2 = None
        self.slider_min_3 = None
        self.slider_max_3 = None
        self.slider_min_4 = None
        self.slider_max_4 = None
        self.slider_min_5 = None
        self.slider_max_5 = None
        self.slider_min_6 = None
        self.slider_max_6 = None
        # Session window (GL/GR / SL/SR); init ≈ envelope.
        self.soft_min = None
        self.soft_max = None
        self.soft_min_2 = None
        self.soft_max_2 = None
        self.soft_min_3 = None
        self.soft_max_3 = None
        self.unit_name = None
        # Public McState int; LOCKED until first verbose status.
        self.status = self.MC_STATE_LOCKED

        self._motion_task = None

    @property
    def axis_count(self):
        """Packed live-channel count from CG ``motors``+``servos`` (or ``axis``)."""
        return self._axis

    def getAxisCount(self):
        return self._axis

    def getMotorCount(self):
        """STEP/DIR motor count (not packed IA)."""
        return self._motors

    def getServoCount(self):
        return self._servos

    # --- callbacks ---------------------------------------------------------

    def set_axis_status_callback(self, cb):
        """Register per-axis verbose `#…` callback.

        ``cb(axis, state, pos, speed, accel, dest)`` — ``axis`` is 1..6.
        Extra groups fire highest fitted axis first, then 1, so an axis-1
        handler already sees later-axis cache/fields. ``None`` unregisters.
        """
        self._axis_status_cb = cb

    def set_error_callback(self, cb):
        """Register cb(code, text) for `!E:` lines."""
        self._error_cb = cb

    def set_answer_callback(self, cb):
        """Register cb(command, answer) for `TAG:value` replies."""
        self._answer_cb = cb

    def on_error(self, code, text):
        """Hook / callback dispatch for `!E:<code> <text>`."""
        cb = self._error_cb
        if cb is not None:
            cb(code, text)

    def on_axis_status(self, axis, state, pos, speed, accel, dest):
        """Hook / callback dispatch for one axis group on a `#…` line."""
        cb = self._axis_status_cb
        if cb is not None:
            cb(axis, state, pos, speed, accel, dest)

    def on_answer(self, command, answer):
        """Hook / callback dispatch for `TAG:value` replies."""
        cb = self._answer_cb
        if cb is not None:
            cb(command, answer)

    # --- lifecycle ---------------------------------------------------------

    async def start(self, banner_timeout_s=3.0):
        """Open RX task, unlock MC with ``\\n``, wait for welcome ``# …``, then ``SV 1``.

        On successful banner, reads MC config via ``CG`` into ``mc_config`` /
        envelopes, then session window via ``GL``/``GR``. Seeds ``SS``/``SA``
        from CG init_speed/init_accel when present.
        """
        if self._rx_task is None:
            self._rx_task = asyncio.create_task(self._rx_loop())
        self._banner_event.clear()
        total_ms = int(float(banner_timeout_s) * 1000)
        if total_ms < 1:
            total_ms = 1
        deadline = time.ticks_add(time.ticks_ms(), total_ms)
        got_banner = False
        while not self._banner_event.is_set():
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                break
            self._uart.write(b"\n")
            try:
                await self._wait_event(self._banner_event, 0.1)
                got_banner = True
                break
            except OSError:
                continue
        if not got_banner and not self._banner_event.is_set():
            print(
                "SliderMC banner timeout (check UART wiring / baud) — continuing without MC"
            )
        self.linked = bool(got_banner or self._banner_event.is_set())
        self._started = True
        await self.send("SV", 1)
        if self.linked:
            await self._warn_protocol()
            await self.fetchConfig()
            await self.fetchSoftLimits()
        # Session SS/SA from CG init_speed/init_accel when present (no CS).
        if self._speed_mm_s is not None:
            self._cmd("SS", _fmt_arg(self._speed_mm_s))
        if self._accel_mm_s2 is not None:
            self._cmd("SA", _fmt_arg(self._accel_mm_s2))
        return self.linked

    async def _warn_protocol(self):
        try:
            vp = await self.query("VP", timeout_s=0.5)
        except OSError:
            return
        if vp is None:
            return
        v = str(vp).strip()
        if v and v != "3":
            print("SliderMC protocol %s (expected 3)" % v)

    async def fetchConfig(self, settle_ms=150):
        """Send bare ``CG`` and collect all ``CG:key=value`` lines into ``mc_config``."""
        collected = {}
        self._cg_collect = collected
        try:
            self._write_line("CG")
            idle_deadline = time.ticks_add(time.ticks_ms(), int(settle_ms))
            last_count = -1
            while time.ticks_diff(idle_deadline, time.ticks_ms()) > 0:
                await asyncio.sleep_ms(10)
                n = len(collected)
                if n != last_count:
                    last_count = n
                    idle_deadline = time.ticks_add(time.ticks_ms(), int(settle_ms))
        finally:
            self._cg_collect = None

        self.mc_config = dict(collected)
        self._apply_counts(collected)
        un = collected.get("unit_name")
        if un is not None:
            un = str(un).strip()
        self.unit_name = un if un else None
        self.max_speed = _parse_cfg_float(collected.get("max_speed_1"))
        self.max_accel = _parse_cfg_float(collected.get("max_accel_1"))
        pairs = []
        i = 1
        while i <= 6:
            pairs.append(_envelope_from_cfg(collected, i, self._motors))
            i += 1
        self.slider_min, self.slider_max = pairs[0]
        self.slider_min_2, self.slider_max_2 = pairs[1]
        self.slider_min_3, self.slider_max_3 = pairs[2]
        self.slider_min_4, self.slider_max_4 = pairs[3]
        self.slider_min_5, self.slider_max_5 = pairs[4]
        self.slider_min_6, self.slider_max_6 = pairs[5]
        self._soft_min = self.slider_min
        self._soft_max = self.slider_max
        self._soft_min_2 = self.slider_min_2
        self._soft_max_2 = self.slider_max_2
        self._soft_min_3 = self.slider_min_3
        self._soft_max_3 = self.slider_max_3
        self._soft_min_4 = self.slider_min_4
        self._soft_max_4 = self.slider_max_4
        self._soft_min_5 = self.slider_min_5
        self._soft_max_5 = self.slider_max_5
        self._soft_min_6 = self.slider_min_6
        self._soft_max_6 = self.slider_max_6
        self._sync_public_soft()
        if self.max_speed is not None:
            self._max_speed_mm_s = self.max_speed
        init_speed = _parse_cfg_float(
            collected.get("init_speed", collected.get("speed"))
        )
        init_accel = _parse_cfg_float(
            collected.get("init_accel", collected.get("accel"))
        )
        if init_speed is not None:
            self._speed_mm_s = init_speed
        if init_accel is not None:
            self._accel_mm_s2 = init_accel
        self._refresh_soft_limit_flag()
        return self.mc_config

    def _apply_counts(self, collected):
        motors_s = collected.get("motors")
        servos_s = collected.get("servos")
        if motors_s is not None or servos_s is not None:
            self._motors = _parse_count(motors_s, 1, 3, 1)
            self._servos = _parse_count(servos_s, 0, 3, 0)
            n = self._motors + self._servos
            if n < 1:
                n = 1
            if n > 6:
                n = 6
            self._axis = n
        else:
            self._axis = _parse_axis_count(collected.get("axis"))
            self._motors = self._axis
            self._servos = 0
            if self._motors > 3:
                self._servos = self._motors - 3
                self._motors = 3
        self.motors = self._motors
        self.servos = self._servos

    async def fetchSoftLimits(self, timeout_s=1.0):
        """Read live session window via ``GL`` / ``GR`` (pipe groups)."""
        mins = []
        maxs = []
        try:
            gl = await self.query("GL", timeout_s=timeout_s)
            mins = _split_pipe_fields(gl)
        except OSError:
            dbg(2, "GL timeout — keep soft_min", self.soft_min)
        try:
            gr = await self.query("GR", timeout_s=timeout_s)
            maxs = _split_pipe_fields(gr)
        except OSError:
            dbg(2, "GR timeout — keep soft_max", self.soft_max)
        attrs_min = (
            "_soft_min",
            "_soft_min_2",
            "_soft_min_3",
            "_soft_min_4",
            "_soft_min_5",
            "_soft_min_6",
        )
        attrs_max = (
            "_soft_max",
            "_soft_max_2",
            "_soft_max_3",
            "_soft_max_4",
            "_soft_max_5",
            "_soft_max_6",
        )
        i = 0
        while i < self._axis and i < 6:
            if i < len(mins):
                v = _parse_cfg_limit(mins[i])
                if v is not None or (i < len(mins) and str(mins[i]).lower() == "none"):
                    setattr(self, attrs_min[i], v)
            if i < len(maxs):
                v = _parse_cfg_limit(maxs[i])
                if v is not None or (i < len(maxs) and str(maxs[i]).lower() == "none"):
                    setattr(self, attrs_max[i], v)
            i += 1
        self._sync_public_soft()
        self._refresh_soft_limit_flag()
        return {
            "min": self.soft_min,
            "max": self.soft_max,
            "min2": self.soft_min_2,
            "max2": self.soft_max_2,
            "min3": self.soft_min_3,
            "max3": self.soft_max_3,
        }

    def _sync_public_soft(self):
        self.soft_min = self._soft_min
        self.soft_max = self._soft_max
        self.soft_min_2 = self._soft_min_2
        self.soft_max_2 = self._soft_max_2
        self.soft_min_3 = self._soft_min_3
        self.soft_max_3 = self._soft_max_3

    async def stop_rx(self):
        """Cancel the RX task (optional shutdown)."""
        t = self._rx_task
        self._rx_task = None
        if t is not None:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

    def _build_line(self, command, *args):
        """Build one MC command line (skip: extra-axis ``None`` → ``_``)."""
        cmd = str(command).strip()
        slots = list(args)
        n = self._axis
        if n < 1:
            n = 1
        if n > 6:
            n = 6
        if len(slots) > n:
            slots = slots[:n]
        while slots and slots[-1] is None:
            slots.pop()
        if not slots:
            return cmd
        parts = [cmd]
        for s in slots:
            parts.append(_fmt_slot(s))
        return " ".join(parts)

    def _cmd(self, command, *args):
        """Fire-and-forget MC line (sync; preserves UART order)."""
        self._write_line(self._build_line(command, *args))

    # --- raw send ----------------------------------------------------------

    async def send(
        self,
        command,
        arg=None,
        arg2=None,
        arg3=None,
        arg4=None,
        arg5=None,
        arg6=None,
        wait_answer=False,
        timeout_s=1.0,
    ):
        """Send one MC command.

        Builds `COMMAND`, `COMMAND arg`, or extra tokens for live axes.
        Extra-axis: ``arg is None`` with a later arg set sends skip ``_``.
        1-axis: extra args are ignored; ``arg is None`` is a bare command.
        If wait_answer, awaits matching `TAG:payload` and returns the payload
        string (spaces kept, e.g. ``IP:100 | 20`` → ``100 | 20``); else None.
        """
        cmd = str(command).strip()
        if not cmd:
            raise ValueError("empty command")
        line = self._build_line(cmd, arg, arg2, arg3, arg4, arg5, arg6)
        tag = cmd.split(None, 1)[0].upper()

        waiter = None
        if wait_answer:
            ev = asyncio.Event()
            box = [None]
            waiter = (ev, box)
            self._waiters.setdefault(tag, []).append(waiter)

        self._write_line(line)

        if not wait_answer:
            return None
        try:
            await self._wait_event(ev, timeout_s)
            return box[0]
        except OSError:
            self._drop_waiter(tag, waiter)
            raise OSError("timeout waiting for %s:" % tag)

    def _write_line(self, line):
        data = (str(line).rstrip("\r\n") + "\n").encode("ascii")
        self._uart.write(data)

    async def _wait_event(self, ev, timeout_s):
        ms = int(float(timeout_s) * 1000)
        if ms < 1:
            ms = 1
        deadline = time.ticks_add(time.ticks_ms(), ms)
        while not ev.is_set():
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                raise OSError("timeout")
            await asyncio.sleep_ms(5)

    def _drop_waiter(self, tag, waiter):
        lst = self._waiters.get(tag)
        if not lst:
            return
        try:
            lst.remove(waiter)
        except ValueError:
            pass
        if not lst:
            self._waiters.pop(tag, None)

    def _complete_waiter(self, tag, answer):
        lst = self._waiters.get(tag)
        if not lst:
            return
        ev, box = lst.pop(0)
        if not lst:
            self._waiters.pop(tag, None)
        box[0] = answer
        ev.set()

    def _seed_ip_answer(self, answer):
        nums = _split_nums(answer)
        if not nums:
            return
        attrs = (
            "_pos_mm",
            "_pos_mm_2",
            "_pos_mm_3",
            "_pos_mm_4",
            "_pos_mm_5",
            "_pos_mm_6",
        )
        i = 0
        while i < len(nums) and i < 6:
            setattr(self, attrs[i], nums[i])
            i += 1

    # --- RX ----------------------------------------------------------------

    async def _rx_loop(self):
        while True:
            n = self._uart.any()
            if n:
                chunk = self._uart.read(n)
                if chunk:
                    self._rx_buf += chunk
                    self._drain_lines()
            await asyncio.sleep_ms(2)

    def _drain_lines(self):
        while True:
            i = self._rx_buf.find(b"\n")
            if i < 0:
                if len(self._rx_buf) > 512:
                    self._rx_buf = self._rx_buf[-256:]
                return
            raw = self._rx_buf[:i]
            self._rx_buf = self._rx_buf[i + 1 :]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            try:
                line = raw.decode("ascii")
            except UnicodeError:
                continue
            self._handle_line(line)

    def _handle_line(self, line):
        if not line:
            return

        if line.startswith("# "):
            self._banner_event.set()
            return

        if len(line) >= 2 and line[0] == "#" and line[1] != " ":
            self._handle_status(line)
            return

        if line.startswith("!E:"):
            rest = line[3:].strip()
            if " " in rest:
                code, text = rest.split(" ", 1)
            else:
                code, text = rest, ""
            try:
                self.on_error(code, text)
            except Exception:
                pass
            return

        colon = line.find(":")
        if colon > 0:
            tag = line[:colon].strip()
            if tag and (" " not in tag) and tag[0].isalpha():
                answer = line[colon + 1 :]
                cmd = tag.upper()
                if cmd == "IP":
                    self._seed_ip_answer(answer)
                if cmd == "CG" and self._cg_collect is not None:
                    key, sep, val = answer.partition("=")
                    if sep:
                        self._cg_collect[key.strip()] = val.strip()
                    try:
                        self.on_answer(cmd, answer)
                    except Exception:
                        pass
                    return
                try:
                    self.on_answer(cmd, answer)
                except Exception:
                    pass
                self._complete_waiter(cmd, answer)
                return

    def _handle_status(self, line):
        # #<state> <pos> [<spd> <acc> [<dest>]] [| group2 [| …]]
        # Empty ``||`` group = idle 0. Trailing idle groups may be omitted.
        # Legacy 2-axis (no |): #<state> <pos> <pos2> [<spd> <spd2> …]
        body = line[1:].strip()
        if not body:
            return
        groups = [g.strip() for g in body.split("|")]
        if not groups:
            return
        head = groups[0].split()
        if not head:
            return
        state = head[0]
        if len(state) != 1:
            return
        groups[0] = " ".join(head[1:]) if len(head) > 1 else ""

        nfit = self._axis
        if nfit < 1:
            nfit = 1
        if nfit > 6:
            nfit = 6

        parsed = []
        ng = len(groups)
        toks0 = groups[0].split()
        legacy = ng == 1 and nfit >= 2 and len(toks0) in (2, 6, 8)
        if legacy:
            n = len(toks0)
            pos = _parse_float(toks0[0]) if n > 0 else None
            pos2 = _parse_float(toks0[1]) if n > 1 else None
            speed = speed2 = accel = accel2 = target = target2 = None
            has1 = has2 = False
            if n >= 6:
                speed = _parse_float(toks0[2])
                speed2 = _parse_float(toks0[3])
                accel = _parse_float(toks0[4])
                accel2 = _parse_float(toks0[5])
            if n >= 8:
                target = _parse_float(toks0[6])
                target2 = _parse_float(toks0[7])
                has1 = has2 = True
            parsed.append((pos, speed, accel, target, has1))
            parsed.append((pos2, speed2, accel2, target2, has2))
            while len(parsed) < nfit:
                parsed.append((0.0, 0.0, 0.0, None, False))
        else:
            i = 0
            while i < nfit:
                if i < ng:
                    parts = groups[i].split()
                    if not parts:
                        parsed.append((0.0, 0.0, 0.0, None, False))
                    else:
                        parsed.append(_parse_axis_group(parts))
                else:
                    parsed.append((0.0, 0.0, 0.0, None, False))
                i += 1

        try:
            self.status = self.MC_STATE_CHARS.index(state)
        except ValueError:
            pass

        self._state = state
        self._moving = state in ("M", "A", "B", "H", "P")
        self._homing = state == "H"
        self._at_hard_limit = state == "L"
        self._drv_error_active = state == "E"
        if state == "D":
            self._enabled = False
        elif state in ("I", "M", "H", "A", "B", "P"):
            if self._enabled is None:
                self._enabled = True

        pos_attrs = (
            "_pos_mm",
            "_pos_mm_2",
            "_pos_mm_3",
            "_pos_mm_4",
            "_pos_mm_5",
            "_pos_mm_6",
        )
        spd_attrs = (
            "_act_speed_mm_s",
            "_act_speed_mm_s_2",
            "_act_speed_mm_s_3",
            "_act_speed_mm_s_4",
            "_act_speed_mm_s_5",
            "_act_speed_mm_s_6",
        )
        tgt_attrs = (
            "_target_mm",
            "_target_mm_2",
            "_target_mm_3",
            "_target_mm_4",
            "_target_mm_5",
            "_target_mm_6",
        )
        idle = state in ("I", "D", "L", "E")
        moving_st = state in ("M", "H", "A", "B", "P")
        accels = []
        i = 0
        while i < nfit:
            pos, speed, accel, target, has_dest = parsed[i]
            if pos is not None:
                setattr(self, pos_attrs[i], pos)
            if speed is not None:
                setattr(self, spd_attrs[i], speed)
                if i == 0:
                    self._act_vel_mm_s = float(speed)
            elif idle:
                setattr(self, spd_attrs[i], 0.0)
                if i == 0:
                    self._act_speed_mm_s = 0.0
                    self._act_vel_mm_s = 0.0
            if has_dest:
                setattr(self, tgt_attrs[i], target)
            elif not moving_st:
                setattr(self, tgt_attrs[i], None)
            acc = accel
            if acc is None and idle:
                acc = 0.0
            if acc is None and i == 0:
                acc = self._accel_mm_s2
            accels.append(acc)
            i += 1

        if state == "A":
            self._accelerating = True
            self._decelerating = False
        elif state == "B":
            self._accelerating = False
            self._decelerating = True
        else:
            spd_abs = abs(self._act_vel_mm_s) if self._act_vel_mm_s is not None else 0.0
            if self._moving and not self._homing and self._prev_act_speed_abs is not None:
                eps = float(getattr(cfg, "LED_ACCEL_SPEED_EPS_MM_S", 3.0))
                delta = spd_abs - self._prev_act_speed_abs
                self._decelerating = delta < -eps
                self._accelerating = delta > eps
            else:
                self._decelerating = False
                self._accelerating = False
            self._prev_act_speed_abs = spd_abs

        if state in ("A", "B", "M", "H", "P"):
            spd_abs = abs(self._act_vel_mm_s) if self._act_vel_mm_s is not None else 0.0
            self._prev_act_speed_abs = spd_abs

        self._refresh_soft_limit_flag()

        ax = nfit
        while ax >= 1:
            pos, speed, accel, target, has_dest = parsed[ax - 1]
            dest = getattr(self, tgt_attrs[ax - 1])
            if ax == 1:
                pos = self._pos_mm
                speed = self._act_speed_mm_s
            acc = accels[ax - 1] if ax - 1 < len(accels) else accel
            try:
                self.on_axis_status(ax, state, pos, speed, acc, dest)
            except Exception:
                pass
            ax -= 1

    def _refresh_soft_limit_flag(self):
        pos = self._pos_mm
        if pos is None:
            self._at_soft_limit = False
            self._near_soft_limit = False
            return
        warn = float(getattr(cfg, "SOFT_LIMIT_WARN_MM", 10.0))
        at = False
        near = False
        if self._soft_min is not None:
            d = float(pos) - float(self._soft_min)
            if abs(d) < 1e-3 or d < 0:
                at = True
            elif d <= warn:
                near = True
        if self._soft_max is not None:
            d = float(self._soft_max) - float(pos)
            if abs(d) < 1e-3 or d < 0:
                at = True
            elif d <= warn:
                near = True
        self._at_soft_limit = at
        self._near_soft_limit = (not at) and near

    # --- configuration API (sync) ------------------------------------------

    def setSpeed(self, mm_per_sec):
        self._speed_mm_s = max(float(mm_per_sec), cfg.MIN_SPEED_MM_S)
        self._cmd("SS", _fmt_arg(self._speed_mm_s))

    def setMaxSpeed(self, mm_per_sec):
        """Persistent max_speed via CS (MC enforces planner ceiling)."""
        v = max(float(mm_per_sec), cfg.MIN_SPEED_MM_S)
        self._max_speed_mm_s = v
        self.max_speed = v
        if self._speed_mm_s is not None and self._speed_mm_s > v:
            self._speed_mm_s = v
        self._cmd("CS", "max_speed_1 %s" % _fmt_arg(v))

    def setAcceleration(self, accel):
        self._accel_mm_s2 = max(float(accel), cfg.MIN_SPEED_MM_S)
        self._cmd("SA", _fmt_arg(self._accel_mm_s2))

    def setLeft(
        self,
        pos=None,
        pos2=None,
        pos3=None,
        pos4=None,
        pos5=None,
        pos6=None,
    ):
        """Session working-window left (`SL`). All None = bare reset to envelope.

        Extra axes: ``None`` on one axis sends skip ``_``. To clear a side to
        session None, pass the string ``"none"`` or use ``setSoftLimits``.
        """
        slots = (pos, pos2, pos3, pos4, pos5, pos6)
        if all(s is None for s in slots):
            self._cmd("SL")
            self._soft_min = self.slider_min
            if self._axis >= 2:
                self._soft_min_2 = self.slider_min_2
            if self._axis >= 3:
                self._soft_min_3 = self.slider_min_3
            if self._axis >= 4:
                self._soft_min_4 = self.slider_min_4
            if self._axis >= 5:
                self._soft_min_5 = self.slider_min_5
            if self._axis >= 6:
                self._soft_min_6 = self.slider_min_6
        else:
            self._cmd("SL", pos, pos2, pos3, pos4, pos5, pos6)
            self._apply_window_slot("_soft_min", pos, self.slider_min)
            if self._axis >= 2:
                self._apply_window_slot("_soft_min_2", pos2, self.slider_min_2)
            if self._axis >= 3:
                self._apply_window_slot("_soft_min_3", pos3, self.slider_min_3)
            if self._axis >= 4:
                self._apply_window_slot("_soft_min_4", pos4, self.slider_min_4)
            if self._axis >= 5:
                self._apply_window_slot("_soft_min_5", pos5, self.slider_min_5)
            if self._axis >= 6:
                self._apply_window_slot("_soft_min_6", pos6, self.slider_min_6)
        self._sync_public_soft()
        self._refresh_soft_limit_flag()

    def setRight(
        self,
        pos=None,
        pos2=None,
        pos3=None,
        pos4=None,
        pos5=None,
        pos6=None,
    ):
        """Session working-window right (`SR`). All None = bare reset to envelope."""
        slots = (pos, pos2, pos3, pos4, pos5, pos6)
        if all(s is None for s in slots):
            self._cmd("SR")
            self._soft_max = self.slider_max
            if self._axis >= 2:
                self._soft_max_2 = self.slider_max_2
            if self._axis >= 3:
                self._soft_max_3 = self.slider_max_3
            if self._axis >= 4:
                self._soft_max_4 = self.slider_max_4
            if self._axis >= 5:
                self._soft_max_5 = self.slider_max_5
            if self._axis >= 6:
                self._soft_max_6 = self.slider_max_6
        else:
            self._cmd("SR", pos, pos2, pos3, pos4, pos5, pos6)
            self._apply_window_slot("_soft_max", pos, self.slider_max)
            if self._axis >= 2:
                self._apply_window_slot("_soft_max_2", pos2, self.slider_max_2)
            if self._axis >= 3:
                self._apply_window_slot("_soft_max_3", pos3, self.slider_max_3)
            if self._axis >= 4:
                self._apply_window_slot("_soft_max_4", pos4, self.slider_max_4)
            if self._axis >= 5:
                self._apply_window_slot("_soft_max_5", pos5, self.slider_max_5)
            if self._axis >= 6:
                self._apply_window_slot("_soft_max_6", pos6, self.slider_max_6)
        self._sync_public_soft()
        self._refresh_soft_limit_flag()

    def _apply_window_slot(self, attr, val, envelope):
        if val is None or (isinstance(val, str) and val == "_"):
            return
        if isinstance(val, str) and val.lower() == "none":
            setattr(self, attr, envelope)
            return
        setattr(self, attr, float(val))

    def getLeft(self):
        """Cached effective left (envelope after fetchConfig / bare `SL` / `none`)."""
        return self._soft_min

    def getRight(self):
        """Cached effective right (envelope after fetchConfig / bare `SR` / `none`)."""
        return self._soft_max

    def getLeft2(self):
        return self._soft_min_2

    def getRight2(self):
        return self._soft_max_2

    def getLeft3(self):
        return self._soft_min_3

    def getRight3(self):
        return self._soft_max_3

    def setSoftLimits(self, min_limit, max_limit, min_limit_2=None, max_limit_2=None):
        """Session working window via `SL`/`SR` (does not persist envelopes).

        On 2-axis MC, pass ``min_limit_2`` / ``max_limit_2`` to set axis 2 in the
        same call. ``None`` on a 2-axis side sends skip ``_`` (unchanged).
        """
        if min_limit is None:
            self._cmd("SL", "none")
            self._soft_min = self.slider_min
        elif min_limit_2 is not None and self._axis >= 2:
            self.setLeft(min_limit, min_limit_2)
        else:
            self.setLeft(min_limit)
        if max_limit is None:
            self._cmd("SR", "none")
            self._soft_max = self.slider_max
        elif max_limit_2 is not None and self._axis >= 2:
            self.setRight(max_limit, max_limit_2)
        else:
            self.setRight(max_limit)
        self._sync_public_soft()
        self._refresh_soft_limit_flag()

    def enable(self, on):
        if on and self.isDRVErrorActive():
            return
        self._enabled = bool(on)
        self._cmd("SE", 1 if self._enabled else 0)

    def estimateMoveTime(self, distance_mm, speed_mm_s, accel_mm_s2):
        d = abs(float(distance_mm))
        if d < 1e-9:
            return 0.0
        v = abs(float(speed_mm_s))
        a = abs(float(accel_mm_s2))
        if v < cfg.MIN_SPEED_MM_S or a < cfg.MIN_SPEED_MM_S:
            return 0.0
        d_r = math.pi * v * v / (4.0 * a)
        t_r = math.pi * v / (2.0 * a)
        if 2.0 * d_r <= d:
            return 2.0 * t_r + (d - 2.0 * d_r) / v
        v_pk = math.sqrt(2.0 * a * d / math.pi)
        return math.pi * v_pk / a

    def estimateMoveTimeTo(self, position_mm, speed_mm_s=None, accel_mm_s2=None):
        """Stop-to-stop sine-ramp time from current position to ``position_mm``."""
        if speed_mm_s is None:
            speed_mm_s = self._speed_mm_s if self._speed_mm_s is not None else 0.0
        if accel_mm_s2 is None:
            accel_mm_s2 = self._accel_mm_s2 if self._accel_mm_s2 is not None else 0.0
        return self.estimateMoveTime(
            float(position_mm) - self.getPosition(), speed_mm_s, accel_mm_s2
        )

    # --- getters -----------------------------------------------------------

    def getPosition(self):
        return self._pos_mm if self._pos_mm is not None else 0.0

    def getPosition2(self):
        return self._pos_mm_2 if self._pos_mm_2 is not None else 0.0

    def getPosition3(self):
        return self._pos_mm_3 if self._pos_mm_3 is not None else 0.0

    def getSpeed(self):
        return self._act_vel_mm_s if self._act_vel_mm_s is not None else 0.0

    def getSpeed2(self):
        return self._act_speed_mm_s_2 if self._act_speed_mm_s_2 is not None else 0.0

    def getSpeed3(self):
        return self._act_speed_mm_s_3 if self._act_speed_mm_s_3 is not None else 0.0

    def getAcceleration(self):
        return self._accel_mm_s2

    def getTarget(self):
        return self._target_mm

    def getTarget2(self):
        return self._target_mm_2

    def getTarget3(self):
        return self._target_mm_3

    def isMoving(self):
        return self._moving

    def isDecelerating(self):
        if not self._moving or self._homing:
            return False
        return bool(self._decelerating)

    def isHoming(self):
        return self._homing

    def isAtSoftLimit(self):
        return self._at_soft_limit

    def isNearSoftLimit(self):
        return self._near_soft_limit

    def isAtHardLimit(self):
        return self._at_hard_limit

    def isDRVErrorActive(self):
        return self._drv_error_active or self._state == "E"

    def setPosition(
        self,
        position_mm=0,
        position2=None,
        position3=None,
        position4=None,
        position5=None,
        position6=None,
    ):
        """Redefine reported pose (`SP`). ``0`` / omitted = here is zero."""
        self._cmd("SP", position_mm, position2, position3, position4, position5, position6)

    async def query(
        self,
        command,
        arg=None,
        arg2=None,
        arg3=None,
        arg4=None,
        arg5=None,
        arg6=None,
        timeout_s=1.0,
    ):
        """Send a get/is/config command and return the answer payload string."""
        return await self.send(
            command,
            arg,
            arg2,
            arg3,
            arg4,
            arg5,
            arg6,
            wait_answer=True,
            timeout_s=timeout_s,
        )

    # --- motion API (sync) -------------------------------------------------

    def moveTo(
        self,
        position,
        position2=None,
        position3=None,
        position4=None,
        position5=None,
        position6=None,
    ):
        """Absolute move. Extra-axis: ``moveTo(None, pos2)`` → ``MT _ pos2``."""
        self._cmd("MT", position, position2, position3, position4, position5, position6)

    def moveBy(
        self,
        dist,
        dist2=None,
        dist3=None,
        dist4=None,
        dist5=None,
        dist6=None,
    ):
        """Relative move. Extra-axis: ``moveBy(None, d2)`` → ``MB _ d2``."""
        self._cmd("MB", dist, dist2, dist3, dist4, dist5, dist6)

    def move(self, speed):
        # Hold-to-jog: SS then MJ ±100 (firmware rejects out-of-window MT).
        speed = float(speed)
        if abs(speed) < 1e-9:
            self._cmd("MS")
            return
        self._speed_mm_s = abs(speed)
        self._cmd("SS", _fmt_arg(abs(speed)))
        self._cmd("MJ", 100 if speed > 0 else -100)

    def home(self, axis=None):
        """Homing. ``axis`` None → ``MH`` (MC defaults to 1); ``1``/``2``/``3`` → ``MH n``.

        Extra motors are a no-op when ``getMotorCount()`` is below that number.
        Servo letters are rejected by the MC — do not home servos.
        """
        self._motion_task = asyncio.create_task(self._home_coro(axis))
        return self._motion_task

    async def _home_coro(self, axis=None):
        if axis is not None:
            axis = int(axis)
            if axis > self._motors:
                return
            self._cmd("MH", axis)
        else:
            self._cmd("MH")
        for _ in range(40):
            if self._state == "H":
                break
            await asyncio.sleep_ms(50)
        while self._state == "H":
            await asyncio.sleep_ms(50)

    def stop(self):
        if self.isDRVErrorActive():
            return
        self._cmd("MS")

    def halt(self):
        self._cmd("HT")
        self._enabled = False

    async def wait(self):
        t = self._motion_task
        if t is not None:
            try:
                await t
            except asyncio.CancelledError:
                pass
            finally:
                if self._motion_task is t:
                    self._motion_task = None
        while self.isMoving() or self.isHoming():
            await asyncio.sleep_ms(20)


def _parse_axis_count(s):
    """Parse packed CG ``axis`` (1..6). Missing/bad → 1."""
    return _parse_count(s, 1, 6, 1)


def _parse_count(s, lo, hi, default):
    n = _parse_float(s)
    if n is None:
        return default
    n = int(n)
    if n < lo:
        return lo
    if n > hi:
        return hi
    return n


def _parse_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _parse_axis_group(parts):
    """Parse one axis group: pos [spd acc [dest]]."""
    pos = _parse_float(parts[0]) if len(parts) > 0 else None
    speed = _parse_float(parts[1]) if len(parts) > 1 else None
    accel = _parse_float(parts[2]) if len(parts) > 2 else None
    has_dest = len(parts) > 3
    target = _parse_float(parts[3]) if has_dest else None
    return pos, speed, accel, target, has_dest


def _parse_cfg_float(s):
    if s is None:
        return None
    t = str(s).strip()
    if not t or t.lower() == "none":
        return None
    return _parse_float(t)


def _parse_cfg_limit(s):
    return _parse_cfg_float(s)


def _envelope_from_cfg(collected, n, motors):
    """Packed channel ``n`` (1-based): MOTOR_/SERVO_ then axis_min_N then legacy."""
    mn = mx = None
    if n <= motors:
        mn = _parse_cfg_limit(collected.get("MOTOR_%d_min" % n))
        mx = _parse_cfg_limit(collected.get("MOTOR_%d_max" % n))
    else:
        s = n - motors
        mn = _parse_cfg_limit(collected.get("SERVO_%d_min" % s))
        mx = _parse_cfg_limit(collected.get("SERVO_%d_max" % s))
    if mn is None:
        mn = _parse_cfg_limit(collected.get("axis_min_%d" % n))
    if mn is None:
        mn = _parse_cfg_limit(collected.get("slider_min_%d" % n))
    if mn is None:
        mn = _parse_cfg_limit(collected.get("soft_min_%d" % n))
    if mx is None:
        mx = _parse_cfg_limit(collected.get("axis_max_%d" % n))
    if mx is None:
        mx = _parse_cfg_limit(collected.get("slider_max_%d" % n))
    if mx is None:
        mx = _parse_cfg_limit(collected.get("soft_max_%d" % n))
    return mn, mx


def _split_nums(s):
    """Split a query payload into floats (``'100 | 20'`` → ``[100.0, 20.0]``)."""
    out = []
    if s is None:
        return out
    t = str(s).replace("|", " ")
    for part in t.split():
        v = _parse_float(part)
        if v is not None:
            out.append(v)
    return out


def _split_pipe_fields(s):
    if s is None:
        return []
    return [p.strip() for p in str(s).split("|")]


def _fmt_arg(v):
    if isinstance(v, int) or (isinstance(v, float) and v == int(v)):
        return str(int(v))
    s = "%.4f" % float(v)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _fmt_slot(v):
    """Format one MC arg; ``None`` → skip token ``_``."""
    if v is None:
        return "_"
    if isinstance(v, str):
        return v
    return _fmt_arg(v)
