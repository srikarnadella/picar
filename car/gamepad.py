"""Game controllers (Linux joystick API, /dev/input/js0) as a command source. Stdlib only; Python 3.7.

Same controls on every supported controller (layouts below):
  left stick X = steer | right trigger = forward, left trigger = reverse (proportional) | right stick Y = throttle
  if no trigger | halt button (until throttle is released) | Start/Menu = take control | D-pad up/down = max power
  +-10% | left/right bumper = driving mode down / up (Indoor, Normal, Sport) | PS button held 3 s = safe shutdown (PS3)

Safety (a controller read with the wrong layout once drove the car into a wall at full power):
  - Only controllers with a known layout are used: matched by the name the driver reports AND by having the axes
    and buttons the layout needs. Anything else is ignored and logged, never driven.
  - A controller drives nothing until it has reported every driving axis and they all read neutral (sticks
    centred, triggers released). One that says "full throttle" when it connects never gets control.
  - A trigger counts only after it has been seen fully released once (before the first real report the driver
    can say "half pressed").
"""
import array
import fcntl
import os
import select
import struct
import subprocess
import time

DEAD = 0.08        # stick dead zone
SHUTDOWN_S = 3.0   # home button hold

# Joystick indices as the kernel numbers them (axes and buttons in the order of their event codes).
PS3 = {"name": "PS3 controller", "match": "PLAYSTATION(R)3",   # hid-sony; measured on this car's DualShock 3
       "steer": 0, "fwd": 5, "rev": 2, "stick": 4,
       "halt": (0,), "take": 9, "mode_down": 4, "mode_up": 5, "power_btns": (13, 14), "shutdown": 10}
XBOX = {"name": "Xbox controller", "match": "Xbox Wireless Controller",   # Bluetooth, hid-generic: axes X Y Z Rz Gas Brake Hat0X Hat0Y
        "steer": 0, "fwd": 4, "rev": 5, "stick": 3,                        # Gas = right trigger, Brake = left trigger, Rz = right stick Y
        "halt": (0, 1), "take": 7, "mode_down": 4, "mode_up": 5, "power_hat": 7,   # A or B halt; Menu; LB / RB; D-pad
        "shutdown": None}   # the Xbox button is not a joystick button on Linux
# Xbox One S (045E:02FD, firmware 0903), measured by pressing each control on 2026-11-26. Newer controller firmware
# numbers the buttons differently (axes, A and B are the same): re-measure after a firmware update.
LAYOUTS = (PS3, XBOX)


def _needs(layout):
    """(axes, buttons) a controller must have for this layout."""
    axes = [layout[k] for k in ("steer", "fwd", "rev", "stick", "power_hat") if layout.get(k) is not None]
    btns = list(layout["halt"]) + list(layout.get("power_btns", ())) + [layout[k] for k in ("take", "mode_down", "mode_up", "shutdown") if layout[k] is not None]
    return max(axes) + 1, max(btns) + 1


def layout_for(name, n_axes, n_buttons):
    """The layout for a controller, or None: the name must match and it must have every axis and button used."""
    for layout in LAYOUTS:
        if layout["match"] in name:
            need_axes, need_btns = _needs(layout)
            return layout if n_axes >= need_axes and n_buttons >= need_btns else None
    return None


def _dz(v):
    return 0.0 if abs(v) < DEAD else (abs(v) - DEAD) / (1 - DEAD) * (1 if v > 0 else -1)


class Pad:
    """Pure state machine: feed() joystick events, read command() and actions(). No I/O, so it is unit-tested."""
    kind = "gamepad"

    def __init__(self, layout=PS3, clock=time.monotonic):
        self.layout, self.name, self.clock = layout, layout["name"], clock
        self.axes, self.armed, self.ready = {}, set(), False
        self.halted, self.home_since, self._actions, self._hat = False, None, [], 0
        self._drive_axes = [layout[k] for k in ("steer", "fwd", "rev", "stick") if layout.get(k) is not None]

    def feed(self, typ, num, value):
        L, init = self.layout, typ & 0x80
        if typ & 0x02:                                  # axis
            if num in (L["fwd"], L["rev"]) and value < -30000:
                self.armed.add(num)
            self.axes[num] = value / 32767.0
            if num == L.get("power_hat"):               # D-pad as a hat axis (Xbox): one action per push
                d = -1 if value < -16000 else 1 if value > 16000 else 0
                if d and d != self._hat and not init:
                    self._actions.append("power+" if d < 0 else "power-")   # up is negative
                self._hat = d
        elif typ & 0x01 and not init:                   # button press/release (ignore the initial state dump)
            if num == L["shutdown"]:
                self.home_since = self.clock() if value else None
            elif value and num in L["halt"]:
                self.halted = True
            elif value and num == L["take"]:
                self._actions.append("take")
            elif value and num in L.get("power_btns", ()):
                self._actions.append("power+" if num == L["power_btns"][0] else "power-")
            elif value and num in (L["mode_down"], L["mode_up"]):
                self._actions.append("mode+" if num == L["mode_up"] else "mode-")

    def _trigger(self, ax):
        return (self.axes.get(ax, -1.0) + 1) / 2 if ax in self.armed else 0.0

    def _raw(self):
        L = self.layout
        steer = _dz(self.axes.get(L["steer"], 0.0))
        thr = self._trigger(L["fwd"]) - self._trigger(L["rev"])
        if abs(thr) < 0.02 and L.get("stick") is not None:
            thr = -_dz(self.axes.get(L["stick"], 0.0))  # stick up is negative
        return steer, thr

    def command(self):
        steer, thr = self._raw()
        if not self.ready:                              # nothing until every driving axis has reported neutral
            if all(a in self.axes for a in self._drive_axes) and steer == 0 and abs(thr) < 0.05:
                self.ready = True
            else:
                return 0.0, 0.0
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
        if self.home_since is not None and self.clock() - self.home_since >= SHUTDOWN_S:
            self.home_since = None
            a.append("shutdown")
        return a


def _bt(*args):
    """bluetoothctl, or "" where there is none (Mac mock runs) or it hangs."""
    try:
        return subprocess.run(["bluetoothctl"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              universal_newlines=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def paired_controllers():
    """Bluetooth addresses of the paired game controllers (PS3, Xbox, any other)."""
    listing = _bt("paired-devices") or _bt("devices", "Paired")
    macs = [line.split()[1] for line in listing.splitlines() if line.startswith("Device ") and len(line.split()) >= 2]
    return [m for m in macs if "Icon: input-gaming" in _bt("info", m)]


def release_controllers():
    """The car software stopped: disconnect every controller and refuse it back (blocked), so it does not reconnect and
    stay on; a controller with no host to join switches itself off after its search times out (about a minute)."""
    for mac in paired_controllers():
        _bt("block", mac)        # blocking also disconnects it
        _bt("disconnect", mac)


def allow_controllers():
    """The car software started: controllers may connect again (press the Xbox / PS button to turn one on)."""
    for mac in paired_controllers():
        _bt("unblock", mac)


def _ioctl_u8(fd, req, size=1):
    buf = array.array("B", [0] * size)
    fcntl.ioctl(fd, req, buf)
    return buf


def describe(fd):
    """(name, number of axes, number of buttons) the joystick driver reports."""
    name = _ioctl_u8(fd, 0x80006A13 | (128 << 16), 128).tobytes().split(b"\0", 1)[0].decode(errors="replace")   # JSIOCGNAME(128)
    return name, _ioctl_u8(fd, 0x80016A11)[0], _ioctl_u8(fd, 0x80016A12)[0]                                      # JSIOCGAXES, JSIOCGBUTTONS


def run(path, on_open, on_tick, on_lost, on_ignored=lambda name: None):
    """Thread body: open the controller when it appears, tick at 20 Hz while connected, report loss.
    on_tick(pad) runs every 50 ms whether or not an event arrived (commands must keep flowing for the lease).
    A controller without a known layout is never driven: on_ignored(name) is told once, and it is looked at again
    every 2 s (it may be swapped for a known one)."""
    ignored = None
    while True:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            ignored = None
            time.sleep(1)
            continue
        try:
            name, n_axes, n_btns = describe(fd)
            layout = layout_for(name, n_axes, n_btns)
        except OSError:
            name, layout = "unreadable", None
        if layout is None:
            os.close(fd)
            if name != ignored:
                on_ignored(name)
                ignored = name
            time.sleep(2)
            continue
        ignored = None
        pad = Pad(layout)
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
