# MC_config — UART link to SliderMC (Pico UART0, same as JKSlider).

UART_ID = 0
PIN_UART_TX = 16
PIN_UART_RX = 17
UART_BAUD = 115_200
MIN_SPEED_MM_S = 0.006
SOFT_LIMIT_WARN_MM = 10.0
LED_ACCEL_SPEED_EPS_MM_S = 3.0

try:
    import SliderPins as _board_pins

    _ov = getattr(_board_pins, "MC_config", None)
    if isinstance(_ov, dict):
        for _k, _v in _ov.items():
            globals()[_k] = _v
except ImportError:
    pass
