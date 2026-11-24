"""PS3 controller (Linux joystick API, /dev/input/js0) as a command source. Stdlib only; Python 3.7.

Mapping (measured on this controller, hid-sony driver):
  left stick X = steer | R2 = forward, L2 = reverse (proportional) | right stick Y = throttle if no trigger
  X = halt (until throttle is released) | Start = take control | D-pad up/down = max power +-10%
  L1 / R1 = driving mode down / up (Indoor, Normal, Sport)
  PS held 3 s = safe shutdown
Safety: a trigger counts only after it has been seen fully released once, because before the first real
report the driver can say "half pressed".
"""
import os
import select
import struct
import time

AX_STEER, AX_L2, AX_RY, AX_R2 = 0, 2, 4, 5
BTN_X, BTN_L1, BTN_R1, BTN_START, BTN_PS, BTN_UP, BTN_DOWN = 0, 4, 5, 9, 10, 13, 14
DEAD = 0.08        # stick dead zone
SHUTDOWN_S = 3.0   # PS hold


def _dz(v):
    return 0.0 if abs(v) < DEAD else (abs(v) - DEAD) / (1 - DEAD) * (1 if v > 0 else -1)


class Pad:
    """Pure state machine: feed() joystick events, read command() and actions(). No I/O, so it is unit-tested."""
    kind = "gamepad"

    def __init__(self, clock=time.monotonic):
        self.clock, self.axes, self.armed = clock, {}, set()
        self.halted, self.ps_since, self._actions = False, None, []

    def feed(self, typ, num, value):
        init = typ & 0x80
        if typ & 0x02:                                  # axis
            if num in (AX_L2, AX_R2) and value < -30000:
                self.armed.add(num)
            self.axes[num] = value / 32767.0
        elif typ & 0x01 and not init:                   # button press/release (ignore the initial state dump)
            if num == BTN_PS:
                self.ps_since = self.clock() if value else None
            elif value and num == BTN_X:
                self.halted = True
            elif value and num == BTN_START:
                self._actions.append("take")
            elif value and num in (BTN_UP, BTN_DOWN):
                self._actions.append("power+" if num == BTN_UP else "power-")
            elif value and num in (BTN_L1, BTN_R1):
                self._actions.append("mode+" if num == BTN_R1 else "mode-")

    def _trigger(self, ax):
        return (self.axes.get(ax, -1.0) + 1) / 2 if ax in self.armed else 0.0

    def command(self):
        steer = _dz(self.axes.get(AX_STEER, 0.0))
        thr = self._trigger(AX_R2) - self._trigger(AX_L2)
        if abs(thr) < 0.02:
            thr = -_dz(self.axes.get(AX_RY, 0.0))       # stick up is negative
        if self.halted:
            if abs(thr) < 0.05:
                self.halted = False                     # released: halt is over
            return steer, 0.0
        return steer, thr

    def moved(self):
        s, t = self.command()
        return abs(s) > 0 or abs(t) > 0.05

    def actions(self):
        a, self._actions = self._actions, []
        if self.ps_since is not None and self.clock() - self.ps_since >= SHUTDOWN_S:
            self.ps_since = None
            a.append("shutdown")
        return a


def run(path, on_open, on_tick, on_lost):
    """Thread body: open the controller when it appears, tick at 20 Hz while connected, report loss.
    on_tick(pad) runs every 50 ms whether or not an event arrived (commands must keep flowing for the lease)."""
    while True:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            time.sleep(1)
            continue
        pad = Pad()
        on_open(pad)
        try:
            while True:
                if select.select([fd], [], [], 0.05)[0]:
                    data = os.read(fd, 8 * 32)
                    if not data:
                        raise OSError("controller gone")
                    for i in range(0, len(data) - 7, 8):
                        _, value, typ, num = struct.unpack_from("IhBB", data, i)
                        pad.feed(typ, num, value)
                on_tick(pad)
        except OSError:
            pass
        finally:
            os.close(fd)
            on_lost(pad)
