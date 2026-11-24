"""The only code that moves the car. Browser input is a request; this decides.

Failsafe: a command is valid for `lease_s`; after that, or on release(), output is neutral.
Outputs are re-written every REFRESH_S even when unchanged (a chip that reset gets its pulses
back), and the systemd watchdog is fed only while the driver works, so a dead I2C bus gets the
service restarted instead of looking healthy. Python 3.7 compatible (runs on the Pi).
"""
import math
import threading
import time

from car import perf
from car.log import log

REFRESH_S = 1.0   # re-write unchanged outputs this often
FAIL_S = 1.0      # driver failing for this long: stop feeding the watchdog


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def expo(x, e):
    """RC-style sensitivity curve: e=0 linear; higher e = softer near centre. Full input still gives full output."""
    return (1 - e) * x + e * x * x * x


class Governor:
    def __init__(self, drv, limits, lease_s=0.25, clock=time.monotonic, heartbeat=None, reverse_tap=None):
        self.drv, self.limits, self.lease_s = drv, limits, lease_s
        self.clock, self.heartbeat = clock, heartbeat
        self.reverse_tap = reverse_tap   # {"power", "tap_s", "settle_s"} or None (see _esc)
        self.power = 1.0                 # max-power slider: full stick = this fraction of the limits
        self.expo = 0.0                  # steering sensitivity from the driving mode (applies to every input)
        self._lock = threading.Lock()
        self._cmd = (0.0, 0.0)           # (steer, throttle), already clamped
        self._stamp = None               # when the last valid command arrived
        self._seq = -1
        self._written, self._wrote_at, self._fail_since = None, None, None
        self._rev_ok, self._tap = False, None
        self.outages = 0                 # motor-driver outages since start (for Health)
        self._run = False
        self.tick()                      # neutral first, before anything else

    def command(self, steer, throttle, seq=0):
        """Returns True if accepted. Rejects NaN/garbage and stale (out-of-order) seq."""
        if not isinstance(seq, int) or isinstance(seq, bool):
            return False
        try:
            steer, throttle = float(steer), float(throttle)
        except (TypeError, ValueError):
            return False
        if not (math.isfinite(steer) and math.isfinite(throttle)):
            return False
        lim = self.limits
        steer = _clamp(expo(_clamp(steer, -1.0, 1.0), self.expo), -lim["max_steer"], lim["max_steer"])
        throttle = _clamp(throttle * self.power, -lim["max_reverse"], lim["max_forward"])
        with self._lock:
            if seq <= self._seq:
                return False
            self._seq, self._cmd, self._stamp = seq, (steer, throttle), self.clock()
        perf.tick("commands")
        return True

    def set_power(self, p):
        """Max-power slider. Ignores garbage; never above 1 (the config limits stay the hard cap)."""
        try:
            p = float(p)
        except (TypeError, ValueError):
            return
        if math.isfinite(p):
            self.power = _clamp(p, 0.1, 1.0)

    @property
    def output(self):
        """What the car is actually doing (after limits, sensitivity and the reverse tap)."""
        return self._written or (0.0, 0.0)

    def rewrite(self):
        """Write the outputs again on the next tick (after a calibration change such as steering trim)."""
        with self._lock:
            self._written = None

    def release(self):
        """New/closed controller: neutral now and forget the sequence."""
        with self._lock:
            self._cmd, self._stamp, self._seq = (0.0, 0.0), None, -1
        self.tick()

    def tick(self):
        with self._lock:
            now = self.clock()
            live = self._stamp is not None and now - self._stamp <= self.lease_s
            out = self._cmd if live else (0.0, 0.0)
            if self.reverse_tap:
                out = self._esc(out, now)
            if out != self._written or now - self._wrote_at >= REFRESH_S:
                try:
                    self.drv.set(*out)
                    if self._fail_since is not None:
                        log("motor driver recovered")
                    self._written, self._wrote_at, self._fail_since = out, now, None
                except Exception as e:  # I2C hiccup: retry next tick; report once per outage
                    if self._fail_since is None:
                        self._fail_since, self.outages = now, self.outages + 1
                        log("motor driver error (retrying):", e)
            healthy = self._fail_since is None or now - self._fail_since < FAIL_S
        if self.heartbeat and healthy:
            self.heartbeat()

    def _esc(self, out, now):
        """Most RC ESCs treat the first reverse after forward (or after power-up) as a brake. So before a real
        reverse: a short reverse tap, then neutral. Forward re-arms the need. Called with the lock held.
        # ponytail: no timeout on _rev_ok; if your ESC forgets after a long neutral, clear it after N s."""
        steer, thr = out
        t = self.reverse_tap
        if thr > 0:
            self._rev_ok, self._tap = False, None
        elif thr < 0 and not self._rev_ok:
            self._tap = now if self._tap is None else self._tap
            dt = now - self._tap
            if dt < t["tap_s"]:
                return steer, -t["power"]
            if dt < t["tap_s"] + t["settle_s"]:
                return steer, 0.0
            self._rev_ok, self._tap = True, None
        elif thr == 0:
            self._tap = None   # abandon an unfinished tap
        return steer, thr

    def start(self):
        self._run = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        last = time.monotonic()
        while self._run:
            self.tick()
            now = time.monotonic()
            perf.timing("motor.loop_gap", (now - last) * 1000)   # should be ~40 ms; spikes mean the Pi is overloaded
            last = now
            time.sleep(0.04)  # 25 Hz is plenty against a 250 ms lease

    def close(self):
        self._run = False
        self.release()
