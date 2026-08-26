# Keep USB CDC free long enough for Thonny Stop / file upload.

import gc
import sys
import time

gc.collect()

_delay = 2.0
try:
    import SW_config as _cfg

    _delay = float(getattr(_cfg, "SW_BOOT_DELAY_S", 2.0))
except Exception:
    pass

if _delay > 0:
    print("SliderWeb: REPL window %.0fs (Thonny Stop/Restart or Ctrl-C)" % _delay)
    try:
        sys.stdout.flush()
    except Exception:
        pass
    try:
        time.sleep(_delay)
    except KeyboardInterrupt:
        print("Stopped — REPL, main.py will not start the app")
        sys.sliderweb_hold_repl = True
        raise SystemExit
