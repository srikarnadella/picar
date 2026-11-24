"""Computer vision on the car: image utilities, object detection, colour tracking and following.

The image utilities and the tracking/steering approach are adapted from donkeycar
(parts/cv.py and parts/line_follower.py; MIT licence, Copyright (c) 2017 Will Roscoe, see car/LICENSE.donkeycar).
Their line follower runs at full camera speed on a Pi because it never runs a neural network: an HSV colour
threshold, a column histogram for position, a confidence check, and PID steering that slows down when off-centre.
Following reuses exactly that loop; the position comes from a colour blob (fast) or a detector box (smart).

Measured on this Pi 3B: colour tracking ~10 ms/frame; SSD-MobileNet (quantized TFLite, 2 threads) 231 ms/frame.
Python 3.7 compatible (runs on the Pi); OpenCV 4.7 there, newer on the Mac.
"""
import math
import threading
import time
from functools import reduce

import cv2
import numpy as np

from car import fisheye, perf
from car.log import log

# ---------------------------------------------------------------- image helpers (from donkeycar parts/cv.py)

def decode_jpeg(jpg):
    return cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)  # BGR


def bgr_to_rgb(img):
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def bgr_to_hsv(img):
    return cv2.cvtColor(img, cv2.COLOR_BGR2HSV)


def gaussian_blur(img, k=5):
    return cv2.GaussianBlur(img, (k, k), 0)


# ---------------------------------------------------------------- colour tracking (the line-follower method)

COLOURS = {  # HSV ranges (OpenCV hue 0-180); red wraps around 0
    "red": [((0, 120, 70), (10, 255, 255)), ((170, 120, 70), (180, 255, 255))],
    "orange": [((10, 140, 90), (22, 255, 255))],
    "yellow": [((22, 110, 90), (35, 255, 255))],
    "green": [((40, 70, 50), (85, 255, 255))],
    "blue": [((95, 120, 50), (130, 255, 255))],
}


def find_colour(img, name, min_frac=0.003):
    """Largest blob of a colour. Like the line follower: HSV threshold, a confidence floor, a position.
    Works on a 160x120 copy, so it costs a few ms. Returns an observation or None."""
    small = cv2.resize(img, (160, 120))
    hsv = bgr_to_hsv(gaussian_blur(small, 5))
    mask = reduce(cv2.bitwise_or, [cv2.inRange(hsv, lo, hi) for lo, hi in COLOURS[name]])
    if cv2.countNonZero(mask) < min_frac * mask.size:     # confidence: too few pixels, no target
        return None
    contours = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[-2]
    x, y, w, h = cv2.boundingRect(max(contours, key=cv2.contourArea))
    box = [x / 160.0, y / 120.0, (x + w) / 160.0, (y + h) / 120.0]
    return {"label": name, "score": 1.0, "box": box}


# ---------------------------------------------------------------- object detection (free: SSD-MobileNet, TFLite)

TYPICAL_HEIGHT_M = {"person": 1.7, "dog": 0.55, "cat": 0.3, "chair": 0.9, "bottle": 0.25, "cup": 0.1,
                    "sports ball": 0.22, "teddy bear": 0.3, "backpack": 0.5, "couch": 0.9, "tv": 0.6, "potted plant": 0.5}


def distance_m(label, box, vfov_deg, f_frac=None):
    """Rough distance from apparent height: d = real height * focal / box height. Only for things of known size.
    f_frac (focal length in image heights) comes from the fisheye calibration when there is one; otherwise it is
    guessed from vfov_deg in config.json, which is rougher, especially near the edges."""
    h_real, h_box = TYPICAL_HEIGHT_M.get(label), box[3] - box[1]
    if not h_real or h_box <= 0.02 or box[3] > 0.98 and box[1] < 0.02:   # unknown, tiny, or fills the frame
        return None
    f = f_frac if f_frac else 0.5 / math.tan(math.radians(vfov_deg) / 2)
    return round(h_real * f / h_box, 1)


class Detector:
    """COCO SSD-MobileNet v1, quantized TFLite (Apache 2.0, from TensorFlow's model zoo). 80 everyday classes."""
    def __init__(self, model, labels, threads=2):
        from tflite_runtime.interpreter import Interpreter  # tiny runtime; 2 threads leaves 2 cores for driving
        self.it = Interpreter(model_path=model, num_threads=threads)
        self.it.allocate_tensors()
        d = self.it.get_input_details()[0]
        self.inp, self.size = d["index"], (d["shape"][2], d["shape"][1])
        self.out = [o["index"] for o in self.it.get_output_details()]
        self.labels = [l.strip() for l in open(labels)]

    def detect(self, img, min_score=0.45):
        self.it.set_tensor(self.inp, cv2.resize(bgr_to_rgb(img), self.size)[None].astype(np.uint8))
        self.it.invoke()
        boxes, classes, scores = (self.it.get_tensor(i)[0] for i in self.out[:3])
        found = []
        for (ymin, xmin, ymax, xmax), c, s in zip(boxes, classes, scores):
            label = self.labels[int(c) + 1] if int(c) + 1 < len(self.labels) else str(int(c))
            if s >= min_score and label != "???":
                found.append({"label": label, "score": round(float(s), 2),
                              "box": [round(float(v), 3) for v in (max(0, xmin), max(0, ymin), min(1, xmax), min(1, ymax))]})
        return found


class MockDetector:
    """Mac runs: one fake person drifting left and right and one standing still on the right, so following
    and tapping a box to lock onto one person can be tried."""
    def detect(self, img, min_score=0.45):
        time.sleep(0.2)
        cx = 0.4 + 0.2 * math.sin(time.time() / 3)
        return [{"label": "person", "score": 0.9, "box": [cx - 0.12, 0.2, cx + 0.12, 0.85]},
                {"label": "person", "score": 0.8, "box": [0.78, 0.35, 0.92, 0.75]}]


# ---------------------------------------------------------------- following (the line follower's control loop)

class Follower:
    """Steer to keep the target centred (PD on the horizontal error, like the line follower's PID) and creep
    toward it, slower the closer it looks, stopping well before it. Throttle eases down off-centre (turn first),
    the line follower's rule. Safety, learned the hard way on the car:
      - not seen in the latest detection = stop at once (driving "through a blink" is how it rammed someone);
      - close = any of: box tall enough, rough distance <= stop_m, or box touching the top/bottom edge
        (up close the head or feet leave the picture and the detector's box gets cut, so size alone lies);
      - speed: the server caps power while following (follow max_power), whatever the driving mode."""
    kind = "follow"

    def __init__(self, target, cfg):
        self.target, self.cfg = target, cfg
        self.throttle, self.prev_err, self.prev_t, self.last_seen = 0.0, 0.0, None, None

    def step(self, obs, now):
        c = self.cfg
        if obs is None:
            self.throttle = 0.0
            return 0.0, 0.0                           # not seen now: stop and wait (never drive blind)
        self.last_seen = now
        x0, y0, x1, y1 = obs.get("ubox") or obs["box"]   # the straightened box when calibrated: true angle
        err = (x0 + x1) / 2 - 0.5                    # -0.5 (far left) .. +0.5 (far right)
        dt = max(1e-3, now - self.prev_t) if self.prev_t is not None else None
        d = (err - self.prev_err) / dt if dt else 0.0
        self.prev_err, self.prev_t = err, now
        steer = max(-1.0, min(1.0, c["kp"] * 2 * err + c["kd"] * d))
        size, dist = y1 - y0, obs.get("dist")
        close = size >= c["near_size"] or y0 <= 0.03 or y1 >= 0.97 or (dist is not None and dist <= c["stop_m"])
        if close:
            goal = 0.0
        else:  # slower the bigger it looks: full creep when small/far, 40% of it just before the stop size
            far = max(0.0, min(1.0, (c["near_size"] - size) / (c["near_size"] - c["far_size"])))
            goal = c["creep"] * (0.4 + 0.6 * far)
        if abs(err) > c["centre_tol"]:
            goal = min(goal, c["creep"] * 0.4)                             # off-centre: slow, turn first
        # speed up gently, slow down at once
        self.throttle = min(goal, self.throttle + c["ramp"]) if self.throttle < goal else goal
        return steer, self.throttle


# ---------------------------------------------------------------- staying on one chosen object

def centre(box):
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def pick_locked(cands, lock, gate=0.25):
    """The detector says "a person is here" every frame but not which one. Keep the same one by choosing the
    candidate nearest to where the chosen one was last seen, with a similar size. Too far away = not it."""
    if not cands:
        return None
    lx, ly = centre(lock)
    lh = lock[3] - lock[1]

    def cost(o):
        x, y = centre(o["box"])
        return math.hypot(x - lx, y - ly) + 0.5 * abs((o["box"][3] - o["box"][1]) - lh)
    best = min(cands, key=cost)
    return best if cost(best) <= gate else None


# ---------------------------------------------------------------- the vision loop

class Vision:
    """Runs only while wanted (detections shown, or following). Takes frames from the shared camera."""
    def __init__(self, cam, cfg, mock):
        self.cam, self.cfg, self.mock = cam, cfg, mock
        self.show, self.target = False, None       # show detection boxes; follow target ("person", "colour:red")
        self.lock = None                           # follow one particular object: its last known box (tapped on screen)
        self.calib, self.correct, self._und = None, True, None   # fisheye calibration, use it, straightener for the frame size
        self.objs, self.obs, self.obs_t, self.fps = [], None, 0.0, 0.0
        self.detector, self.error = None, None
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def active(self):
        return self.show or self.target is not None

    def _get_detector(self):
        if self.detector is None and self.error is None:
            try:
                self.detector = MockDetector() if self.mock else Detector(self.cfg["model"], self.cfg["labels"], self.cfg["threads"])
                log("object detector ready")
            except Exception as e:  # no model file or runtime: colour following still works
                self.error = str(e)
                log("object detector unavailable:", e)
        return self.detector

    def _run(self):
        holding, last, n, t0 = False, None, 0, time.time()
        while True:
            if not self.active:
                if holding:
                    self.cam.release()
                    holding, self.objs, self.obs, self.fps = False, [], None, 0.0
                time.sleep(0.2)
                continue
            if not holding:
                self.cam.acquire()
                holding = True
            f = self.cam.frame
            if f is None or f is last:
                time.sleep(0.02)
                continue
            with perf.timer("vision.decode"):
                last, img, now = f, decode_jpeg(f), time.time()
            und = self._undistorter(img)
            if und is not None:
                with perf.timer("vision.straighten"):
                    img = und.apply(img)
            target = self.target
            if target and target.startswith("colour:"):
                with perf.timer("vision.colour"):
                    o = find_colour(img, target[7:])
                found = [o] if o else []
            else:
                det = self._get_detector()
                with perf.timer("vision.detect"):
                    found = det.detect(img) if det else []
            for o in found:   # ubox: straightened (for steering and distance); box: on the video you see (for drawing and tapping)
                o["ubox"] = o["box"]
                o["dist"] = distance_m(o["label"], o["ubox"], self.cfg["vfov_deg"], und.f_frac if und else None)
                if und is not None:
                    o["box"] = und.to_display(o["ubox"])
            self.objs = sorted(found, key=lambda o: -o["score"])[:6]
            perf.tick("vision.frames")
            pick = [o for o in self.objs if target and o["label"] == target.replace("colour:", "")]
            lock = self.lock
            if lock is not None:   # one chosen object: stay on it; if it is not near its last place, it is lost
                o = pick_locked(pick, lock)
                if o is not None:
                    self.lock, self.obs, self.obs_t = o["box"], o, time.monotonic()
            elif pick:             # any of that kind: the biggest (usually the nearest)
                self.obs, self.obs_t = max(pick, key=lambda o: (o["box"][2] - o["box"][0]) * (o["box"][3] - o["box"][1])), time.monotonic()
            n += 1
            if now - t0 >= 2:
                self.fps, n, t0 = round(n / (now - t0), 1), 0, now

    def _undistorter(self, img):
        if not (self.calib and self.correct):
            return None
        size = (img.shape[1], img.shape[0])
        if self._und is None or self._und.size != size:   # camera quality changed: rebuild for the new size
            self._und = fisheye.Undistorter(self.calib, size)
        return self._und

    def set_calibration(self, calib):
        self.calib, self._und = calib, None

    def observation(self, max_age):
        """The followed target, if seen recently enough."""
        return self.obs if self.obs is not None and time.monotonic() - self.obs_t <= max_age else None

    def snapshot(self):
        return {"on": self.active, "fps": self.fps, "objs": self.objs,   # box: on the camera view; ubox: on the straightened view
                "follow": self.target, "error": self.error, "straightened": bool(self.calib and self.correct),
                "lock": self.lock if self.target else None}
