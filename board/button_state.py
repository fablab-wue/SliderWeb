"""Shared button-state semantics for slider controls.

Ported from SliderCtrl/button_state.py (MicroPython-safe: no dataclasses).
"""

import time as _time

if not hasattr(_time, "ticks_ms"):
    _time.ticks_ms = lambda: int(_time.monotonic() * 1000.0)

if not hasattr(_time, "ticks_diff"):
    def _ticks_diff(a, b):
        return int(a - b)

    _time.ticks_diff = _ticks_diff

time = _time


class MoveSemanticState:
    """Normalized result for a left/right move pair."""

    def __init__(
        self,
        direction=0,
        active=False,
        boost=False,
        start_edge=False,
        short_release_latched=False,
        long_release_stop=False,
        hold_to_run=False,
    ):
        self.direction = direction
        self.active = active
        self.boost = boost
        self.start_edge = start_edge
        self.short_release_latched = short_release_latched
        self.long_release_stop = long_release_stop
        self.hold_to_run = hold_to_run


class ButtonAdapter:
    """Wrap a raw button provider into the shared button-state interface."""

    def __init__(self, raw_provider, debounce_ms=20, long_ms=1000, extra_long_ms=None):
        self._provider = raw_provider
        self.state = ButtonState(
            debounce_ms=debounce_ms,
            long_ms=long_ms,
            extra_long_ms=extra_long_ms,
        )

    def update(self):
        self.state.update(self._provider())

    def __getattr__(self, name):
        return getattr(self.state, name)

    @property
    def pressed(self):
        return self.state.pressed()

    @property
    def hold_ms(self):
        return self.state.hold_ms


class ButtonState:
    """Generic debounced button tracker for non-hardware code."""

    def __init__(self, debounce_ms=20, long_ms=1000, extra_long_ms=None):
        self.debounce_ms = int(debounce_ms)
        self.long_ms = int(long_ms)
        self.extra_long_ms = None if extra_long_ms is None else int(extra_long_ms)

        self._stable = False
        self._raw_last = False
        self._change_ms = 0
        self._down_ms = None
        self._long_fired = False
        self._extra_long_fired = False

        self.edge_press = False
        self.edge_release = False
        self.short_press = False
        self.long_press = False
        self.extra_long_press = False
        self.last_hold_ms = 0

    def pressed(self):
        return self._stable

    def hold_ms(self, now_ms=None):
        if self._stable and self._down_ms is not None:
            if now_ms is None:
                now_ms = time.ticks_ms()
            return now_ms - self._down_ms
        return 0

    def update(self, raw):
        self.edge_press = False
        self.edge_release = False
        self.short_press = False
        self.long_press = False
        self.extra_long_press = False

        raw = bool(raw)
        now = time.ticks_ms()

        if raw != self._raw_last:
            self._raw_last = raw
            self._change_ms = now

        if time.ticks_diff(now, self._change_ms) >= self.debounce_ms:
            if raw != self._stable:
                self._stable = raw
                if self._stable:
                    self.edge_press = True
                    self._down_ms = now
                    self._long_fired = False
                    self._extra_long_fired = False
                    self.last_hold_ms = 0
                else:
                    self.edge_release = True
                    if self._down_ms is not None:
                        self.last_hold_ms = time.ticks_diff(now, self._down_ms)
                        if not self._long_fired:
                            self.short_press = True
                    self._down_ms = None

        if self._stable and self._down_ms is not None:
            held = time.ticks_diff(now, self._down_ms)
            if not self._long_fired and held >= self.long_ms:
                self._long_fired = True
                self.long_press = True
            if (
                self.extra_long_ms is not None
                and not self._extra_long_fired
                and held >= self.extra_long_ms
            ):
                self._extra_long_fired = True
                self.extra_long_press = True


def _last_motion_dir(btn_a, btn_b, tap_ms):
    if btn_a.edge_press and not btn_b.pressed():
        return -1
    if btn_b.edge_press and not btn_a.pressed():
        return 1
    if btn_a.edge_release:
        return -1
    if btn_b.edge_release:
        return 1
    if btn_a.pressed() and not btn_b.pressed():
        return -1
    if btn_b.pressed() and not btn_a.pressed():
        return 1
    return 0


def resolve_move_semantics(left, right, option_active, tap_ms=333):
    direction = _last_motion_dir(left, right, tap_ms)

    active = (left.pressed() and not right.pressed()) or (
        right.pressed() and not left.pressed()
    )
    boost = active and bool(option_active)

    start_edge = bool(
        (left.edge_press and not right.pressed())
        or (right.edge_press and not left.pressed())
    )

    short_release_latched = False
    long_release_stop = False
    hold_to_run = False

    for btn, sign in ((left, -1), (right, 1)):
        if btn.edge_release:
            if btn.last_hold_ms <= tap_ms and direction == sign:
                short_release_latched = True
            elif btn.last_hold_ms > tap_ms and direction == sign:
                long_release_stop = True

    if active:
        for btn in (left, right):
            if btn.pressed() and btn.hold_ms() >= tap_ms:
                hold_to_run = True
                break

    return MoveSemanticState(
        direction=direction,
        active=active,
        boost=boost,
        start_edge=start_edge,
        short_release_latched=short_release_latched,
        long_release_stop=long_release_stop,
        hold_to_run=hold_to_run,
    )


def allow_move_out_of_soft_limit(pos_mm, direction, soft_min=None, soft_max=None):
    if direction == 0:
        return False
    try:
        pos = float(pos_mm)
    except (TypeError, ValueError):
        return True
    if soft_min is not None:
        if pos <= float(soft_min) and direction < 0:
            return True
        if pos <= float(soft_min) and direction > 0:
            return False
    if soft_max is not None:
        if pos >= float(soft_max) and direction > 0:
            return True
        if pos >= float(soft_max) and direction < 0:
            return False
    return True
