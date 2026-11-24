"""Shared camera: captures only while at least one viewer is connected, restarts itself if it fails,
and takes camera settings live (brightness, exposure, flip) or with a quick restart (quality)."""
import io
import threading
import time

from car import perf
from car.log import log

QUALITY = {"low": (320, 240, 30), "normal": (480, 360, 30), "high": (640, 480, 35)}  # width, height, JPEG quality


def pi_frames(cam):
    import picamera  # legacy stack, on the Pi
    w, h, q = QUALITY[cam.look["cam_quality"]]
    with picamera.PiCamera(resolution=(w, h), framerate=cam.cfg["fps"]) as pc:
        time.sleep(1)  # let exposure settle
        buf, applied = io.BytesIO(), None
        for _ in pc.capture_continuous(buf, "jpeg", use_video_port=True, quality=q):
            if applied != cam.rev:  # settings changed: apply between frames, no restart
                applied, look = cam.rev, cam.look
                pc.brightness = int(look["cam_brightness"])
                pc.exposure_mode = look["cam_exposure"]
                pc.rotation = 180 if look["cam_flip"] == "upside" else 0
                pc.hflip = look["cam_flip"] == "mirror"
                pc.saturation, pc.contrast, pc.sharpness = (int(look[k]) for k in ("cam_saturation", "cam_contrast", "cam_sharpness"))
                pc.awb_mode = look["cam_awb"]
            buf.seek(0)
            yield buf.read()
            buf.seek(0)
            buf.truncate()


def mock_frames(cam):
    """Fake camera for Mac runs. Draws the current settings so changes are visible (and testable)."""
    from PIL import Image, ImageDraw, ImageEnhance
    n = 0
    while True:
        look = cam.look
        w, h, q = QUALITY[look["cam_quality"]]
        shade = int(look["cam_brightness"]) / 50.0 * (1.6 if look["cam_exposure"] == "night" else 1)
        img = Image.new("RGB", (w, h), tuple(min(255, int(c * shade)) for c in (30, 40, 60)))
        ImageDraw.Draw(img).rectangle([w // 2, h // 2, w - 20, h - 20], fill=(200, 60, 60))   # a red patch so colour settings show
        img = ImageEnhance.Color(img).enhance(1 + look["cam_saturation"] / 100.0)
        img = ImageEnhance.Contrast(img).enhance(1 + look["cam_contrast"] / 100.0)
        ImageDraw.Draw(img).text((12, 12), "MOCK CAMERA %d  %s  brightness %s  %s  %s" % (
            n, time.strftime("%H:%M:%S"), look["cam_brightness"], look["cam_exposure"], look["cam_flip"]), fill=(255, 255, 255))
        if look["cam_flip"] == "upside":
            img = img.rotate(180)
        elif look["cam_flip"] == "mirror":
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=q)
        yield out.getvalue()
        n += 1
        time.sleep(1.0 / cam.cfg["fps"])


class Straightened:
    """The camera's frames with the fisheye straightened, made once in the background for every viewer who
    chose that view (so driving is never slowed). Runs only while someone watches it; costs nothing otherwise."""
    def __init__(self, cam, calib_source):
        self.cam, self.calib_source = cam, calib_source   # calib_source() -> the lens calibration, or None to pass frames through
        self.frame, self.users, self._thread = None, 0, None
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            self.users += 1
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def release(self):
        with self._lock:
            self.users -= 1

    def _run(self):
        import cv2
        import numpy as np
        from car import fisheye
        self.cam.acquire()
        last, und = None, None
        try:
            while True:
                with self._lock:
                    if self.users <= 0:
                        self._thread, self.frame = None, None
                        return
                f = self.cam.frame
                if f is None or f is last:
                    time.sleep(0.02)
                    continue
                last = f
                img = cv2.imdecode(np.frombuffer(f, np.uint8), cv2.IMREAD_COLOR)
                calib = self.calib_source()
                if not calib:
                    self.frame = f                                  # not calibrated: the normal view
                    continue
                size = (img.shape[1], img.shape[0])
                if und is None or und.size != size or und.calib is not calib:
                    und = fisheye.Undistorter(calib, size)
                    und.calib = calib
                with perf.timer("video.straighten"):
                    ok, jpg = cv2.imencode(".jpg", und.apply(img), [cv2.IMWRITE_JPEG_QUALITY, QUALITY[self.cam.look["cam_quality"]][2]])
                if ok:
                    self.frame = jpg.tobytes()
                    perf.tick("video.straight_frames")
        finally:
            self.cam.release()


class Camera:
    def __init__(self, source, cfg, look):
        self.source, self.cfg, self.look, self.rev = source, cfg, dict(look), 0
        self.frame, self.users, self._thread, self._restart = None, 0, None, False
        self._lock = threading.Lock()

    def set_look(self, look):
        """New camera settings. Quality changes the resolution, so the capture restarts (about 2 s)."""
        if look["cam_quality"] != self.look["cam_quality"]:
            self._restart = True
        self.look, self.rev = dict(look), self.rev + 1

    def acquire(self):
        with self._lock:
            self.users += 1
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def release(self):
        with self._lock:
            self.users -= 1

    def _run(self):
        while True:
            frames = self.source(self)
            try:
                for jpg in frames:
                    self.frame = jpg
                    perf.tick("camera.frames")
                    if self.users <= 0 or self._restart:
                        break
            except Exception as e:  # camera busy, cable glitch...: viewers see No video, we retry
                log("camera error (retrying):", e)
                self.frame = None
                time.sleep(2)
            finally:
                frames.close()  # releases the camera before anyone can open it again
            with self._lock:  # stop only if still nobody wants frames; decided and cleared under one lock
                self._restart = False
                if self.users <= 0:
                    self._thread, self.frame = None, None
                    return
