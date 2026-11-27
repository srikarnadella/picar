"""Motor/servo drivers. Both take set(steer, throttle) with values in -1..1 (+steer = right)."""
import collections
import json
import math
import os
import time

from car.log import log

# Car-wide settings changed from the dashboard. Lives on the car only: deploys never overwrite it.
SETTINGS = os.environ.get("PICAR_SETTINGS") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
DEFAULTS = {"steer_trim": 0.0, "default_power": 1.0,
            "cam_brightness": 50, "cam_exposure": "auto", "cam_flip": "none", "cam_quality": "normal",
            "cam_saturation": 0, "cam_contrast": 0, "cam_sharpness": 0, "cam_awb": "auto", "vision_correct": "on", "obstacle_brake": "on"}


SETTING_RANGES = {"steer_trim": (-0.3, 0.3), "default_power": (0.1, 1.0), "cam_brightness": (20, 80),
                  "cam_saturation": (-100, 100), "cam_contrast": (-100, 100), "cam_sharpness": (-100, 100)}
SETTING_CHOICES = {"cam_exposure": ("auto", "night"), "cam_flip": ("none", "upside", "mirror"), "cam_quality": ("low", "normal", "high"),
                   "cam_awb": ("auto", "sunlight", "cloudy", "tungsten"), "vision_correct": ("on", "off"), "obstacle_brake": ("on", "off")}


def clean(key, value):
    """A setting checked against its allowed values: (True, value clamped and typed) or (False, None)."""
    if key in SETTING_CHOICES:
        return (True, value) if isinstance(value, str) and value in SETTING_CHOICES[key] else (False, None)
    if key in SETTING_RANGES and not isinstance(value, bool):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return False, None
        if not math.isfinite(value):
            return False, None
        lo, hi = SETTING_RANGES[key]
        value = max(lo, min(hi, value))
        return True, int(round(value)) if key.startswith("cam_") else round(value, 3)   # camera values are whole numbers
    return False, None


def load_settings():
    """settings.json, every value checked: anything hand-edited into nonsense falls back to its default. stop.py
    (neutral after the server stops) reads this too, so a bad file must never crash it."""
    try:
        with open(SETTINGS) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    out = {}
    for k, default in DEFAULTS.items():
        ok, v = clean(k, saved.get(k))
        out[k] = v if ok else default
    return out


def save_settings(s):
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, SETTINGS)  # atomic: a power cut never leaves half a file


def pulses(c, steer, throttle):
    """-1..1 commands to 12-bit PCA9685 ticks using the calibration dict.
    throttle_min: the ESC dead zone. Any non-zero throttle starts there, so the whole stick moves the car.
    steer_trim: added to every steering command (centre too), for a car that pulls to one side."""
    steer = max(-1.0, min(1.0, steer + c.get("steer_trim", 0.0)))
    mid = (c["steer_left"] + c["steer_right"]) / 2.0
    s = mid + steer * (c["steer_right"] - c["steer_left"]) / 2.0
    end = c["throttle_fwd"] if throttle > 0 else c["throttle_rev"]
    m = c.get("throttle_min", 0.0)
    mag = m + (1 - m) * abs(throttle) if throttle else 0.0
    t = c["throttle_stop"] + mag * (end - c["throttle_stop"])
    return int(round(s)), int(round(t))


class Mock:
    def __init__(self, quiet=False):
        self.quiet = quiet
        self.last = (0.0, 0.0)
        self.history = collections.deque(maxlen=1000)

    def set(self, steer, throttle):
        self.last = (steer, throttle)
        self.history.append(self.last)
        if not self.quiet:
            print("mock drive steer=%+.2f throttle=%+.2f" % self.last)


class Pca9685:
    """Same pulse math as donkeycar's PWMSteering/PWMThrottle (12-bit ticks), minus the ESC
    full-throttle arming blip: we just hold neutral, which arms a normal ESC."""
    MODE1, SLEEP = 0x00, 0x10

    def __init__(self, cal):
        self.cal = cal
        self._init()
        self.set(0.0, 0.0)

    def _init(self):
        import Adafruit_PCA9685  # on the Pi (in ~/env). Its constructor wakes the chip; set_pwm_freq alone does not
        self.pwm = Adafruit_PCA9685.PCA9685(address=self.cal["address"])
        self.pwm.set_pwm_freq(self.cal["pwm_freq"])
        self._checked = time.monotonic()

    def set(self, steer, throttle):
        if time.monotonic() - self._checked > 1.0:  # a brownout resets the chip to sleep, outputs off: catch it
            self._checked = time.monotonic()
            if self.pwm._device.readU8(self.MODE1) & self.SLEEP:
                log("PCA9685 had reset (power dip?); re-initialised")
                self._init()
        s, t = pulses(self.cal, steer, throttle)
        self.pwm.set_pwm(self.cal["steer_channel"], 0, s)
        self.pwm.set_pwm(self.cal["throttle_channel"], 0, t)
