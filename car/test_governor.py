"""Run: .venv/bin/python -m unittest car.test_governor"""
import unittest

from car.drivers import Mock, pulses
from car.governor import Governor, expo

LIM = {"max_forward": 1.0, "max_reverse": 1.0, "max_steer": 1.0}
CAL = {"steer_left": 500, "steer_right": 250, "throttle_fwd": 500, "throttle_stop": 300, "throttle_rev": 210}


class T(unittest.TestCase):
    def setUp(self):
        self.now, self.drv = 0.0, Mock()
        self.g = Governor(self.drv, dict(LIM), 0.25, clock=lambda: self.now)

    def test_starts_neutral(self):
        self.assertEqual(self.drv.history[0], (0.0, 0.0))

    def test_command_applies(self):
        self.assertTrue(self.g.command(0.5, 0.7, 1))
        self.g.tick()
        self.assertEqual(self.drv.last, (0.5, 0.7))

    def test_lease_expiry_stops_car(self):
        self.g.command(0.5, 0.7, 1)
        self.g.tick()
        self.now = 0.26
        self.g.tick()
        self.assertEqual(self.drv.last, (0.0, 0.0))

    def test_fresh_command_renews_lease(self):
        self.g.command(0, 0.5, 1)
        self.now = 0.2
        self.g.command(0, 0.5, 2)
        self.now = 0.4
        self.g.tick()
        self.assertEqual(self.drv.last, (0.0, 0.5))

    def test_release_stops_immediately(self):
        self.g.command(0, 1, 1)
        self.g.tick()
        self.g.release()
        self.assertEqual(self.drv.last, (0.0, 0.0))

    def test_stale_seq_ignored_and_reset_after_release(self):
        self.assertTrue(self.g.command(0, 0.5, 5))
        self.assertFalse(self.g.command(0, 1.0, 5))
        self.assertFalse(self.g.command(0, 1.0, 3))
        self.g.release()
        self.assertTrue(self.g.command(0, 0.5, 1))  # new controller starts at low seq

    def test_garbage_rejected(self):
        for bad in [(float("nan"), 0), (0, float("inf")), ("x", 0), (None, 0)]:
            self.assertFalse(self.g.command(*bad, seq=9))

    def test_clamped_to_limits(self):
        g = Governor(self.drv, {"max_forward": 0.3, "max_reverse": 0.2, "max_steer": 0.5}, clock=lambda: self.now)
        g.command(2, 2, 1)
        g.tick()
        self.assertEqual(self.drv.last, (0.5, 0.3))
        g.command(-2, -2, 2)
        g.tick()
        self.assertEqual(self.drv.last, (-0.5, -0.2))

    def test_driver_error_retries(self):
        class Flaky(Mock):
            n = 0

            def set(s, a, b):
                s.n += 1
                if s.n == 2:
                    raise IOError("i2c")
                Mock.set(s, a, b)
        d = Flaky()
        g = Governor(d, dict(LIM), clock=lambda: self.now)
        g.command(0, 1, 1)
        g.tick()   # fails
        g.tick()   # retried
        self.assertEqual(d.last, (0.0, 1.0))

    def test_reverse_tap_then_real_reverse(self):
        g = Governor(self.drv, dict(LIM), 5.0, clock=lambda: self.now, reverse_tap={"power": 0.35, "tap_s": 0.3, "settle_s": 0.5})
        g.command(0, -0.5, 1)
        for t, want in [(0.0, -0.35), (0.2, -0.35), (0.4, 0.0), (0.7, 0.0), (0.9, -0.5)]:
            self.now = t
            g.tick()
            self.assertEqual(self.drv.last[1], want, "t=%s" % t)

    def test_forward_rearms_reverse_tap_and_neutral_cancels_it(self):
        g = Governor(self.drv, dict(LIM), 5.0, clock=lambda: self.now, reverse_tap={"power": 0.35, "tap_s": 0.3, "settle_s": 0.5})
        g.command(0, -0.5, 1)
        for t in (0.0, 0.4, 0.9):                                  # tap, settle, real reverse
            self.now = t
            g.tick()
        self.assertEqual(self.drv.last[1], -0.5)
        g.command(0, 0.4, 2); self.now = 1.0; g.tick()             # forward
        g.command(0, -0.5, 3); self.now = 1.1; g.tick()            # reverse again: must tap again
        self.assertEqual(self.drv.last[1], -0.35)
        g.command(0, 0, 4); self.now = 1.2; g.tick()               # neutral abandons the tap
        self.assertEqual(self.drv.last[1], 0.0)

    def test_reverse_tap_needed_again_after_a_long_neutral(self):
        g = Governor(self.drv, dict(LIM), 5.0, clock=lambda: self.now, reverse_tap={"power": 0.35, "tap_s": 0.3, "settle_s": 0.5, "rearm_s": 2.0})
        g.command(0, -0.5, 1)
        for t in (0.0, 0.4, 0.9):                                  # tap, settle, real reverse
            self.now = t; g.tick()
        g.command(0, 0, 2); self.now = 1.0; g.tick()               # short neutral: still armed
        g.command(0, -0.5, 3); self.now = 1.5; g.tick()
        self.assertEqual(self.drv.last[1], -0.5)
        g.command(0, 0, 4); self.now = 2.0; g.tick()               # long neutral: the ESC may have forgotten
        self.now = 4.1; g.tick()
        g.command(0, -0.5, 5); self.now = 4.2; g.tick()
        self.assertEqual(self.drv.last[1], -0.35)                  # so: tap again

    def test_obstacle_brake_stops_forward_only(self):
        ahead = [None]
        self.g.forward_block = lambda: ahead[0]
        self.g.command(0.3, 0.8, 1); self.g.tick()
        self.assertEqual(self.drv.last, (0.3, 0.8))
        ahead[0] = "person"; self.g.tick()                         # mid-lease: stops at once
        self.assertEqual(self.drv.last, (0.3, 0.0))
        self.assertEqual(self.g.braking, "person")
        self.g.command(-0.3, -0.5, 2); self.g.tick()               # steering and reverse still work: back away
        self.assertEqual(self.drv.last, (-0.3, -0.5))
        ahead[0] = None; self.g.command(0, 0.8, 3); self.g.tick()
        self.assertEqual((self.drv.last, self.g.braking), ((0.0, 0.8), None))

    def test_max_power_scales_throttle_not_steering_and_ignores_garbage(self):
        self.g.set_power(0.5)
        self.g.command(0.8, 1.0, 1); self.g.tick()
        self.assertEqual(self.drv.last, (0.8, 0.5))
        self.g.command(0, -1.0, 2); self.g.tick()
        self.assertEqual(self.drv.last, (0.0, -0.5))
        for bad in ("x", None, float("nan"), float("inf")):
            self.g.set_power(bad)
        self.assertEqual(self.g.power, 0.5)
        self.g.set_power(5); self.assertEqual(self.g.power, 1.0)    # never above the configured limits
        self.g.set_power(0); self.assertEqual(self.g.power, 0.1)

    def test_unchanged_output_is_rewritten_every_second(self):
        n = len(self.drv.history)
        self.now = 0.5; self.g.tick()
        self.assertEqual(len(self.drv.history), n)          # nothing changed, no write
        self.now = 1.1; self.g.tick()
        self.assertEqual(len(self.drv.history), n + 1)      # refreshed (a reset chip gets its pulse back)

    def test_watchdog_starves_only_while_driver_keeps_failing(self):
        beats, broken = [], [True]

        class Dead(Mock):
            def set(s, a, b):
                if broken[0]:
                    raise IOError("i2c gone")
                Mock.set(s, a, b)
        g = Governor(Dead(quiet=True), dict(LIM), clock=lambda: self.now, heartbeat=lambda: beats.append(self.now))
        beats.clear()                                       # the constructor's own tick (t=0) is the first failure
        for t in (0.1, 0.5, 0.9):
            self.now = t; g.tick()
        self.assertEqual(len(beats), 3)                     # short glitch: still healthy
        self.now = 1.2; g.tick()
        self.assertEqual(len(beats), 3)                     # failing > 1 s: systemd will restart us
        broken[0] = False; self.now = 1.3; g.tick()
        self.assertEqual(len(beats), 4)                     # recovered

    def test_seq_must_be_an_int(self):
        for bad in ("1", None, 1.5, True):
            self.assertFalse(self.g.command(0, 0, bad))

    def test_dead_zone_offset(self):
        cal = dict(CAL, throttle_min=0.2)
        self.assertEqual(pulses(cal, 0, 0), (375, 300))      # zero stays neutral
        self.assertEqual(pulses(cal, 0, 0.01)[1], 342)       # tiny input jumps past the dead zone
        self.assertEqual(pulses(cal, 0, 1)[1], 500)          # full stays full
        self.assertEqual(pulses(cal, 0, -1)[1], 210)
        self.assertEqual(pulses(cal, 0, -0.19)[1], 268)      # reverse tap: same pulse that worked on the car

    def test_steering_trim_shifts_centre_and_stays_in_range(self):
        cal = dict(CAL, steer_trim=0.1)
        self.assertEqual(pulses(cal, 0, 0)[0], 362)          # centre 375 moved toward right (250) by 0.1 * 125
        self.assertEqual(pulses(cal, 1, 0)[0], 250)          # never past full lock
        self.assertEqual(pulses(cal, -1, 0)[0], 488)

    def test_rewrite_forces_the_next_tick_to_write(self):
        n = len(self.drv.history)
        self.g.rewrite(); self.g.tick()
        self.assertEqual(len(self.drv.history), n + 1)

    def test_steering_expo_softens_centre_keeps_full_lock(self):
        self.assertEqual((expo(1, 0.6), expo(-1, 0.6), expo(0, 0.6)), (1, -1, 0))
        self.assertAlmostEqual(expo(0.5, 0.0), 0.5)                       # sport: linear
        self.assertAlmostEqual(expo(0.5, 0.6), 0.275)                     # indoor: half stick is about a quarter turn
        self.g.expo = 0.6
        self.g.command(0.5, 0, 1); self.g.tick()
        self.assertAlmostEqual(self.drv.last[0], 0.275)
        self.g.command(-2, 0, 2); self.g.tick()
        self.assertEqual(self.drv.last[0], -1.0)                         # out-of-range input still lands on full lock

    def test_pulse_math_matches_calibration(self):
        p = lambda a, b: pulses(CAL, a, b)
        self.assertEqual(p(0, 0), (375, 300))
        self.assertEqual(p(-1, 1), (500, 500))
        self.assertEqual(p(1, -1), (250, 210))
        self.assertEqual(p(0, 0.5), (375, 400))


if __name__ == "__main__":
    unittest.main()
