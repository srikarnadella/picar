"""Fisheye correction for vision only (the video you watch is unchanged).

Calibrate once with a printed checkerboard (OpenCV's fisheye camera model). After that the detector sees a
straightened image: boxes fit better near the edges, distances use the lens's real focal length, and following
steers by true angle. Boxes are mapped back onto the curved video before they are drawn, so they still line up.
Idea from donkeycar's image_transformations (undistort). Python 3.7, OpenCV 4.7 on the Pi.
"""
import json
import os

import cv2
import numpy as np

BOARD = (9, 6)            # inner corners of the printed checkerboard (10 x 7 squares)
NEEDED = 12               # captures for a good calibration
CRIT = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 1e-6)


def board_png(path, square=90, margin=60):
    """The checkerboard to print (any size works: only the pattern matters for the lens)."""
    cols, rows = BOARD[0] + 1, BOARD[1] + 1
    img = np.full((rows * square + 2 * margin, cols * square + 2 * margin), 255, np.uint8)
    for r in range(rows):
        for c in range(cols):
            if (r + c) % 2 == 0:
                img[margin + r * square: margin + (r + 1) * square, margin + c * square: margin + (c + 1) * square] = 0
    cv2.imwrite(path, img)


def find_corners(img):
    """The board's inner corners in one frame, or None. Takes a few hundred ms on the Pi."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    ok, corners = cv2.findChessboardCorners(gray, BOARD, cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE)
    if not ok:
        return None
    return cv2.cornerSubPix(gray, corners, (3, 3), (-1, -1), CRIT)


def is_new_view(corners, previous, size, min_change=0.08):
    """Hands-free capture keeps a view only if the board is somewhere new: centre moved or size changed enough.
    Near-duplicates teach the calibration nothing."""
    pts = np.asarray(corners, np.float64).reshape(-1, 2) / size
    centre, extent = pts.mean(0), np.linalg.norm(pts.max(0) - pts.min(0))
    for prev in previous:
        p = np.asarray(prev, np.float64).reshape(-1, 2) / size
        if np.linalg.norm(p.mean(0) - centre) + abs(np.linalg.norm(p.max(0) - p.min(0)) - extent) < min_change:
            return False
    return True


def calibrate(corner_sets, size):
    """Lens model from >= a few views of the board. size = (width, height) of the frames."""
    objp = np.zeros((1, BOARD[0] * BOARD[1], 3), np.float64)
    objp[0, :, :2] = np.mgrid[0:BOARD[0], 0:BOARD[1]].T.reshape(-1, 2)
    obj = [objp] * len(corner_sets)
    img = [np.asarray(c, np.float64).reshape(1, -1, 2) for c in corner_sets]
    rms, K, D, _, _ = cv2.fisheye.calibrate(obj, img, tuple(size), np.zeros((3, 3)), np.zeros((4, 1)),
                                            flags=cv2.fisheye.CALIB_RECOMPUTE_EXTRINSIC | cv2.fisheye.CALIB_FIX_SKEW, criteria=CRIT)
    return {"size": list(size), "K": K.tolist(), "D": D.ravel().tolist(), "rms": round(float(rms), 3)}


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save(path, calib):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(calib, f)
    os.replace(tmp, path)


class Undistorter:
    """Straightens frames of one size (the calibration is scaled if the camera quality changed)."""
    def __init__(self, calib, size, balance=0.4):
        w, h = size
        K = np.array(calib["K"], np.float64)
        K[0] *= w / calib["size"][0]
        K[1] *= h / calib["size"][1]
        K[2, 2] = 1.0
        self.K, self.D, self.size = K, np.array(calib["D"], np.float64).reshape(4, 1), (w, h)
        self.newK = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(self.K, self.D, (w, h), np.eye(3), balance=balance)
        self.m1, self.m2 = cv2.fisheye.initUndistortRectifyMap(self.K, self.D, np.eye(3), self.newK, (w, h), cv2.CV_16SC2)
        self.f_frac = self.newK[1, 1] / h     # focal length in image heights: distance = real height * f_frac / box height

    def apply(self, img):
        return cv2.remap(img, self.m1, self.m2, cv2.INTER_LINEAR)

    def to_display(self, box):
        """A box in the straightened image (0-1) -> the box around the same thing on the curved video (0-1)."""
        w, h = self.size
        xs, ys = np.linspace(box[0], box[2], 6) * w, np.linspace(box[1], box[3], 6) * h
        border = np.array([(x, ys[0]) for x in xs] + [(x, ys[-1]) for x in xs] + [(xs[0], y) for y in ys] + [(xs[-1], y) for y in ys], np.float64)
        norm = (border - [self.newK[0, 2], self.newK[1, 2]]) / [self.newK[0, 0], self.newK[1, 1]]
        pts = cv2.fisheye.distortPoints(norm.reshape(1, -1, 2), self.K, self.D).reshape(-1, 2)
        x0, y0 = pts.min(0) / (w, h)
        x1, y1 = pts.max(0) / (w, h)
        return [round(float(max(0.0, x0)), 3), round(float(max(0.0, y0)), 3), round(float(min(1.0, x1)), 3), round(float(min(1.0, y1)), 3)]
