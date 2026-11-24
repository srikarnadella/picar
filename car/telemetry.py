"""Real Pi vitals from /proc, /sys and vcgencmd. Unavailable values are None (the dashboard prints --).
With sim=True (Mac mock runs) it returns slowly varying FAKE values, and the dashboard marks the screen SIM."""
import math
import os
import subprocess
import time

_pwr = [0.0, None]  # [last read time, value]: vcgencmd is a subprocess, so read it every 5 s only


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _throttled():
    if time.monotonic() - _pwr[0] > 5:
        _pwr[0] = time.monotonic()
        try:
            out = subprocess.check_output(["vcgencmd", "get_throttled"], timeout=1).decode()
            _pwr[1] = int(out.split("=")[1], 16)
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            _pwr[1] = None
    return _pwr[1]


def system(sim=False):
    if sim:
        t = time.time()
        return {"temp": 48 + 3 * math.sin(t / 20), "load": 22 + 8 * math.sin(t / 7), "wifi": -52 + 4 * math.sin(t / 11), "pwr": 0,
                "batt": 8.2 - 1.4 * (t / 900 % 1)}  # drains over 15 minutes, then "recharges"
    temp = _read("/sys/class/thermal/thermal_zone0/temp")
    wifi = _read("/proc/net/wireless")
    out = {"temp": int(temp) / 1000.0 if temp else None, "load": None, "wifi": None, "pwr": _throttled()}
    try:
        out["load"] = min(100.0, os.getloadavg()[0] / (os.cpu_count() or 1) * 100)
    except OSError:
        pass
    if wifi:
        for line in wifi.splitlines()[2:]:
            out["wifi"] = float(line.split()[3])  # signal level, dBm
    return out


class Battery:
    """Motor battery voltage: ADS1115 (I2C, same bus as the PCA9685) reading AIN0 through a resistor divider.
    Wiring: battery + -> 10k -> AIN0 -> 4.7k -> GND; ADS1115 VDD 3.3 V, GND shared with the Pi and the ESC.
    Non-blocking: each read() returns the conversion started by the previous call (2 Hz), then starts the next.
    No board fitted: returns None and looks again every 10 s."""
    CONFIG = [0xC3, 0x83]  # start single-shot, AIN0 vs GND, +-4.096 V range, 128 samples/s, comparator off
    LSB = 4.096 / 32768

    def __init__(self, cfg, dev=None):
        self.cfg, self.dev, self.started, self.retry_at, self.v = cfg, dev, False, 0.0, None

    def read(self):
        now = time.monotonic()
        try:
            if self.dev is None:
                if now < self.retry_at:
                    return None
                from Adafruit_GPIO import I2C  # comes with Adafruit_PCA9685 on the Pi
                self.dev, self.started = I2C.get_i2c_device(self.cfg["address"]), False
            if self.started:
                v = max(0, self.dev.readS16BE(0x00)) * self.LSB * self.cfg["divider"]
                self.v = v if self.v is None else self.v * 0.7 + v * 0.3  # smooth motor-load sag
            self.dev.writeList(0x01, self.CONFIG)
            self.started = True
            return self.v
        except (OSError, ImportError):
            self.dev, self.v, self.retry_at = None, None, now + 10
            return None


def battery_percent(v, cfg):
    """Rough state of charge from voltage, linear between the configured empty and full volts."""
    if v is None:
        return None
    return max(0.0, min(100.0, (v - cfg["empty"]) / (cfg["full"] - cfg["empty"]) * 100))


def _run(cmd):
    try:
        return subprocess.check_output(cmd, timeout=1, stderr=subprocess.DEVNULL).decode()
    except (OSError, subprocess.SubprocessError):
        return None


def health():
    """Slower, on-request facts for Settings > Health and logs. Missing pieces are None."""
    out = system()
    up = _read("/proc/uptime")
    out["pi_up_s"] = float(up.split()[0]) if up else None
    mem = _read("/proc/meminfo")
    if mem:
        m = dict(l.split(":") for l in mem.splitlines() if ":" in l)
        out["mem_pct"] = round(100.0 * (1 - int(m["MemAvailable"].split()[0]) / int(m["MemTotal"].split()[0])), 1)
    st = os.statvfs("/")
    out["disk_free_gb"], out["disk_gb"] = round(st.f_bavail * st.f_frsize / 1e9, 1), round(st.f_blocks * st.f_frsize / 1e9, 1)
    ips = _run(["hostname", "-I"])
    out["ip"] = ips.split()[0] if ips and ips.split() else None
    wpa = _run(["sudo", "-n", "wpa_cli", "-i", "wlan0", "status"]) or ""   # wpa_cli needs root on the Pi
    out["ssid"] = next((l[5:] for l in wpa.splitlines() if l.startswith("ssid=")), None)
    return out
