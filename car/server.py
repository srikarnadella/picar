"""python -m car.server [--mock]   Python 3.7 compatible.

Exactly one driver at a time (a phone or the PS3 controller); every other phone is a watcher that still
gets video and telemetry and can take control. A new phone takes over from another phone (dead or reloaded
phones never lock you out) but not from a driving controller: you tap Take control (or Halt) for that.
"""
import argparse
import asyncio
import hmac
import itertools
import json
import math
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time

import tornado.iostream
import tornado.ioloop
import tornado.web
import tornado.websocket

from car import camera, fisheye, gamepad, perf, sounds, telemetry, vision
from car.drivers import load_settings, save_settings
from car.governor import Governor
from car.log import events, log

SETTING_RANGES = {"steer_trim": (-0.3, 0.3), "default_power": (0.25, 1.0), "cam_brightness": (20, 80),
                  "cam_saturation": (-100, 100), "cam_contrast": (-100, 100), "cam_sharpness": (-100, 100)}
SETTING_CHOICES = {"cam_exposure": ("auto", "night"), "cam_flip": ("none", "upside", "mirror"), "cam_quality": ("low", "normal", "high"),
                   "cam_awb": ("auto", "sunlight", "cloudy", "tungsten"), "vision_correct": ("on", "off")}
CAM_KEYS = ("cam_brightness", "cam_exposure", "cam_flip", "cam_quality", "cam_saturation", "cam_contrast", "cam_sharpness", "cam_awb")

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.join(os.path.dirname(HERE), "dashboard")
TOKEN_FILE = os.path.join(HERE, ".token")          # full access: drive
VIEW_FILE = os.path.join(HERE, ".viewtoken")       # view-only link: watch, never drive
CALIB_FILE = os.path.join(HERE, "calib.json")      # fisheye calibration (made on the car; deploys never overwrite it)
STARTED = time.time()


def load_token(path=TOKEN_FILE, new=False):
    if new or not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_urlsafe(8 if path == VIEW_FILE else 6))
        os.chmod(path, 0o600)
    return open(path).read().strip()


def access(handler):
    """'full' for the driving key (?t=), 'view' for the view-only key (?v=), else None."""
    app = handler.application
    if hmac.compare_digest(handler.get_argument("t", ""), app.token):
        return "full"
    v = handler.get_argument("v", "")
    return "view" if v and hmac.compare_digest(v, app.view_token) else None


_nsock = []


def sd_notify(msg):
    """systemd readiness/watchdog ping; no-op when not run by systemd."""
    path = os.environ.get("NOTIFY_SOCKET")
    if path:
        if not _nsock:
            _nsock.append(socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM))
        _nsock[0].sendto(msg, "\0" + path[1:] if path[0] == "@" else path)


# ---- who drives (all of these run on the IOLoop thread)

def send(c, obj):
    try:
        c.write_message(obj if isinstance(obj, str) else json.dumps(obj))
    except tornado.websocket.WebSocketClosedError:
        pass


def announce(app):
    by = app.controller.kind if app.controller else None
    for c in list(app.clients):
        send(c, {"role": "driver" if c is app.controller else "viewer" if c.access == "view" else "watching", "by": by})


def take(app, who):
    if app.controller is not who:
        app.controller, app.rx = who, time.monotonic()
        app.gov.release()  # neutral and a fresh sequence for the new driver
        app.gov.set_power(app.settings["default_power"])  # every new driver starts at the default max power
        log({"gamepad": "PS3 controller took control", "follow": "following: %s" % getattr(who, "target", "")}.get(who.kind, "a phone took control"))
        announce(app)


MODE_ORDER = ("indoor", "normal", "sport")


def start_follow(app, target, box=None):
    """The car drives itself toward a target: a COCO label ("person") or a colour ("colour:red").
    It becomes the driver, so the phone watches; Halt or Take control on any phone (or Start on the PS3) ends it."""
    labels = app.vision_labels
    if not isinstance(target, str) or not (target in labels or target[7:] in vision.COLOURS and target.startswith("colour:")):
        return
    if box is not None:   # a tapped box: four numbers inside the picture, or the request is ignored
        try:
            box = [float(v) for v in box]
        except (TypeError, ValueError):
            return
        if len(box) != 4 or not all(0 <= v <= 1 for v in box) or box[0] >= box[2] or box[1] >= box[3]:
            return
    follower = vision.Follower(target, app.follow_cfg)
    app.vision.target, app.vision.lock, app.vision.obs = target, box, None
    take(app, follower)
    app.gov.set_power(min(app.gov.power, app.follow_cfg["max_power"]))  # following is always slow, whatever the mode

    def loop():  # 20 Hz commands through the governor, like a phone; ends the moment someone else drives
        seq = itertools.count(1)
        while app.controller is follower:
            steer, thr = follower.step(app.vision.observation(0.4), time.monotonic())   # one missed detection (~0.25 s) = stop
            app.rx = time.monotonic()
            app.gov.command(steer, thr, next(seq))
            time.sleep(0.05)
        if app.vision.target == target:
            app.vision.target, app.vision.lock = None, None
        log("stopped following %s" % target)
    threading.Thread(target=loop, daemon=True).start()


def set_mode(app, name):
    """Driving mode: max power and steering sensitivity together (values in config.json)."""
    if name not in app.modes:
        return
    app.mode = name
    app.gov.expo = app.modes[name]["expo"]
    app.gov.set_power(app.modes[name]["power"])
    log("driving mode: %s" % name)


def change_setting(app, key, value):
    """Car-wide setting from the driver's dashboard: validated, clamped, saved, applied at once."""
    if key in SETTING_CHOICES:
        if value not in SETTING_CHOICES[key]:
            return
        app.settings[key] = value
    elif key in SETTING_RANGES:
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(value):
            return
        lo, hi = SETTING_RANGES[key]
        value = max(lo, min(hi, value))
        app.settings[key] = int(round(value)) if key.startswith("cam_") else round(value, 3)   # camera values are whole numbers
    else:
        return
    save_settings(app.settings)
    log("setting %s = %s" % (key, app.settings[key]))
    if key == "steer_trim":
        app.cal["steer_trim"] = app.settings[key]
        app.gov.rewrite()  # the wheels move to the new centre now, not at the next command
    if key in CAM_KEYS:
        app.cam.set_look(app.settings)
    if key == "vision_correct":
        app.vision.correct = value == "on"


def solve_calibration(app, how):
    calib = fisheye.calibrate(app.calib_frames, app.calib_size)
    fisheye.save(CALIB_FILE, calib)
    app.vision.set_calibration(calib)
    app.calib_frames = []
    log("camera lens calibrated%s (error %.2f px)" % (how, calib["rms"]))
    return {"done": True, "rms": calib["rms"], "count": 0}


def calib_action(app, ws, action):
    """Fisheye calibration from the driver's dashboard. Board finding and the maths run off the event loop."""
    loop = tornado.ioloop.IOLoop.current()

    def later(work):
        def reply(fut):
            try:
                result = fut.result()
            except Exception as e:  # e.g. the views were too similar for a solution
                result = {"error": "Calibration failed (%s). Try more varied angles." % e}
            send(ws, {"calib": result})
        loop.run_in_executor(None, work).add_done_callback(lambda f: loop.add_callback(reply, f))

    if app.calib_auto and action not in ("stop", "clear"):   # hands-free owns the captures until it stops
        return
    if action == "capture":
        jpg = app.cam.frame
        if jpg is None:
            return send(ws, {"calib": {"error": "No camera picture yet. Is the video showing?"}})

        def capture():
            img = vision.decode_jpeg(jpg)
            size = (img.shape[1], img.shape[0])
            if app.calib_size and size != app.calib_size:   # camera quality changed mid-way: start over
                app.calib_frames = []
            corners = fisheye.find_corners(img)
            if corners is not None:
                app.calib_frames.append(corners)
                app.calib_size = size
            return {"found": corners is not None, "count": len(app.calib_frames), "needed": fisheye.NEEDED}
        later(capture)
    elif action == "finish":
        if len(app.calib_frames) < 6:
            return send(ws, {"calib": {"error": "Need at least 6 good captures (12 is better)."}})

        later(lambda: solve_calibration(app, ""))
    elif action == "auto":   # hands-free: the car captures whenever the board is somewhere new, then finishes
        app.calib_auto = True
        app.cam.acquire()
        log("hands-free lens calibration started")

        def auto():
            try:
                while app.calib_auto and ws in app.clients and len(app.calib_frames) < fisheye.NEEDED:
                    jpg = app.cam.frame
                    if jpg is None:
                        time.sleep(0.3)
                        continue
                    img = vision.decode_jpeg(jpg)
                    size = (img.shape[1], img.shape[0])
                    corners = fisheye.find_corners(img)
                    if corners is not None and fisheye.is_new_view(corners, app.calib_frames, size):
                        app.calib_frames.append(corners)
                        app.calib_size = size
                        loop.add_callback(send, ws, {"calib": {"found": True, "auto": True, "count": len(app.calib_frames), "needed": fisheye.NEEDED}})
                    elif corners is not None:
                        loop.add_callback(send, ws, {"calib": {"same": True, "count": len(app.calib_frames), "needed": fisheye.NEEDED}})
                    time.sleep(0.4)
                if app.calib_auto and len(app.calib_frames) >= fisheye.NEEDED:
                    loop.add_callback(send, ws, {"calib": solve_calibration(app, " hands-free")})
            except Exception as e:
                loop.add_callback(send, ws, {"calib": {"error": "Calibration failed (%s). Try more varied positions." % e}})
            finally:
                app.calib_auto = False
                app.cam.release()
                loop.add_callback(send, ws, {"calib": {"auto_off": True}})
        threading.Thread(target=auto, daemon=True).start()
    elif action == "stop":
        app.calib_auto = False
    elif action == "restart":
        app.calib_frames = []
        send(ws, {"calib": {"count": 0}})
    elif action == "clear":
        if os.path.exists(CALIB_FILE):
            os.remove(CALIB_FILE)
        app.vision.set_calibration(None)
        log("camera lens calibration removed")
        send(ws, {"calib": {"cleared": True, "count": len(app.calib_frames)}})


def drop(app):
    """The driver left or went silent: nobody drives, the car is neutral now."""
    if app.controller:
        log("driver left (%s): car neutral" % app.controller.kind)
    app.controller = None
    app.gov.release()
    announce(app)


class Authed(tornado.web.RequestHandler):
    allow_view = False  # most endpoints need the driving key; video also opens with the view-only key

    def prepare(self):
        kind = access(self)
        if kind is None or (kind == "view" and not self.allow_view):
            raise tornado.web.HTTPError(403)


class WS(tornado.websocket.WebSocketHandler):
    kind = "phone"

    def check_origin(self, origin):
        return True  # the token is the gate; the page may be opened by IP or .local

    def open(self):
        app = self.application
        self.access = access(self)
        if self.access is None:
            return self.close(4403)
        app.clients.add(self)
        if self.access == "view":
            announce(app)  # a viewer only ever watches
        elif app.controller is None or (app.controller.kind == "phone" and self.get_argument("join", "") != "watch"):
            take(app, self)
        else:
            announce(app)  # the controller is driving: join as a watcher

    def on_message(self, msg):
        app = self.application
        try:
            m = json.loads(msg)
        except ValueError:
            return
        if not isinstance(m, dict):  # anything malformed is ignored, never allowed to drop the connection
            return
        if self.access == "view":  # view-only: acks for the latency readout, nothing else
            if isinstance(m.get("n"), int) and not isinstance(m["n"], bool):
                send(self, '{"a":%d}' % m["n"])
            return
        driving = self is app.controller
        if driving:
            app.rx = time.monotonic()
        n = m.get("n")
        if "take" in m:
            take(app, self)
        elif "sound" in m:
            sounds.play(str(m["sound"]))
        elif "p" in m:
            if driving:
                app.gov.set_power(m["p"])
        elif "detect" in m:
            app.vision.show = bool(m["detect"])  # show what the car sees (costs CPU only while on)
        elif "follow" in m:
            if driving:
                start_follow(app, m["follow"], m.get("box"))
        elif "mode" in m:
            if driving:
                set_mode(app, m["mode"])
        elif "calib" in m:
            if driving and m["calib"] in ("capture", "finish", "restart", "clear", "auto", "stop"):
                calib_action(app, self, m["calib"])
        elif "set" in m:
            if driving and isinstance(m["set"], dict):  # calibration moves the motors: driver only
                for key, value in m["set"].items():
                    change_setting(app, key, value)
        elif isinstance(n, int) and not isinstance(n, bool):
            if driving:
                app.gov.command(m.get("s", 0), m.get("t", 0), n)
            send(self, '{"a":%d}' % n)  # watchers are acked too, so their link readout stays honest

    def on_close(self):
        app = self.application
        app.clients.discard(self)
        if self is app.controller:
            drop(app)


class Video(Authed):
    allow_view = True

    async def get(self):
        app = self.application
        straight = self.get_argument("view", "") == "straight"   # the fisheye-straightened picture, if calibrated
        cam = app.straight if straight else app.cam
        self.set_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.set_header("Cache-Control", "no-store")
        cam.acquire()
        last = None
        try:
            while True:
                f = cam.frame
                if f is not None and f is not last:
                    last = f
                    self.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(f) + f + b"\r\n")
                    await self.flush()
                    perf.tick("video.sent")
                    self.application.sent += 1  # frames actually delivered: what the dashboard shows as VIDEO fps
                else:
                    await asyncio.sleep(0.03)
        except tornado.iostream.StreamClosedError:
            pass
        finally:
            cam.release()


class Static(tornado.web.StaticFileHandler):
    def set_extra_headers(self, path):
        self.set_header("Cache-Control", "no-cache")  # revalidate (cheap 304), so phones never run an old dashboard


class Sounds(Authed):
    def get(self):
        self.write({"sounds": sounds.names()})


class Share(Authed):
    """GET: the view-only link's key. POST: a new key; anyone on the old link is cut off at once."""
    def get(self):
        self.write({"view": self.application.view_token})

    def post(self):
        app = self.application
        app.view_token = load_token(VIEW_FILE, new=True)
        for c in [c for c in app.clients if c.access == "view"]:
            c.close(4403)
        log("view-only link replaced; old link revoked")
        self.write({"view": app.view_token})


class Health(Authed):
    def get(self):
        app = self.application
        h = telemetry.health() if not app.mock else telemetry.system(True)
        roles = [("driver" if c is app.controller else c.access) for c in app.clients]
        h.update(software_up_s=round(time.time() - STARTED), driver=app.controller.kind if app.controller else None,
                 watching=roles.count("full"), viewing=roles.count("view"), pad=app.pad is not None,
                 camera_on=app.cam._thread is not None, camera_viewers=app.cam.users, outages=app.gov.outages,
                 trim=app.settings["steer_trim"], defpow=app.settings["default_power"], sim=app.mock)
        h["perf"] = perf.snapshot()
        self.write({"health": h, "events": list(events)[-60:][::-1]})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="fake motors/camera, no hardware")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()
    cfg = json.load(open(os.path.join(HERE, "config.json")))
    settings = load_settings()
    cfg["cal"]["steer_trim"] = settings["steer_trim"]  # the driver reads this same dict, so later changes apply live

    if args.mock:
        from car.drivers import Mock
        drv, source = Mock(), camera.mock_frames
    else:
        from car.drivers import Pca9685
        drv, source = Pca9685(cfg["cal"]), camera.pi_frames

    beat = [0.0]

    def heartbeat():  # fed by the governor thread, so a hung driver stops it and systemd restarts us
        if time.monotonic() - beat[0] > 0.5:
            beat[0] = time.monotonic()
            sd_notify(b"WATCHDOG=1")

    gov = Governor(drv, cfg["limits"], cfg["lease_s"], heartbeat=heartbeat, reverse_tap=cfg["reverse_tap"])
    gov.start()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # runs the finally below

    app = tornado.web.Application(
        [(r"/ws", WS), (r"/video.mjpg", Video), (r"/sounds", Sounds), (r"/share", Share), (r"/health", Health),
         (r"/(.*)", Static, {"path": DASH, "default_filename": "index.html"})])
    app.gov, app.cam, app.token, app.settings, app.cal = gov, camera.Camera(source, cfg["camera"], settings), load_token(), settings, cfg["cal"]
    app.controller, app.clients, app.pad, app.rx, app.sent = None, set(), None, 0.0, 0
    app.view_token, app.mock = load_token(VIEW_FILE), args.mock
    app.modes = cfg["modes"]
    vcfg = dict(cfg["vision"], model=os.path.join(HERE, cfg["vision"]["model"]), labels=os.path.join(HERE, cfg["vision"]["labels"]))
    app.vision, app.follow_cfg = vision.Vision(app.cam, vcfg, args.mock), cfg["follow"]
    app.vision.set_calibration(fisheye.load(CALIB_FILE))
    app.vision.correct = settings["vision_correct"] == "on"
    app.straight = camera.Straightened(app.cam, lambda: app.vision.calib if app.vision.correct else None)
    app.calib_frames, app.calib_size, app.calib_auto = [], None, False
    app.vision_labels = set(l.strip() for l in open(vcfg["labels"])) - {"???"}
    set_mode(app, "normal")
    gov.set_power(settings["default_power"])  # the default max power still wins at start (and for each new driver)
    port = args.port or cfg["port"]
    last = [0]
    pushed = [time.monotonic()]
    batt = None if args.mock else telemetry.Battery(cfg["battery"])

    def push():  # 2 Hz: drop a silent driver, telemetry to every phone
        now = time.monotonic()
        perf.timing("server.lag", max(0.0, (now - pushed[0] - 0.5) * 1000))   # late = the event loop was busy
        pushed[0] = now
        fps, last[0] = (app.sent - last[0]) * 2 // max(1, app.cam.users), app.sent  # per viewer, i.e. what this phone gets
        c = app.controller
        if c and time.monotonic() - app.rx > 5:  # the car is already halted by the lease; this frees the seat
            log("driver went silent for 5 s: dropped")
            c.close() if c.kind == "phone" else drop(app)
        if app.clients:
            tm = telemetry.system(args.mock)
            if batt:
                tm["batt"] = batt.read()
            tm.update(fps=fps, sim=args.mock, batt_pct=telemetry.battery_percent(tm.get("batt"), cfg["battery"]),
                      pad=app.pad is not None, power=gov.power, trim=app.settings["steer_trim"], defpow=app.settings["default_power"],
                      cam={k[4:]: app.settings[k] for k in CAM_KEYS}, mode=app.mode, vision=app.vision.snapshot(),
                      calib={"rms": (app.vision.calib or {}).get("rms"), "captured": len(app.calib_frames), "needed": fisheye.NEEDED, "auto": app.calib_auto,
                             "on": app.settings["vision_correct"] == "on"},
                      drive=[round(v, 2) for v in gov.output])
            msg = json.dumps({"tm": tm})
            for client in list(app.clients):
                send(client, msg)

    def shutdown():
        log("PS button held: shutting the Pi down")
        drop(app)
        for client in list(app.clients):
            send(client, {"role": "shutdown"})
        if not args.mock:
            subprocess.Popen(["sudo", "-n", "/sbin/shutdown", "-h", "now"])

    async def serve():
        loop = tornado.ioloop.IOLoop.current()
        app.listen(port)
        tornado.ioloop.PeriodicCallback(push, 500).start()
        seq = itertools.count(1)

        def pad_action(pad, a):
            if a == "take":
                take(app, pad)
            elif a in ("mode+", "mode-") and app.controller is pad:
                i = MODE_ORDER.index(app.mode) + (1 if a == "mode+" else -1)
                set_mode(app, MODE_ORDER[max(0, min(len(MODE_ORDER) - 1, i))])
            elif a in ("power+", "power-"):
                gov.set_power(max(0.25, gov.power + (0.1 if a == "power+" else -0.1)))
            elif a == "shutdown":
                shutdown()

        def pad_tick(pad):  # gamepad thread, every 50 ms
            for a in pad.actions():
                loop.add_callback(pad_action, pad, a)
            if app.controller is pad:
                app.rx = time.monotonic()
                gov.command(*pad.command(), seq=next(seq))
            elif app.controller is None and pad.moved():
                loop.add_callback(take, app, pad)  # nobody driving: touching the controller takes the car

        def pad_lost(pad):
            def lost():
                log("PS3 controller disconnected")
                app.pad = None
                if app.controller is pad:
                    drop(app)  # Bluetooth dropped: neutral now (the lease would anyway within 250 ms)
            loop.add_callback(lost)

        if not args.mock:
            threading.Thread(target=gamepad.run, daemon=True,
                             args=(cfg["gamepad"], lambda pad: (loop.add_callback(setattr, app, "pad", pad), log("PS3 controller connected")), pad_tick, pad_lost)).start()
        log("car software started; open http://%s.local:%d/?t=%s" % (socket.gethostname().split(".")[0], port, app.token))
        sd_notify(b"READY=1")
        await asyncio.Event().wait()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    finally:
        gov.close()


if __name__ == "__main__":
    main()
