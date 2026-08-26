# SliderWeb

**WLAN UI controller for DIY motorized camera sliders** — MicroPython + asyncio on a **Raspberry Pi Pico W** or **Pico 2 W**. It is a sibling of **[SliderCtrl](https://github.com/fablab-wue/SliderCtrl)** (physical JKSlider panel): same UART to **[SliderMC](https://github.com/fablab-wue/SliderMC)**, same GPIO map, web instead of buttons/OLED. A phone, tablet, or laptop opens a dark page on the local AP or your Wi-Fi.

Docs: **[SliderDoc](https://github.com/fablab-wue/SliderDoc)**.

## What it is

```text
Phone / tablet  --WLAN / captive AP-->  Pico W / Pico 2 W (this firmware)
                                          UART0 1 Mbaud  GP16 TX / GP17 RX
                                        SliderMC (RP2040)
```

Makers own `board/www/` (HTML / CSS / JS). Firmware serves those files plus JSON/WebSocket. Edit in **Thonny**, on the Config tab file editor, or with `tools/preview.py`.

## Features (v1)

- Captive **AP** if no Wi-Fi is saved (`SWeb-xxxx` / password `sliderweb`); saved SSID tries **STA**, then AP fallback
- Short radio hiccups stay silent (no AP/IP flap; WebSocket retries)
- Live **pos / spd / acc / state letter** plus two OLED-style status lines
- **MOVE_L · STOP · MOVE_R · FAST** with JKSlider tap / hold timings
- **SPEED slider** → SliderMC `SS` (commanded); status Spd is actual
- 1- and **2-axis** callbacks and `CG` config
- RGB status LED on **GP2 / GP3 / GP4** (JKSlider colours + last-octet IP Morse on STA)
- USB CDC debug prints
- **MC sim** (default on): if SliderMC does not answer the UART banner within 3 s, dummy verbose status keeps the UI live

Later: accel slider, A/B/C, timelapse, user manual.

## Hardware

**Board:** Pico W or Pico 2 W (official MicroPython `RPI_PICO_W` / `RPI_PICO2_W` UF2).

GPIO numbers match **JKSlider** on a Pico (`SliderCtrl` `UIC_config` / `MC_config`):

| Function | GPIO | Notes |
|----------|------|-------|
| UART TX → MC RX | GP16 | UART0, 3.3 V, 1 Mbaud, crossed like SliderCtrl |
| UART RX ← MC TX | GP17 | |
| LED R | GP2 | PWM, common-cathode, `LED_ACTIVE_HIGH = True` |
| LED G | GP3 | ≈ 5 mA per channel from 3.3 V |
| LED B | GP4 | |
| WS2812 | — | Off (`PIN_NEOPIXEL = None`) |
| 5 V / GND | VSYS / GND | Same 4-wire as SliderCtrl. Prefer **one** 5 V source (MC cable *or* USB). |

Copy `board/SliderPins.example.py` → `SliderPins.py` on the device (next to `main.py`) and edit that file only.

RGB wiring: see SliderDoc [JKSlider RGB LED](https://github.com/fablab-wue/SliderDoc/blob/main/uic/projects/jkslider/technical/panel.md#wiring-schematics--rgb-led).

Flash [MicroPython for Pico W](https://micropython.org/download/RPI_PICO_W/) or [Pico 2 W](https://micropython.org/download/RPI_PICO2_W/). Copy the **contents** of `board/` onto the device **root** (`/`) — not the `board` folder itself.

**Thonny:** after reset the board waits **2 s** (`SW_BOOT_DELAY_S`) so Stop/Restart can grab the REPL before Wi-Fi starts.

**mpremote** (from the repo root):

```text
mpremote fs cp -r board/: /
```

## Wi-Fi

1. No `wifi.json` → AP `SWeb-` + last 4 unique-id hex, DHCP `192.168.4.x`, captive DNS → this page. Open `http://192.168.4.1/` if the captive sheet does not appear.
2. Config tab: SSID + password → STA. Hostname default `slider`.
3. STA fail / long drop → AP fallback.
4. SoftAP DHCP stays on **`192.168.4.1`** (Pico default pool).

## JSON contract (`/ws` and `GET /api/status`)

```json
{
  "state": "M",
  "axes": 1,
  "pos": 12.3, "spd": 0.5, "acc": 20.0, "tgt": 100.0,
  "ss": 40.0, "spd_min": 1.0, "max_speed": 100.0,
  "slider_min": 0, "slider_max": 600,
  "line1": "Cruising R",
  "line2": "Near limit",
  "warn": false
}
```

When `axes` is 2, `pos2` / `spd2` / `acc2` / `tgt2` / `slider_min_2` / `slider_max_2` are included.

**Client → board**

```json
{"t":"btn","n":"MOVE_L","e":"down","ax":1}
{"t":"btn","n":"MOVE_L","e":"hold","ms":333}
{"t":"btn","n":"MOVE_L","e":"up","ms":410}
{"t":"ss","v":40.0}
{"t":"ax","v":1}
{"t":"ping"}
```

`ax`: `1` axis 1, `2` axis 2, `0` both. Buttons: `MOVE_L` `MOVE_R` `FAST_L` `FAST_R` `STOP`.

Also: `GET /api/config` (full `CG`), `GET|POST /api/wifi`, `GET|PUT /api/files`, `POST /api/cmd` (same JSON as `/ws` when WebSocket is down).

## Preview without a board

```text
python tools/preview.py
```

Open http://127.0.0.1:8080/

## Layout

```text
board/                 copy this tree onto the Pico root (/)
  boot.py  main.py
  MC_client.py         UART client (from SliderCtrl)
  button_state.py      tap / hold semantics
  panel_app.py         MOVE / STOP / SS / status lines
  web_app.py           Microdot + WebSocket
  wifi_portal.py       STA / AP / captive DNS
  led_status.py        RGB PWM GP2/3/4
  www/                 HTML/CSS/JS
  microdot/            vendored Microdot
tools/preview.py       PC preview of board/www
```

## License

MIT — Jochen Krapf. `MC_client.py` / `button_state.py` follow SliderCtrl. Microdot is MIT (Miguel Grinberg).
