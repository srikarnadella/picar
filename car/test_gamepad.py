"""Run: .venv/bin/python -m unittest car.test_gamepad   (controller layouts and safety rules; no controller needed)"""
import unittest

from car import gamepad
from car.gamepad import PS3, XBOX, Pad, layout_for

AXIS, BUTTON, INIT = 0x02, 0x01, 0x80
FULL = 32767


class Base(unittest.TestCase):
    layout = PS3

    def setUp(self):
        self.now = 0.0
        self.p = Pad(self.layout, clock=lambda: self.now)

    def axis(self, n, v, init=False):
        self.p.feed(AXIS | (INIT if init else 0), n, v)

    def press(self, n, down=True):
        self.p.feed(BUTTON, n, 1 if down else 0)

    def rest(self):
        """The driver's state dump on connect: sticks centred, triggers released."""
        L = self.layout
        for ax, v in ((L["steer"], 0), (L["stick"], 0), (L["fwd"], -FULL), (L["rev"], -FULL)):
            self.axis(ax, v, init=True)


class Safety(Base):
    def test_only_known_controllers_with_the_needed_controls(self):
        self.assertIs(layout_for("Sony PLAYSTATION(R)3 Controller", 6, 17), PS3)
        self.assertIs(layout_for("Xbox Wireless Controller", 8, 15), XBOX)
        self.assertIsNone(layout_for("8BitDo SN30 Pro", 8, 15))              # unknown: never driven
        self.assertIsNone(layout_for("Xbox Wireless Controller", 6, 15))     # known name, missing controls: never driven

    def test_the_wall_crash_cannot_happen(self):
        """The Xbox controller read with the PS3 layout: its released right trigger (-1 on axis 4) is where the PS3
        layout reads the right stick, i.e. "full forward". Now it is never driven: wrong name for the PS3 layout..."""
        self.assertIsNot(layout_for("Xbox Wireless Controller", 8, 15), PS3)
        # ...and even if a layout were wrong, a controller that says "full forward" on connect never gets control
        for ax, v in ((0, 0), (2, 0), (4, -FULL), (5, -FULL)):
            self.axis(ax, v, init=True)
        self.assertEqual(self.p.command(), (0.0, 0.0))
        self.assertFalse(self.p.moved())

    def test_nothing_until_every_driving_axis_reported_neutral(self):
        self.axis(PS3["steer"], 0, init=True)                                 # only one axis so far
        self.assertEqual(self.p.command(), (0.0, 0.0))
        self.axis(PS3["steer"], -FULL)                                        # stick held at connect
        self.rest(); self.axis(PS3["steer"], -FULL)
        self.assertEqual(self.p.command(), (0.0, 0.0))
        self.axis(PS3["steer"], 0)                                            # let go: now it may drive
        self.assertEqual(self.p.command(), (0.0, 0.0))
        self.axis(PS3["steer"], -FULL)
        self.assertAlmostEqual(self.p.command()[0], -1.0)

    def test_half_pressed_trigger_at_connect_is_ignored(self):
        self.rest()
        self.p.armed.clear()
        self.axis(PS3["fwd"], 0, init=True)                                   # driver's first guess: "half pressed"
        self.assertEqual(self.p.command(), (0.0, 0.0))
        self.assertFalse(self.p.moved())
        self.axis(PS3["fwd"], -FULL)                                          # seen released once: now armed
        self.p.command()
        self.axis(PS3["fwd"], FULL)
        self.assertAlmostEqual(self.p.command()[1], 1.0)


class Ps3(Base):
    def setUp(self):
        super().setUp()
        self.rest()
        self.p.command()

    def test_steering_with_dead_zone(self):
        self.axis(PS3["steer"], -FULL)
        self.assertAlmostEqual(self.p.command()[0], -1.0)
        self.axis(PS3["steer"], 1500)                                         # stick wobble
        self.assertEqual(self.p.command()[0], 0.0)

    def test_r2_forward_l2_reverse_proportional(self):
        self.axis(PS3["fwd"], 0)
        self.assertAlmostEqual(self.p.command()[1], 0.5, places=3)
        self.axis(PS3["fwd"], -FULL); self.axis(PS3["rev"], FULL)
        self.assertAlmostEqual(self.p.command()[1], -1.0)

    def test_right_stick_when_no_trigger(self):
        self.axis(PS3["stick"], -FULL)                                        # stick up
        self.assertAlmostEqual(self.p.command()[1], 1.0)

    def test_x_halts_until_throttle_released(self):
        self.axis(PS3["fwd"], FULL)
        self.press(PS3["halt"][0])
        self.assertEqual(self.p.command()[1], 0.0)                            # held trigger is ignored
        self.axis(PS3["fwd"], -FULL)                                          # released
        self.p.command()
        self.axis(PS3["fwd"], FULL)
        self.assertAlmostEqual(self.p.command()[1], 1.0)                      # drives again

    def test_start_and_dpad_actions(self):
        up, down = PS3["power_btns"]
        for b in (PS3["take"], up, down, PS3["mode_up"], PS3["mode_down"]):
            self.press(b)
        self.p.feed(BUTTON | INIT, PS3["take"], 1)                            # the initial state dump is not a press
        self.assertEqual(self.p.actions(), ["take", "power+", "power-", "mode+", "mode-"])
        self.assertEqual(self.p.actions(), [])

    def test_ps_held_three_seconds_means_shutdown(self):
        self.press(PS3["shutdown"])
        self.now = 2.9
        self.assertEqual(self.p.actions(), [])
        self.press(PS3["shutdown"], False); self.press(PS3["shutdown"])       # let go early: the count restarts
        self.now = 5.0
        self.assertEqual(self.p.actions(), [])
        self.now = 5.95
        self.assertEqual(self.p.actions(), ["shutdown"])
        self.assertEqual(self.p.actions(), [])                                # once


class Xbox(Base):
    layout = XBOX

    def setUp(self):
        super().setUp()
        self.rest()
        self.axis(XBOX["power_hat"], 0, init=True)
        self.p.command()

    def test_released_triggers_are_neutral_and_triggers_drive(self):
        self.assertEqual(self.p.command(), (0.0, 0.0))                        # the crash case, read correctly
        self.axis(XBOX["fwd"], FULL)                                          # right trigger
        self.assertAlmostEqual(self.p.command()[1], 1.0)
        self.axis(XBOX["fwd"], -FULL); self.axis(XBOX["rev"], 0)              # left trigger half
        self.assertAlmostEqual(self.p.command()[1], -0.5, places=3)

    def test_a_or_b_halts(self):
        for b in XBOX["halt"]:
            self.axis(XBOX["rev"], -FULL); self.axis(XBOX["fwd"], FULL)
            self.press(b)
            self.assertEqual(self.p.command()[1], 0.0)
            self.axis(XBOX["fwd"], -FULL); self.p.command()

    def test_dpad_hat_one_action_per_push(self):
        hat = XBOX["power_hat"]
        self.axis(hat, -FULL); self.axis(hat, -FULL); self.axis(hat, 0)       # up, held, released
        self.axis(hat, FULL)                                                  # down
        self.press(XBOX["take"]); self.press(XBOX["mode_up"])
        self.assertEqual(self.p.actions(), ["power+", "power-", "take", "mode+"])


class Bluetooth(unittest.TestCase):
    def test_controllers_are_blocked_on_stop_and_unblocked_on_start(self):
        calls, real = [], gamepad._bt
        info = {"00:00:00:00:00:01": "Icon: input-gaming", "00:00:00:00:00:02": "Icon: input-gaming", "AA:BB:CC:DD:EE:FF": "Icon: audio-card"}

        def fake(*args):
            calls.append(args)
            if args == ("paired-devices",):
                return "Device 00:00:00:00:00:01 Xbox Wireless Controller\nDevice 00:00:00:00:00:02 Sony PLAYSTATION(R)3 Controller\nDevice AA:BB:CC:DD:EE:FF Speaker\n"
            return info.get(args[1], "") if args[0] == "info" else ""
        gamepad._bt = fake
        try:
            self.assertEqual(gamepad.paired_controllers(), ["00:00:00:00:00:01", "00:00:00:00:00:02"])   # not the speaker
            calls.clear(); gamepad.release_controllers()
            self.assertIn(("block", "00:00:00:00:00:01"), calls)
            self.assertIn(("disconnect", "00:00:00:00:00:01"), calls)
            self.assertNotIn(("block", "AA:BB:CC:DD:EE:FF"), calls)
            calls.clear(); gamepad.allow_controllers()
            self.assertEqual([c for c in calls if c[0] == "unblock"], [("unblock", "00:00:00:00:00:01"), ("unblock", "00:00:00:00:00:02")])
        finally:
            gamepad._bt = real


if __name__ == "__main__":
    unittest.main()
