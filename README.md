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
| CAMERA_CTRL | GP15 | Timelapse shutter pulse (`CAMERA_PULSE_MS`) |
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

On each WebSocket connect the board first sends a **`hello`** snapshot (also `GET /api/hello`), then periodic status.

**Hello** (reconnect / cache fill):

```json
{
  "t": "hello",
  "linked": true,
  "config": { "slider_min": 0, "slider_max": 600, "max_speed": 100 },
  "soft": { "min": 12.0, "max": 480.0, "min2": null, "max2": null },
  "session": { "enabled": true, "ss": 40.0, "sa": 100.0 },
  "task": null
}
```

- `config` — full `CG` map; **physical** `slider_min`/`slider_max` never change for the slider
- `soft` — live soft window from `GL`/`GR` (set via `SL`/`SR`); re-read on every hello
- `session` — commanded ENABLE / SPEED / ACCEL
- `task` — active Pico task or `null`

**Status** (excerpt):

```json
{
  "state": "M",
  "axes": 1,
  "pos": 12.3, "spd": 0.5, "acc": 20.0, "tgt": 100.0,
  "ss": 40.0, "spd_min": 1.0, "max_speed": 100.0,
  "slider_min": 0, "slider_max": 600,
  "soft_min": 12.0, "soft_max": 480.0,
  "session": { "enabled": true, "ss": 40.0, "sa": 100.0 },
  "task": { "name": "TSK_TL_MSM", "state": "move", "detail": "frame 3/120", "frame": 3, "frames": 120 },
  "linked": true,
  "line1": "Task TL",
  "line2": "running",
  "warn": false
}
```

When `axes` is 2, `pos2` / `spd2` / `acc2` / `tgt2` / `slider_min_2` / `slider_max_2` / `soft_min_2` / `soft_max_2` are included.

**Client → board** (`/ws` or `POST /api/cmd`):

```json
{"wdt":"alive"}
{"mc":"SS 40"}
{"mc":"ML"}
{"task":"TSK_PPM 0 _ 600 _ 1.0"}
{"task":"TSK_TL_CONT 100 _ 0.004000 0.010000 0.333333 0.100000"}
{"task":"TSK_TL_MSM 100 _ 120 0.333333 0.100000"}
```

- `{"mc":…}` — raw UART line; `SS`/`SA`/`SE` mirror into Pico session. Any command token starting with **`M`** (`MS`/`ML`/`MR`/`MT`/`MH`) cancels an active task (camera line forced **low**), then is forwarded.
- `{"task":"TSK_…"}` — Pico-owned task (not sent to UART). One task at a time; a new start **replaces** the previous.
- `{"wdt":"alive"}` — ~1/s. Missing ~2.5 s → board sends `MS` **unless a task is active** (phone sleep must not kill a shoot).

**Tasks**

| Task | Args | Notes |
|------|------|-------|
| `TSK_PPM` | `pos1 pos1_2 pos2 pos2_2 delay_s` | Ping-pong; `_` = unused axis |
| `TSK_TL_CONT` | `pos pos2 speed accel trigger_time_s trigger_length_s` | Crawl + periodic shutter; speed/accel from phone (6 decimals OK) |
| `TSK_TL_MSM` | `pos pos2 frames trigger_time_s trigger_length_s` | Hop shoot; hop SS/SA from `SW_SPEED_TL_MM_S` / `SW_ACCEL_TL_MM_S2` |

Silent clamps: `trigger_time_s ≥ 0.2`, `trigger_length_s ≥ 0.01`. Task cancel (STOP / any `M*` / limit error) drives **CAMERA_CTRL low**.

Phone TL tab: `trigger_time = FACTOR/FPS`, exposure from UI, MSM `frames = ceil((delta/(SS/FACTOR))*FPS)`.

Also: `GET /api/config` (CG map), `GET|POST /api/wifi`, `GET|PUT /api/files`.

**Camera:** `PIN_CAMERA_CTRL` (default GP15). Timelapse tasks pulse active-high for `trigger_length_s`; abort always clears the line.

**Config (hello `config`):** `speed_tl_mm_s`, `accel_tl_mm_s2` — MSM hop motion defaults (`SW_SPEED_TL_MM_S`, `SW_ACCEL_TL_MM_S2`).

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
  panel_app.py         thin bridge + hello + WDT + tasks
  task_runner.py       TSK_PPM / TSK_TL_CONT / TSK_TL_MSM
  camera_ctrl.py       CAMERA_CTRL pulse (non-blocking + off)
  web_app.py           Microdot + WebSocket
  wifi_portal.py       STA / AP / captive DNS
  led_status.py        RGB PWM GP2/3/4
  www/                 HTML/CSS/JS
  microdot/            vendored Microdot
tools/preview.py       PC preview of board/www
```

## License

MIT — Jochen Krapf. `MC_client.py` / `button_state.py` follow SliderCtrl. Microdot is MIT (Miguel Grinberg).
