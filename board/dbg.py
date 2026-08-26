# dbg — USB CDC debug prints (CRLF-prefixed, like SliderCtrl UIC_base).

import sys
import time

import SW_config as cfg

try:
    DEBUG_LEVEL = int(getattr(cfg, "DEBUG_LEVEL", 3))
except (TypeError, ValueError):
    DEBUG_LEVEL = 3

RING = []
RING_MAX = 80


def dbg(level, *args):
    if DEBUG_LEVEL < level:
        return
    try:
        line = " ".join(str(a) for a in args)
    except Exception:
        line = "?"
    msg = "%d %s" % (time.ticks_ms(), line)
    RING.append(msg)
    if len(RING) > RING_MAX:
        del RING[: RING_MAX // 2]
    try:
        sys.stdout.write("\r\n" + msg + "\r\n")
        sys.stdout.flush()
    except Exception:
        pass


def dump_ring(path="join.log"):
    n = len(RING)
    try:
        with open(path, "w") as f:
            f.write("\n".join(RING))
            f.write("\n")
    except Exception as exc:
        dbg(2, "join.log fail", exc)
        return
    dbg(3, "join.log", n, "lines")
    for msg in RING[-12:]:
        try:
            sys.stdout.write("\r\nreplay " + msg + "\r\n")
            sys.stdout.flush()
        except Exception:
            return
