"""Run: .venv/bin/python -m unittest car.test_vision   (needs opencv + numpy in .venv)"""
import unittest

import cv2
import numpy as np

from car import vision

CFG = {"kp": 0.9, "kd": 0.0, "max_power": 0.4, "creep": 0.4, "far_size": 0.2, "near_size": 0.45, "stop_m": 1.5, "centre_tol": 0.15, "ramp": 1.0}


def frame_with(colour_bgr=None, x=0, w=0):
    img = np.full((360, 480, 3), 40, np.uint8)
    if colour_bgr:
        cv2.rectangle(img, (x, 120), (x + w, 240), colour_bgr, -1)
    return img


class Colour(unittest.TestCase):
    def test_finds_red_where_it_is_and_nothing_when_absent(self):
        o = vision.find_colour(frame_with((0, 0, 255), x=360, w=80), "red")
        self.assertAlmostEqual((o["box"][0] + o["box"][2]) / 2, 400 / 480, delta=0.03)   # right side
        self.assertIsNone(vision.find_colour(frame_with((0, 0, 255), x=360, w=80), "blue"))
        self.assertIsNone(vision.find_colour(frame_with(), "red"))


class Distance(unittest.TestCase):
    def test_known_size_only(self):
        self.assertAlmostEqual(vision.distance_m("person", [0.4, 0.25, 0.6, 0.75], 90), 1.7)   # half-frame-tall person
        self.assertIsNone(vision.distance_m("kite", [0.4, 0.2, 0.6, 0.8], 90))


class Lock(unittest.TestCase):
    def test_stays_on_the_chosen_one(self):
        left, right = {"label": "person", "box": [0.1, 0.2, 0.3, 0.8]}, {"label": "person", "box": [0.7, 0.1, 0.95, 0.95]}
        moved = {"label": "person", "box": [0.14, 0.2, 0.34, 0.8]}
        self.assertIs(vision.pick_locked([right, moved], left["box"]), moved)    # not the bigger one, the same one
        self.assertIsNone(vision.pick_locked([right], left["box"]))              # it left: lost, not swapped
        self.assertIsNone(vision.pick_locked([], left["box"]))


class Follow(unittest.TestCase):
    def obs(self, cx, y0=0.4, h=0.15, dist=None):
        return {"box": [cx - 0.1, y0, cx + 0.1, y0 + h], "dist": dist}

    def test_steers_toward_and_creeps_when_far_and_centred(self):
        s, t = vision.Follower("person", CFG).step(self.obs(0.55), 0.0)
        self.assertGreater(s, 0)                        # target a bit right: steer right
        self.assertAlmostEqual(t, 0.4)                  # small (far) and centred: full creep
        s, _ = vision.Follower("person", CFG).step(self.obs(0.2), 0.0)
        self.assertLess(s, 0)

    def test_slower_as_it_gets_closer(self):
        far = vision.Follower("person", CFG).step(self.obs(0.5, h=0.2), 0.0)[1]
        mid = vision.Follower("person", CFG).step(self.obs(0.5, h=0.35), 0.0)[1]
        self.assertGreater(far, mid)
        self.assertGreater(mid, 0)

    def test_every_close_sign_stops(self):
        for o in (self.obs(0.5, h=0.5),                          # tall enough
                  self.obs(0.5, h=0.3, dist=1.2),                # rough distance inside 1.5 m
                  self.obs(0.5, y0=0.0, h=0.3),                  # head cut off at the top: right in front
                  self.obs(0.5, y0=0.7, h=0.29)):                # feet cut off at the bottom
            self.assertEqual(vision.Follower("person", CFG).step(o, 0.0)[1], 0.0, o)

    def test_off_centre_turns_first(self):
        _, t = vision.Follower("person", CFG).step(self.obs(0.9), 0.0)
        self.assertLessEqual(t, 0.4 * 0.4)

    def test_not_seen_stops_at_once(self):
        f = vision.Follower("person", CFG)
        self.assertGreater(f.step(self.obs(0.5), 0.0)[1], 0)
        self.assertEqual(f.step(None, 0.05), (0.0, 0.0))  # one missed detection: stopped, no coasting


if __name__ == "__main__":
    unittest.main()
