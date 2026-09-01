# SliderPins.example.py — copy to SliderPins.py (same folder / device root).
#
# Overlay dicts merge into MC_config / SW_config (missing keys keep defaults).
# Defaults match JKSlider on a Pico / Pico W / Pico 2 W.

MC_config = {
    "UART_ID": 0,
    "PIN_UART_TX": 16,  # UART0 TX → SliderMC RX
    "PIN_UART_RX": 17,  # UART0 RX ← SliderMC TX
    "UART_BAUD": 115_200,
}

SW_config = {
    "PIN_LED_R": 2,
    "PIN_LED_G": 3,
    "PIN_LED_B": 4,
    "PIN_NEOPIXEL": None,  # WS2812 off
    "PIN_CAMERA_CTRL": 15,  # timelapse shutter
    "CAMERA_PULSE_MS": 100,
    "LED_ACTIVE_HIGH": True,
    "AP_PASSWORD": "sliderweb",
    "HOSTNAME": "slider",
    "STA_GRACE_MS": 10000,
    "DEBUG_LEVEL": 3,
    "SW_MC_SIM": True,
    "SW_MC_POWER_DELAY_MS": 300,
    "SW_MC_BANNER_S": 5.0,
}
