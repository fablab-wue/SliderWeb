# SliderWeb — Pico W / Pico 2 W WLAN UIC for SliderMC.
#
# Copy this tree onto the board with Thonny. main.py runs on boot.

try:
    import uasyncio as asyncio
except ImportError:
    import asyncio

if not hasattr(asyncio, "sleep_ms"):
    async def _sleep_ms(ms):
        await asyncio.sleep(ms / 1000.0)

    asyncio.sleep_ms = _sleep_ms

import gc

from dbg import dbg
from led_status import LedStatus
from wifi_portal import WifiPortal
import SW_config as cfg


async def main():
    dbg(3, "SliderWeb boot Pico")
    gc.collect()
    led = LedStatus()
    wifi = WifiPortal(led)
    await wifi.start()

    wifi_task = asyncio.create_task(wifi.run())
    led_task = asyncio.create_task(led.run())

    from panel_app import PanelApp
    from web_app import WebApp

    panel = PanelApp(None, led, sim=False)
    web = WebApp(panel, wifi)
    web_task = asyncio.create_task(web.run())
    panel_task = asyncio.create_task(panel.run())

    async def mc_start():
        """Link SliderMC at power-on (same supply as Pico) — do not wait for phone/DNS."""
        delay_ms = int(getattr(cfg, "SW_MC_POWER_DELAY_MS", 300))
        if delay_ms > 0:
            dbg(3, "UART power settle", delay_ms, "ms")
            await asyncio.sleep_ms(delay_ms)
        banner_s = float(getattr(cfg, "SW_MC_BANNER_S", 5.0))
        sim_ok = bool(getattr(cfg, "SW_MC_SIM", True))
        linked = False
        try:
            from MC_client import MC_Client

            mc = MC_Client()
            linked = bool(await mc.start(banner_timeout_s=banner_s))
            if linked:
                dbg(3, "MC axes", mc.axis_count, "max_speed", mc.max_speed)
                panel.sim = False
                panel.bind_mc(mc)
            else:
                dbg(2, "MC banner timeout", banner_s, "s")
        except Exception as exc:
            dbg(1, "MC start fail", exc)
            linked = False
        if (not linked) and sim_ok:
            panel.sim = True
            dbg(3, "MC sim on (dummy verbose, all zeros)")

    await asyncio.gather(
        wifi_task,
        led_task,
        panel_task,
        web_task,
        mc_start(),
    )


def run():
    asyncio.run(main())


if __name__ == "__main__":
    import sys

    if not getattr(sys, "sliderweb_hold_repl", False):
        run()
