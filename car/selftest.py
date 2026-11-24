"""Slow wiring/calibration check: steer left, right, centre, then a little forward and backward.
Run on the Pi with the WHEELS OFF THE GROUND:   python -m car.selftest      (--mock dry-runs on the Mac)
Goes through the Governor, so the failsafe applies; Ctrl-C or any error ends at neutral."""
import argparse
import itertools
import json
import os
import subprocess
import time

from car.drivers import Mock, Pca9685, load_settings, pulses
from car.governor import Governor

HERE = os.path.dirname(os.path.abspath(__file__))


def service_running():
    """Two processes writing the same PCA9685 would fight; the server only re-writes once a second."""
    try:
        return subprocess.call(["systemctl", "is-active", "--quiet", "picar"]) == 0
    except OSError:  # no systemd (Mac)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--power", type=float, default=0.15, help="throttle used for forward/reverse (0-1)")
    ap.add_argument("--steer", type=float, default=0.6, help="steering used for left/right (0-1)")
    ap.add_argument("--ramp", type=float, help="instead of the routine: ease throttle 0 -> this, forward then reverse, printing ticks, to find where the wheels start")
    ap.add_argument("--dir", choices=["both", "fwd", "back"], default="both", help="with --ramp: which direction(s)")
    ap.add_argument("--yes", action="store_true", help="skip the wheels-off confirmation")
    a = ap.parse_args()
    cfg = json.load(open(os.path.join(HERE, "config.json")))
    cfg["cal"]["steer_trim"] = load_settings()["steer_trim"]
    if not a.mock and service_running():
        return print("The picar service is driving the car. Stop it first:  sudo systemctl stop picar")
    if not a.yes and input("Wheels OFF the ground and clear of anything? type yes: ") != "yes":
        return print("aborted")

    drv = Mock(quiet=True) if a.mock else Pca9685(cfg["cal"])
    gov = Governor(drv, cfg["limits"], cfg["lease_s"], reverse_tap=cfg["reverse_tap"])
    gov.start()
    seq, cur = itertools.count(1), [0.0, 0.0]

    def step(label, steer, thr, hold, ramp=0.8, verbose=False):
        """Ease from where we are to (steer, thr) over `ramp` s, then hold; 20 commands/s keep the deadman happy."""
        print("%-34s steer %+.2f  throttle %+.2f  ticks ch1/ch0 %s" % (label, steer, thr, pulses(cfg["cal"], steer, thr)), flush=True)
        start, frm = time.monotonic(), tuple(cur)
        last = 0
        while time.monotonic() - start < ramp + hold:
            f = min(1.0, (time.monotonic() - start) / ramp)
            cur[0], cur[1] = frm[0] + (steer - frm[0]) * f, frm[1] + (thr - frm[1]) * f
            gov.command(cur[0], cur[1], next(seq))
            if verbose and time.monotonic() - start - last >= 0.5:
                last = time.monotonic() - start
                print("    t=%4.1fs  throttle %+.3f  ticks %s" % (last, cur[1], pulses(cfg["cal"], cur[0], cur[1])[1]), flush=True)
            time.sleep(0.05)

    S, P = a.steer, a.power
    try:
        if a.ramp:
            for sign, name in ((1, "FORWARD"), (-1, "BACKWARD")):
                if (a.dir == "fwd" and sign < 0) or (a.dir == "back" and sign > 0):
                    continue
                step("ramp %s to %.2f" % (name, a.ramp), 0, sign * a.ramp, 0.5, 12, verbose=True)
                step("stop", 0, 0, 2.0, 0.5)
            return print("done: neutral")
        step("centre", 0, 0, 1.0, 0.2)
        step("steer LEFT  (wheels turn left)", -S, 0, 1.2)
        step("centre", 0, 0, 0.6)
        step("steer RIGHT (wheels turn right)", S, 0, 1.2)
        step("centre", 0, 0, 0.8)
        step("drive FORWARD, slowly", 0, P, 1.2)
        step("stop", 0, 0, 1.5)
        step("drive BACKWARD, slowly (tap first)", 0, -P, 2.0)
        step("stop", 0, 0, 0.6)
        print("done: neutral")
    finally:
        gov.close()


if __name__ == "__main__":
    main()
