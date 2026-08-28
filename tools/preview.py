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
    "unit2": "°",
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
    # Physical (CG) — immutable in preview
    "slider_min": 0.0,
    "slider_max": 600.0,
    "slider_min_2": 0.0,
    "slider_max_2": 360.0,
    # Soft window (GL/GR)
    "soft_min": 0.0,
    "soft_max": 600.0,
    "soft_min_2": 0.0,
    "soft_max_2": 360.0,
    "soft": {"min": 0.0, "max": 600.0, "min2": 0.0, "max2": 360.0},
    "session": {"enabled": True, "ss": 40.0, "sa": 100.0},
    "task": None,
    "linked": True,
    "line1": "Ready",
    "line2": "",
    "warn": False,
    "enabled": True,
    "sim": True,
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

CONFIG = {
    "axis_count": 2,
    "axis2_use": 1,
    "name": "SliderWeb preview",
    "slider_min": 0.0,
    "slider_max": 600.0,
    "slider_min_2": 0.0,
    "slider_max_2": 360.0,
    "max_speed": 100.0,
    "max_speed_2": 100.0,
    "max_accel": 500.0,
    "max_accel_2": 500.0,
    "init_speed": 40.0,
    "init_accel": 100.0,
    "unit_name": "mm",
    "unit_name_2": "deg",
    "speed_tl_mm_s": 5.0,
    "accel_tl_mm_s2": 50.0,
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
_sa = 100.0
_tgt = None
_tgt2 = None
_last_t = time.monotonic()
_task = None
_task_id = 0
_cam_until = 0.0
_TL_NEAR_MM = 1.0
_TRIG_TIME_MIN = 0.2
_TRIG_LEN_MIN = 0.01


def _hello():
    with _lock:
        return {
            "t": "hello",
            "linked": True,
            "sim": True,
            "config": dict(CONFIG),
            "soft": dict(MOCK["soft"]),
            "session": dict(MOCK["session"]),
            "task": _task_public(),
        }


def _sync_soft_fields():
    s = MOCK["soft"]
    MOCK["soft_min"] = s.get("min")
    MOCK["soft_max"] = s.get("max")
    MOCK["soft_min_2"] = s.get("min2")
    MOCK["soft_max_2"] = s.get("max2")


def _derive_axis_state(global_st, spd, acc, tgt):
    st = str(global_st or "?")
    if st in ("E", "L", "D", "H"):
        return st
    if st == "?":
        return "?"
    try:
        v = abs(float(spd)) if spd is not None else 0.0
    except (TypeError, ValueError):
        v = 0.0
    if v > 0.05:
        if st in ("A", "B"):
            return st
        return "M"
    if tgt is not None:
        try:
            float(tgt)
            return "M"
        except (TypeError, ValueError):
            pass
    return "I"


def _sync_axis_states():
    gst = MOCK.get("state") or "?"
    MOCK["state1"] = _derive_axis_state(
        gst, MOCK.get("spd"), MOCK.get("acc"), MOCK.get("tgt")
    )
    if int(MOCK.get("axes") or 1) >= 2:
        MOCK["state2"] = _derive_axis_state(
            gst, MOCK.get("spd2"), MOCK.get("acc2"), MOCK.get("tgt2")
        )


def _parse_slot(s):
    t = str(s).strip()
    if not t or t == "_" or t.lower() == "none":
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _fmt_pos(v):
    n = float(v)
    r = round(n, 1)
    if abs(r - int(r)) < 1e-6:
        return str(int(r))
    return "%s" % r


def _moving_phrase(p1, p2):
    if p1 is not None and p2 is not None:
        return "Moving to %s / %s" % (_fmt_pos(p1), _fmt_pos(p2))
    if p1 is not None:
        return "Moving to %s" % _fmt_pos(p1)
    if p2 is not None:
        return "Moving to %s" % _fmt_pos(p2)
    return "Moving"


def _task_public():
    if _task is None:
        return None
    out = {
        "name": _task.get("name"),
        "state": _task.get("state"),
        "detail": _task.get("detail") or "",
        "args": list(_task.get("args") or []),
        "id": _task.get("id"),
    }
    if _task.get("loop") is not None:
        out["loop"] = int(_task["loop"])
    if _task.get("frame") is not None and _task.get("frames") is not None:
        out["frame"] = int(_task["frame"])
        out["frames"] = int(_task["frames"])
    return out


def _cam_off():
    global _cam_until
    _cam_until = 0.0


def _cam_active():
    return time.monotonic() < _cam_until


def _cam_start(duration_s):
    global _cam_until
    try:
        dur = float(duration_s)
    except (TypeError, ValueError):
        dur = _TRIG_LEN_MIN
    if dur < _TRIG_LEN_MIN:
        dur = _TRIG_LEN_MIN
    _cam_until = time.monotonic() + dur


def _clamp_trig_time(v):
    try:
        t = float(v)
    except (TypeError, ValueError):
        t = _TRIG_TIME_MIN
    return max(_TRIG_TIME_MIN, t)


def _clamp_trig_len(v):
    try:
        t = float(v)
    except (TypeError, ValueError):
        t = _TRIG_LEN_MIN
    return max(_TRIG_LEN_MIN, t)


def _task_cancel(reason="stop"):
    global _task, _vel, _vel2, _tgt, _tgt2
    if _task is None:
        return
    sys.stderr.write("TASK cancel %s (%s)\n" % (_task.get("name"), reason))
    _cam_off()
    _task = None
    _vel = 0.0
    _vel2 = 0.0
    _tgt = None
    _tgt2 = None
    MOCK["tgt"] = None
    MOCK["tgt2"] = None
    MOCK["task"] = None
    MOCK["line1"] = "Ready"
    MOCK["line2"] = ""


def _ppm_set_targets(which):
    global _tgt, _tgt2, _vel, _vel2
    if _task is None:
        return
    if which == 1:
        p1, p2 = _task.get("pos_1"), _task.get("pos_1_2")
        _task["state"] = "move_1"
    else:
        p1, p2 = _task.get("pos_2"), _task.get("pos_2_2")
        _task["state"] = "move_2"
    _task["detail"] = _moving_phrase(p1, p2)
    _task["wait_until"] = None
    _tgt = p1
    _tgt2 = p2
    MOCK["tgt"] = _tgt
    MOCK["tgt2"] = _tgt2
    if _tgt is None:
        _vel = 0.0
    if _tgt2 is None:
        _vel2 = 0.0


def _ppm_begin_wait(which):
    if _task is None:
        return
    delay_s = float(_task.get("delay_s") or 0)
    _task["state"] = "wait_1" if which == 1 else "wait_2"
    if delay_s <= 0:
        _task["wait_until"] = time.monotonic()
        _task["detail"] = "Waiting 0.0 s"
        return
    _task["wait_until"] = time.monotonic() + delay_s
    rem = delay_s
    _task["detail"] = "Waiting %.1f s" % rem


def _task_start(line):
    global _task, _task_id, _ss
    line = str(line or "").strip()
    parts = line.split()
    if not parts:
        return False
    name = parts[0].upper()
    need = {"TSK_PPM": 5, "TSK_TL_CONT": 6, "TSK_TL_MSM": 5}.get(name)
    if need is None or len(parts) - 1 < need:
        return False
    if _task is not None:
        _task_cancel("replace")
    _task_id = (_task_id + 1) & 0xFFFF
    args = parts[1 : 1 + need]
    if name == "TSK_PPM":
        try:
            delay_s = float(args[4])
        except ValueError:
            delay_s = 0.0
        if delay_s < 0:
            delay_s = 0.0
        _task = {
            "name": name,
            "state": "move_1",
            "detail": "",
            "args": args,
            "id": _task_id,
            "loop": 0,
            "pos_1": _parse_slot(args[0]),
            "pos_1_2": _parse_slot(args[1]),
            "pos_2": _parse_slot(args[2]),
            "pos_2_2": _parse_slot(args[3]),
            "delay_s": delay_s,
            "wait_until": None,
        }
        _ppm_set_targets(1)
        MOCK["task"] = _task_public()
        MOCK["line1"] = _task["detail"]
        MOCK["line2"] = "loop 0"
        sys.stderr.write("TASK start %s %s\n" % (name, args))
        return True
    if name == "TSK_TL_CONT":
        dest1 = _parse_slot(args[0])
        dest2 = _parse_slot(args[1])
        try:
            speed = float(args[2])
        except (TypeError, ValueError):
            speed = 0.0
        try:
            accel = float(args[3])
        except (TypeError, ValueError):
            accel = 0.0
        trig_time = _clamp_trig_time(args[4])
        trig_len = _clamp_trig_len(args[5])
        if dest1 is None and dest2 is None:
            return False
        if speed <= 0:
            return False
        cur = float(MOCK["pos"])
        near = True
        if dest1 is not None and abs(cur - dest1) > _TL_NEAR_MM:
            near = False
        if dest2 is not None and abs(float(MOCK["pos2"]) - dest2) > _TL_NEAR_MM:
            near = False
        if near:
            return False
        _ss = abs(speed)
        _task = {
            "name": name,
            "state": "move",
            "detail": _moving_phrase(dest1, dest2),
            "args": args,
            "id": _task_id,
            "dest_1": dest1,
            "dest_2": dest2,
            "trig_time_s": trig_time,
            "trig_len_s": trig_len,
            "next_trig": time.monotonic(),
            "pulse_until": None,
        }
        _tgt = dest1
        _tgt2 = dest2
        MOCK["tgt"] = _tgt
        MOCK["tgt2"] = _tgt2
        MOCK["task"] = _task_public()
        MOCK["line1"] = _task["detail"]
        MOCK["line2"] = ""
        sys.stderr.write("TASK start %s %s\n" % (name, args))
        return True
    if name == "TSK_TL_MSM":
        dest1 = _parse_slot(args[0])
        dest2 = _parse_slot(args[1])
        try:
            frames = int(float(args[2]))
        except (TypeError, ValueError):
            frames = 0
        trig_time = _clamp_trig_time(args[3])
        trig_len = _clamp_trig_len(args[4])
        if dest1 is None and dest2 is None or frames < 1:
            return False
        start1 = float(MOCK["pos"])
        start2 = float(MOCK["pos2"])
        d1 = float(dest1) - start1 if dest1 is not None else 0.0
        ok = dest1 is not None and abs(d1) >= _TL_NEAR_MM
        if not ok:
            return False
        _ss = float(CONFIG.get("speed_tl_mm_s", 5.0))
        _task = {
            "name": name,
            "state": "pulse",
            "detail": "frame 0/%d" % frames,
            "args": args,
            "id": _task_id,
            "dest_1": dest1,
            "dest_2": dest2,
            "start_1": start1,
            "start_2": start2,
            "delta_1": d1,
            "delta_2": 0.0,
            "frames": frames,
            "frame": 0,
            "trig_time_s": trig_time,
            "trig_len_s": trig_len,
            "wait_until": None,
        }
        _cam_start(trig_len)
        _task["pulse_until"] = time.monotonic() + trig_len
        MOCK["task"] = _task_public()
        MOCK["line1"] = _task["detail"]
        MOCK["line2"] = ""
        sys.stderr.write("TASK start %s %s\n" % (name, args))
        return True
    return False


def _ppm_tick(now):
    """Advance PPM after motion integration for this frame."""
    global _tgt, _tgt2
    if _task is None or _task.get("name") != "TSK_PPM":
        return
    phase = _task.get("state")
    if phase in ("move_1", "move_2"):
        idle = _tgt is None and _tgt2 is None and _vel == 0.0 and _vel2 == 0.0
        if idle:
            which = 1 if phase == "move_1" else 2
            _ppm_begin_wait(which)
            if float(_task.get("delay_s") or 0) <= 0:
                _ppm_tick(now)
        return
    if phase in ("wait_1", "wait_2"):
        until = _task.get("wait_until")
        if until is None:
            until = now
        rem = until - now
        if rem < 0:
            rem = 0
        _task["detail"] = "Waiting %.1f s" % (int(rem * 10 + 0.5) * 0.1)
        if rem > 0:
            return
        if phase == "wait_1":
            _ppm_set_targets(2)
        else:
            _task["loop"] = int(_task.get("loop") or 0) + 1
            _ppm_set_targets(1)
        return


def _msm_target(n):
    if _task is None:
        return None, None
    frames = int(_task.get("frames") or 1)
    if frames < 1:
        frames = 1
    frac = float(n) / float(frames)
    p1 = float(_task.get("start_1") or 0) + float(_task.get("delta_1") or 0) * frac
    return p1, None


def _tl_cont_tick(now):
    global _tgt, _tgt2
    if _task is None or _task.get("name") != "TSK_TL_CONT":
        return
    if _task.get("state") != "move":
        return
    if not _cam_active():
        if now >= float(_task.get("next_trig") or 0):
            trig_len = float(_task.get("trig_len_s") or _TRIG_LEN_MIN)
            trig_time = float(_task.get("trig_time_s") or _TRIG_TIME_MIN)
            _cam_start(trig_len)
            _task["pulse_until"] = now + trig_len
            _task["next_trig"] = now + trig_time
            if trig_len > 1.0:
                rem = max(0.0, _task["pulse_until"] - now)
                _task["detail"] = "Trigger %.1f s" % (int(rem * 10 + 0.5) * 0.1)
            else:
                _task["detail"] = "Trigger"
    elif float(_task.get("trig_len_s") or 0) > 1.0:
        rem = max(0.0, float(_task.get("pulse_until") or 0) - now)
        _task["detail"] = "Trigger %.1f s" % (int(rem * 10 + 0.5) * 0.1)
    idle = _tgt is None and _tgt2 is None and _vel == 0.0 and _vel2 == 0.0
    if idle:
        _cam_off()
        _task_cancel("done")


def _tl_msm_tick(now):
    global _tgt, _tgt2, _task
    if _task is None or _task.get("name") != "TSK_TL_MSM":
        return
    phase = _task.get("state")
    frames = int(_task.get("frames") or 1)
    if phase == "pulse":
        if _cam_active():
            if float(_task.get("trig_len_s") or 0) > 1.0:
                rem = max(0.0, float(_task.get("pulse_until") or 0) - now)
                _task["detail"] = "Trigger %.1f s" % (int(rem * 10 + 0.5) * 0.1)
            return
        k = int(_task.get("frame") or 0) + 1
        _task["frame"] = k
        _task["detail"] = "frame %d/%d" % (k, frames)
        p1, p2 = _msm_target(k)
        _tgt = p1
        _tgt2 = p2
        MOCK["tgt"] = _tgt
        MOCK["tgt2"] = _tgt2
        _task["state"] = "move"
        _task["detail"] = _moving_phrase(p1, p2)
        return
    if phase == "move":
        idle = _tgt is None and _tgt2 is None and _vel == 0.0 and _vel2 == 0.0
        if not idle:
            return
        k = int(_task.get("frame") or 0)
        if k >= frames:
            _cam_off()
            _task = None
            MOCK["task"] = None
            MOCK["line1"] = "Ready"
            MOCK["line2"] = ""
            return
        trig_time = float(_task.get("trig_time_s") or _TRIG_TIME_MIN)
        _task["state"] = "wait"
        _task["wait_until"] = now + trig_time
        rem = trig_time
        if rem > 1.0:
            _task["detail"] = "Waiting %.1f s" % (int(rem * 10 + 0.5) * 0.1)
        else:
            _task["detail"] = "Waiting %.1f s" % rem
        return
    if phase == "wait":
        until = float(_task.get("wait_until") or 0)
        rem = until - now
        if rem < 0:
            rem = 0
        if rem > 1.0:
            _task["detail"] = "Waiting %.1f s" % (int(rem * 10 + 0.5) * 0.1)
        else:
            _task["detail"] = "Waiting %.1f s" % (int(rem * 10 + 0.5) * 0.1)
        if rem > 0:
            return
        trig_len = float(_task.get("trig_len_s") or _TRIG_LEN_MIN)
        _cam_start(trig_len)
        _task["pulse_until"] = now + trig_len
        _task["state"] = "pulse"
        return


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

        soft_lo = MOCK["soft"].get("min")
        soft_hi = MOCK["soft"].get("max")
        if soft_lo is None:
            soft_lo = MOCK.get("slider_min")
        if soft_hi is None:
            soft_hi = MOCK.get("slider_max")

        if _vel != 0.0:
            pos = float(MOCK["pos"]) + _vel * dt
            pos, hit = _clamp(pos, soft_lo, soft_hi)
            MOCK["pos"] = round(pos, 3)
            if hit:
                _vel = 0.0
                _tgt = None
                MOCK["tgt"] = None
                if _task is not None:
                    _task_cancel("error")

        soft_lo2 = MOCK["soft"].get("min2")
        soft_hi2 = MOCK["soft"].get("max2")
        if soft_lo2 is None:
            soft_lo2 = MOCK.get("slider_min_2")
        if soft_hi2 is None:
            soft_hi2 = MOCK.get("slider_max_2")

        if _vel2 != 0.0:
            pos2 = float(MOCK["pos2"]) + _vel2 * dt
            pos2, hit2 = _clamp(pos2, soft_lo2, soft_hi2)
            MOCK["pos2"] = round(pos2, 3)
            if hit2:
                _vel2 = 0.0
                _tgt2 = None
                MOCK["tgt2"] = None

        if _task is not None and _task.get("name") == "TSK_PPM":
            _ppm_tick(now)
        elif _task is not None and _task.get("name") == "TSK_TL_CONT":
            _tl_cont_tick(now)
        elif _task is not None and _task.get("name") == "TSK_TL_MSM":
            _tl_msm_tick(now)

        MOCK["spd"] = round(abs(_vel), 3)
        MOCK["spd2"] = round(abs(_vel2), 3)
        MOCK["ss"] = _ss
        MOCK["session"] = {"enabled": MOCK["enabled"], "ss": _ss, "sa": _sa}
        MOCK["task"] = _task_public()
        if _task is not None:
            MOCK["line1"] = _task.get("detail") or "Task"
            if _task.get("loop") is not None:
                MOCK["line2"] = "loop %d" % int(_task["loop"])
            elif _task.get("frame") is not None and _task.get("frames") is not None:
                MOCK["line2"] = "frame %d/%d" % (
                    int(_task["frame"]),
                    int(_task["frames"]),
                )
            else:
                MOCK["line2"] = ""
            if _task.get("state", "").startswith("move"):
                MOCK["state"] = "M"
            else:
                MOCK["state"] = "I"
        elif _vel != 0.0 or _vel2 != 0.0 or _tgt is not None or _tgt2 is not None:
            MOCK["state"] = "M"
            MOCK["line1"] = "Moving"
            MOCK["line2"] = ""
        else:
            MOCK["state"] = "I"
            MOCK["line1"] = "Ready"
            MOCK["line2"] = ""
        _sync_axis_states()


def _apply_mc(line):
    """Apply a UART-style MC command to the mock motion state."""
    global _vel, _vel2, _ss, _sa, _tgt, _tgt2
    parts = str(line or "").strip().split()
    if not parts:
        return
    cmd = parts[0].upper()
    with _lock:
        if cmd.startswith("M") and _task is not None:
            _task_cancel("move")
            # Fall through so MS/ML/… still apply after cancel.
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
            MOCK["session"]["enabled"] = en
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
            MOCK["session"]["ss"] = _ss
            if _vel != 0.0 and _tgt is None:
                _vel = _ss if _vel > 0 else -_ss
            if _vel2 != 0.0 and _tgt2 is None:
                _vel2 = _ss if _vel2 > 0 else -_ss
            return
        if cmd == "SA" and len(parts) > 1:
            try:
                _sa = abs(float(parts[1]))
            except ValueError:
                return
            MOCK["session"]["sa"] = _sa
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
        if cmd == "MJ":
            pct0 = 0.0
            pct1 = 0.0
            if len(parts) > 1:
                try:
                    pct0 = float(parts[1])
                except ValueError:
                    return
            if len(parts) > 2:
                try:
                    pct1 = float(parts[2])
                except ValueError:
                    pct1 = 0.0
            v0 = (pct0 / 100.0) * _ss
            v1 = (pct1 / 100.0) * _ss
            if abs(v0) < 1e-3:
                _vel = 0.0
                _tgt = None
                MOCK["tgt"] = None
            else:
                _vel = v0
                _tgt = None
                MOCK["tgt"] = None
            if MOCK.get("axes", 1) >= 2:
                if abs(v1) < 1e-3:
                    _vel2 = 0.0
                    _tgt2 = None
                    MOCK["tgt2"] = None
                else:
                    _vel2 = v1
                    _tgt2 = None
                    MOCK["tgt2"] = None
            MOCK["spd"] = abs(_vel)
            MOCK["spd2"] = abs(_vel2)
            if abs(_vel) < 1e-3 and abs(_vel2) < 1e-3:
                MOCK["state"] = "I"
                MOCK["line1"] = "Ready"
                MOCK["line2"] = ""
            else:
                MOCK["state"] = "M"
                MOCK["line1"] = "Joy"
                MOCK["line2"] = ""
            return
        if cmd == "MT":
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
            if len(parts) > 1 and parts[1].lower() != "none":
                try:
                    MOCK["soft"]["min"] = float(parts[1])
                except ValueError:
                    pass
            else:
                MOCK["soft"]["min"] = None
            _sync_soft_fields()
            return
        if cmd == "SR":
            if len(parts) > 1 and parts[1].lower() != "none":
                try:
                    MOCK["soft"]["max"] = float(parts[1])
                except ValueError:
                    pass
            else:
                MOCK["soft"]["max"] = None
            _sync_soft_fields()
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
        if u.path == "/api/hello":
            self._json(_hello())
            return
        if u.path == "/api/config":
            self._json(dict(CONFIG))
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
            if "task" in body:
                line = body.get("task")
                sys.stderr.write("TASK> %s\n" % line)
                with _lock:
                    _task_start(line)
            if "mc" in body:
                line = body.get("mc")
                sys.stderr.write("MC> %s\n" % line)
                _apply_mc(line)
            if "ax" in body:
                try:
                    ax = int(body.get("ax"))
                except (TypeError, ValueError):
                    ax = 1
                if ax not in (0, 1, 2):
                    ax = 1
                with _lock:
                    MOCK["ax"] = ax
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
    _sync_axis_states()
    port = int(os.environ.get("PORT", "8080"))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("SliderWeb preview  http://127.0.0.1:%d/" % port)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
