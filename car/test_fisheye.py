"""Run: .venv/bin/python -m unittest car.test_fisheye   (no camera needed: a simulated fisheye lens)"""
import os
import tempfile
import time
import unittest

import cv2
import numpy as np

from car import fisheye, perf

W, H = 480, 360
K_TRUE = np.array([[150.0, 0, 240], [0, 150.0, 180], [0, 0, 1]])
D_TRUE = np.array([[-0.05], [0.02], [-0.01], [0.002]])


def views(n=15, seed=1):
    """The board's corners as a fisheye camera with K_TRUE, D_TRUE would see them from n different poses."""
    rng = np.random.RandomState(seed)
    objp = np.zeros((1, fisheye.BOARD[0] * fisheye.BOARD[1], 3))
    objp[0, :, :2] = np.mgrid[0:fisheye.BOARD[0], 0:fisheye.BOARD[1]].T.reshape(-1, 2)
    objp -= objp.mean(axis=1, keepdims=True) * [1, 1, 0]
    out = []
    while len(out) < n:
        rvec = rng.uniform(-0.5, 0.5, (3, 1))
        tvec = np.array([[rng.uniform(-3, 3)], [rng.uniform(-2, 2)], [rng.uniform(7, 14)]])
        pts, _ = cv2.fisheye.projectPoints(objp, rvec, tvec, K_TRUE, D_TRUE)
        if (pts[..., 0].min() > 5 and pts[..., 0].max() < W - 5 and pts[..., 1].min() > 5 and pts[..., 1].max() < H - 5):
            out.append(pts.reshape(-1, 1, 2).astype(np.float32))
    return out


class Lens(unittest.TestCase):
    def test_calibration_recovers_the_lens(self):
        calib = fisheye.calibrate(views(), (W, H))
        K = np.array(calib["K"])
        self.assertLess(calib["rms"], 0.1)
        self.assertAlmostEqual(K[0, 0], 150, delta=3)                    # focal length
        self.assertAlmostEqual(K[0, 2], 240, delta=3)                    # centre
        np.testing.assert_allclose(calib["D"], D_TRUE.ravel(), atol=0.02)

    def test_boxes_map_back_onto_the_curved_picture(self):
        und = fisheye.Undistorter({"size": [W, H], "K": K_TRUE.tolist(), "D": D_TRUE.ravel().tolist()}, (W, H))
        mid = und.to_display([0.45, 0.45, 0.55, 0.55])                    # centre: barely moves
        self.assertAlmostEqual((mid[0] + mid[2]) / 2, 0.5, delta=0.02)
        edge = und.to_display([0.8, 0.4, 0.9, 0.6])                       # near the edge: pulled outward on screen differs
        self.assertGreater(edge[2], edge[0])
        self.assertTrue(all(0 <= v <= 1 for v in edge))
        img = np.zeros((H, W, 3), np.uint8)
        self.assertEqual(und.apply(img).shape, img.shape)
        self.assertGreater(und.f_frac, 0)
        half = fisheye.Undistorter({"size": [W, H], "K": K_TRUE.tolist(), "D": D_TRUE.ravel().tolist()}, (W // 2, H // 2))
        self.assertAlmostEqual(half.K[0, 0], 75)                          # lower camera quality: lens scaled

    def test_the_printable_board_is_detectable(self):
        path = os.path.join(tempfile.mkdtemp(), "board.png")
        fisheye.board_png(path)
        self.assertIsNotNone(fisheye.find_corners(cv2.imread(path)))
        self.assertIsNone(fisheye.find_corners(np.full((360, 480, 3), 128, np.uint8)))

    def test_hands_free_keeps_only_new_views(self):
        v = views(3)
        self.assertTrue(fisheye.is_new_view(v[0], [], (W, H)))
        self.assertFalse(fisheye.is_new_view(v[0] + 2, [v[0]], (W, H)))        # nudged 2 px: same view, skipped
        self.assertTrue(fisheye.is_new_view(v[1], [v[0]], (W, H)))             # a different pose: kept

    def test_save_and_load(self):
        path = os.path.join(tempfile.mkdtemp(), "calib.json")
        fisheye.save(path, {"rms": 0.3})
        self.assertEqual(fisheye.load(path), {"rms": 0.3})
        self.assertIsNone(fisheye.load(path + ".missing"))


class StraightStream(unittest.TestCase):
    def test_straightens_frames_while_watched(self):
        from car import camera
        img = np.zeros((H, W, 3), np.uint8); img[:, ::40] = 255                 # vertical stripes
        cam = type("Cam", (), {"frame": cv2.imencode(".jpg", img)[1].tobytes(), "look": {"cam_quality": "normal"},
                               "acquire": lambda self: None, "release": lambda self: None})()
        calib = {"size": [W, H], "K": K_TRUE.tolist(), "D": D_TRUE.ravel().tolist()}
        st = camera.Straightened(cam, lambda: calib)
        st.acquire()
        for _ in range(100):
            if st.frame:
                break
            time.sleep(0.02)
        self.assertIsNotNone(st.frame)
        self.assertNotEqual(st.frame, cam.frame)                                   # a different, straightened picture
        st.release()
        time.sleep(0.2)
        self.assertIsNone(st._thread)                                              # stops when nobody watches


class Perf(unittest.TestCase):
    def test_timings_and_rates(self):
        for ms in (10, 20, 30):
            perf.timing("t.x", ms)
        for _ in range(20):
            perf.tick("t.rate")
        with perf.timer("t.block"):
            time.sleep(0.01)
        s = perf.snapshot()
        self.assertEqual(s["t.x"], {"avg": 20.0, "max": 30.0})
        self.assertEqual(s["t.rate"]["rate"], 2.0)                       # 20 in a 10 s window
        self.assertGreaterEqual(s["t.block"]["avg"], 9)
        self.assertIn("rss_mb", s["process"])


if __name__ == "__main__":
    unittest.main()
