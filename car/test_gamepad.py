"""Run: .venv/bin/python -m unittest car.test_gamepad   (the PS3 mapping logic; no controller needed)"""
import unittest

from car.gamepad import AX_L2, AX_R2, AX_RY, AX_STEER, BTN_DOWN, BTN_L1, BTN_PS, BTN_R1, BTN_START, BTN_UP, BTN_X, Pad

AXIS, BUTTON, INIT = 0x02, 0x01, 0x80
FULL = 32767


class T(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.p = Pad(clock=lambda: self.now)

    def axis(self, n, v):
        self.p.feed(AXIS, n, v)

    def press(self, n, down=True):
        self.p.feed(BUTTON, n, 1 if down else 0)

    def test_steering_with_dead_zone(self):
        self.axis(AX_STEER, -FULL)
        self.assertAlmostEqual(self.p.command()[0], -1.0)
        self.axis(AX_STEER, 1500)                                # stick wobble
        self.assertEqual(self.p.command()[0], 0.0)

    def test_half_pressed_trigger_at_connect_is_ignored(self):
        self.p.feed(AXIS | INIT, AX_R2, 0)                       # driver's first guess: "half pressed"
        self.assertEqual(self.p.command(), (0.0, 0.0))
        self.assertFalse(self.p.moved())
        self.axis(AX_R2, -FULL)                                  # seen released once: now armed
        self.axis(AX_R2, FULL)
        self.assertAlmostEqual(self.p.command()[1], 1.0)

    def test_r2_forward_l2_reverse_proportional(self):
        for ax in (AX_R2, AX_L2):
            self.axis(ax, -FULL)
        self.axis(AX_R2, 0)
        self.assertAlmostEqual(self.p.command()[1], 0.5, places=3)
        self.axis(AX_R2, -FULL); self.axis(AX_L2, FULL)
        self.assertAlmostEqual(self.p.command()[1], -1.0)

    def test_right_stick_when_no_trigger(self):
        self.axis(AX_RY, -FULL)                                  # stick up
        self.assertAlmostEqual(self.p.command()[1], 1.0)

    def test_x_halts_until_throttle_released(self):
        self.axis(AX_R2, -FULL); self.axis(AX_R2, FULL)
        self.press(BTN_X)
        self.assertEqual(self.p.command()[1], 0.0)               # held trigger is ignored
        self.assertEqual(self.p.command()[1], 0.0)
        self.axis(AX_R2, -FULL)                                  # released
        self.p.command()
        self.axis(AX_R2, FULL)
        self.assertAlmostEqual(self.p.command()[1], 1.0)         # drives again

    def test_start_and_dpad_actions(self):
        self.press(BTN_START); self.press(BTN_UP); self.press(BTN_DOWN); self.press(BTN_R1); self.press(BTN_L1)
        self.p.feed(BUTTON | INIT, BTN_START, 1)                 # the initial state dump is not a press
        self.assertEqual(self.p.actions(), ["take", "power+", "power-", "mode+", "mode-"])
        self.assertEqual(self.p.actions(), [])

    def test_ps_held_three_seconds_means_shutdown(self):
        self.press(BTN_PS)
        self.now = 2.9
        self.assertEqual(self.p.actions(), [])
        self.press(BTN_PS, False); self.press(BTN_PS)            # let go early: the count restarts
        self.now = 5.0
        self.assertEqual(self.p.actions(), [])
        self.now = 5.95
        self.assertEqual(self.p.actions(), ["shutdown"])
        self.assertEqual(self.p.actions(), [])                   # once


if __name__ == "__main__":
    unittest.main()
