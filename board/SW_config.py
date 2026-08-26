# SW_config — SliderWeb panel / Wi-Fi / RGB / web defaults (Pico W / Pico 2 W).
#
# Users: copy SliderPins.example.py → SliderPins.py (same folder / device root).
# GPIO numbers match JKSlider on a Pico (SliderCtrl UIC_config / MC_config).

# ---------------------------------------------------------------------------
# GPIO — RGB status LED (JKSlider: GP2 / GP3 / GP4). WS2812 off for now.
# ---------------------------------------------------------------------------
PIN_LED_R = 2
PIN_LED_G = 3
PIN_LED_B = 4
PIN_NEOPIXEL = None
LED_ACTIVE_HIGH = True  # common-cathode
LED_PWM_HZ = 1000

# ---------------------------------------------------------------------------
# Motion feel (JKSlider-compatible)
# ---------------------------------------------------------------------------
SW_SPEED_MIN_MM_S = 1.0
SW_SPEED_MAX_MM_S = 100.0
SW_SPEED_CURVE_GAMMA = 2.0
SW_MOVE_TAP_MS = 333
SW_LONG_PRESS_MS = 1000
SW_STOP_HALT_MS = 1000
SW_STOP_DISABLE_MS = 2000
SW_FLASH_MS = 1500
SW_STATUS_HZ = 12
SW_SS_HYST_MM_S = 0.05
SW_LEFT_IS_NEGATIVE = True

# ---------------------------------------------------------------------------
# Thin MC bridge (JS owns panel logic; Pico relays UART)
# ---------------------------------------------------------------------------
# Client must send {"wdt":"alive"} ~1/s. Missing for this long → MS (stop).
SW_WDT_TIMEOUT_MS = 2500
# Echo every forwarded MC line to USB CDC (REPL), e.g. "MC> SS 40"
SW_MC_USB_ECHO = True
# Max length of {"mc":"..."} payload (ASCII command line)
SW_MC_LINE_MAX = 80

# ---------------------------------------------------------------------------
# SliderMC link / simulation
# ---------------------------------------------------------------------------
SW_MC_SIM = True
SW_MC_BANNER_S = 3.0
SW_MC_SIM_HZ = 10

# ---------------------------------------------------------------------------
# Wi-Fi (Pico W / 2W SoftAP DHCP is 192.168.4.x — stay on that subnet)
# ---------------------------------------------------------------------------
WIFI_JSON = "wifi.json"
AP_SSID_PREFIX = "SWeb"
AP_OPEN = False
AP_PASSWORD = "sliderweb"
AP_CHANNEL = 6
WIFI_COUNTRY = "DE"
HOSTNAME = "slider"
STA_CONNECT_S = 12.0
STA_GRACE_MS = 10000
WIFI_FILE = WIFI_JSON
AP_IP = "192.168.4.1"

# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------
HTTP_PORT = 80
WWW_DIR = "www"
FILE_MAX_BYTES = 49152
WWW_EDIT_PREFIX = "www"

# ---------------------------------------------------------------------------
# Debug
# ---------------------------------------------------------------------------
DEBUG_LEVEL = 3
SW_BOOT_DELAY_S = 2.0

try:
    import SliderPins as _board_pins

    _ov = getattr(_board_pins, "SW_config", None)
    if isinstance(_ov, dict):
        for _k, _v in _ov.items():
            globals()[_k] = _v
except ImportError:
    pass
