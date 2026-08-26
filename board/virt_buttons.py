# virt_buttons — WebSocket pointer events → ButtonState-compatible objects.

import time

from button_state import ButtonState


class VirtualButton:
    """ButtonState lookalike driven by client-timed down/hold/up events."""

    def __init__(self, long_ms=1000, extra_long_ms=None):
        self.long_ms = int(long_ms)
        self.extra_long_ms = None if extra_long_ms is None else int(extra_long_ms)
        self._stable = False
        self._down_ms = None
        self._long_fired = False
        self._extra_long_fired = False
        self.edge_press = False
        self.edge_release = False
        self.short_press = False
        self.long_press = False
        self.extra_long_press = False
        self.last_hold_ms = 0
        self.last_event_ms = time.ticks_ms()

    def pressed(self):
        return self._stable

    def hold_ms(self, now_ms=None):
        if self._stable and self._down_ms is not None:
            if now_ms is None:
                now_ms = time.ticks_ms()
            return time.ticks_diff(now_ms, self._down_ms)
        return 0

    def apply(self, event, client_ms=None):
        self.edge_press = False
        self.edge_release = False
        self.short_press = False
        self.long_press = False
        self.extra_long_press = False
        now = time.ticks_ms()
        self.last_event_ms = now
        ev = str(event or "")
        held = 0 if client_ms is None else int(client_ms)

        if ev == "down":
            self._stable = True
            self._down_ms = now
            self._long_fired = False
            self._extra_long_fired = False
            self.last_hold_ms = 0
            self.edge_press = True
            return

        if ev == "hold":
            if not self._stable:
                self._stable = True
                self._down_ms = time.ticks_add(now, -held) if held else now
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
            return

        if ev == "up":
            self._stable = False
            self.edge_release = True
            self.last_hold_ms = held
            if not self._long_fired:
                self.short_press = True
            self._down_ms = None
            return

    def force_up(self):
        """Watchdog / socket-dead-man: release without a client up event."""
        if not self._stable:
            self.edge_press = False
            self.edge_release = False
            self.short_press = False
            self.long_press = False
            self.extra_long_press = False
            return
        held = self.hold_ms()
        self.apply("up", held if held else self.last_hold_ms)


# Keep a reference so importers can type-check against ButtonState.
_ButtonState = ButtonState
