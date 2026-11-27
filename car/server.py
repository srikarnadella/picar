"""python -m car.server [--mock]   Python 3.7 compatible.

Exactly one driver at a time (a phone or a game controller: PS3 or Xbox); every other phone is a watcher that still
gets video and telemetry and can take control. A new phone takes over from another phone (dead or reloaded
phones never lock you out) but not from a driving controller: you tap Take control (or Halt) for that.
"""
import argparse
import asyncio
import hmac
import itertools
import json
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
from car.drivers import clean, load_settings, save_settings
from car.governor import Governor
from car.log import events, log

CAM_KEYS = ("cam_brightness", "cam_exposure", "cam_flip", "cam_quality", "cam_saturation", "cam_contrast", "cam_sharpness", "cam_awb")

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.join(os.path.dirname(HERE), "dashboard")
TOKEN_FILE = os.path.join(HERE, ".token")          # full access: drive
VIEW_FILE = os.path.join(HERE, ".viewtoken")       # view-only link: watch, never drive
CALIB_FILE = os.environ.get("PICAR_CALIB") or os.path.join(HERE, "calib.json")   # fisheye calibration (made on the car; deploys never overwrite it)
STARTED = time.time()


def load_token(path=TOKEN_FILE, new=False):
    if new or not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_urlsafe(8 if path == VIEW_FILE else 6))
        os.chmod(path, 0o600)
    return open(path).read().strip()


def access(handler):
    """'full' for the driving key (?t=), 'view' for the view-only key (?v=), else None."""
    app = handler.application   # bytes: compare_digest raises on non-ASCII str, which a URL can carry
    if hmac.compare_digest(handler.get_argument("t", "").encode(), app.token.encode()):
        return "full"
    v = handler.get_argument("v", "").encode()
    return "view" if v and hmac.compare_digest(v, app.view_token.encode()) else None


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


def driver_label(app):
    """Who drives, for people: "Phone", "Laptop" (as each page reports itself), the controller's name, "Car (following)"."""
    c = app.controller
    if c is None:
        return None
    return {"gamepad": getattr(c, "name", "Controller"), "follow": "Car (following)"}.get(c.kind) or ("Laptop" if getattr(c, "dev", "") == "laptop" else "Phone")


def announce(app):
    by, who = (app.controller.kind if app.controller else None), driver_label(app)
    for c in list(app.clients):
        send(c, {"role": "driver" if c is app.controller else "viewer" if c.access == "view" else "watching", "by": by, "who": who})


def guard(app):
    """The obstacle brake watches the road only while someone (or the follower) drives: it costs the detector's CPU."""
    app.vision.guard = app.settings["obstacle_brake"] == "on" and app.controller is not None


def take(app, who):
    if app.controller is not who:
        app.controller, app.rx = who, time.monotonic()
        app.gov.release()  # neutral and a fresh sequence for the new driver
        app.gov.set_power(app.settings["default_power"])  # every new driver starts at the default max power (shown in the header)
        guard(app)
        log({"gamepad": "%s took control" % getattr(who, "name", "controller"), "follow": "following: %s" % getattr(who, "target", "")}.get(who.kind, "a phone took control"))
        announce(app)


MODE_ORDER = ("indoor", "normal", "sport")


def start_follow(app, target, box=None):
    """The car drives itself toward a target: a COCO label ("person") or a colour ("colour:red").
    It becomes the driver, so the phone watches; Halt or Take control on any phone (or Start/Menu on a controller) ends it."""
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
    ok, value = clean(key, value)
    if not ok:
        return
    app.settings[key] = value
    save_settings(app.settings)
    log("setting %s = %s" % (key, app.settings[key]))
    if key == "steer_trim":
        app.cal["steer_trim"] = app.settings[key]
        app.gov.rewrite()  # the wheels move to the new centre now, not at the next command
    if key in CAM_KEYS:
        app.cam.set_look(app.settings)
    if key == "vision_correct":
        app.vision.correct = value == "on"
    if key == "obstacle_brake":
        guard(app)
    if key == "default_power":
        app.gov.set_power(value)   # the header's max power follows at once, not only for the next driver


def solve_calibration(app, how):
    try:
        calib = fisheye.calibrate(list(app.calib_frames), app.calib_size)
    finally:
        app.calib_busy = False
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
    if app.calib_busy and action in ("capture", "finish", "restart", "auto"):   # a solve is running: let it finish
        return send(ws, {"calib": {"error": "Still working out the lens. One moment."}})
    if action == "capture":
        jpg = app.cam.frame
        if jpg is None:
            return send(ws, {"calib": {"error": "No camera picture yet. Is the video showing?"}})

        def capture():
            img = vision.decode_jpeg(jpg)
            if img is None:
                return {"error": "That picture was damaged. Try again."}
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

        app.calib_busy = True
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
                    if img is None:   # a damaged frame: wait for the next
                        time.sleep(0.1)
                        continue
                    size = (img.shape[1], img.shape[0])
                    if app.calib_size and size != app.calib_size:   # camera quality changed mid-way: start over
                        app.calib_frames = []
                    corners = fisheye.find_corners(img)
                    if corners is not None and fisheye.is_new_view(corners, app.calib_frames, size):
                        app.calib_frames.append(corners)
                        app.calib_size = size
                        loop.add_callback(send, ws, {"calib": {"found": True, "auto": True, "count": len(app.calib_frames), "needed": fisheye.NEEDED}})
                    elif corners is not None:
                        loop.add_callback(send, ws, {"calib": {"same": True, "count": len(app.calib_frames), "needed": fisheye.NEEDED}})
                    time.sleep(0.4)
                if app.calib_auto and len(app.calib_frames) >= fisheye.NEEDED:
                    app.calib_busy = True
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
    guard(app)
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
        self.dev = "laptop" if self.get_argument("dev", "") == "laptop" else "phone"   # what the page says it runs on (for "who drives")
        if self.access is None:
            return self.close(4403)
        app.clients.add(self)
        if self.access == "view":
            announce(app)  # a viewer only ever watches
        elif self.get_argument("join", "") == "never":
            announce(app)  # a hidden page: watch only, never become the driver on its own (Take control still works)
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
        elif m.get("power") == "off":   # Settings > Turn off the car (full access only; view-only never gets here)
            app.shutdown("turned off from a dashboard")
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
    gone = False

    def on_connection_close(self):   # noticed even while no frames come (camera down), so the camera is released
        self.gone = True

    async def get(self):
        app = self.application
        straight = self.get_argument("view", "") == "straight"   # the fisheye-straightened picture, if calibrated
        cam = app.straight if straight else app.cam
        self.set_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.set_header("Cache-Control", "no-store")
        cam.acquire()
        app.viewers += 1
        last = None
        try:
            while not self.gone and access(self) is not None:   # a replaced view-only key ends its streams too
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
            app.viewers -= 1
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
    async def get(self):
        app = self.application   # health() starts subprocesses (sudo is slow on a Pi 3B): never on the driving thread
        h = await tornado.ioloop.IOLoop.current().run_in_executor(None, telemetry.health) if not app.mock else telemetry.system(True)
        roles = [("driver" if c is app.controller else c.access) for c in app.clients]
        h.update(software_up_s=round(time.time() - STARTED), driver=app.controller.kind if app.controller else None, driver_label=driver_label(app),
                 watching=roles.count("full"), viewing=roles.count("view"), pad=app.pad.name if app.pad else None,
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

    beat, loop_alive = [0.0], [time.monotonic()]

    def heartbeat():  # fed by the governor thread, so a hung driver stops it and systemd restarts us;
        # a hung event loop (no 2 Hz push for 2 s) stops it too: the car would be neutral but unreachable
        if time.monotonic() - beat[0] > 0.5 and time.monotonic() - loop_alive[0] < 2.0:
            beat[0] = time.monotonic()
            sd_notify(b"WATCHDOG=1")

    gov = Governor(drv, cfg["limits"], cfg["lease_s"], heartbeat=heartbeat, reverse_tap=cfg["reverse_tap"])
    gov.start()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # runs the finally below

    app = tornado.web.Application(
        [(r"/ws", WS), (r"/video.mjpg", Video), (r"/sounds", Sounds), (r"/share", Share), (r"/health", Health),
         (r"/(.*)", Static, {"path": DASH, "default_filename": "index.html"})])
    app.gov, app.cam, app.token, app.settings, app.cal = gov, camera.Camera(source, cfg["camera"], settings), load_token(), settings, cfg["cal"]
    app.controller, app.clients, app.pad, app.rx, app.sent, app.viewers = None, set(), None, 0.0, 0, 0
    app.view_token, app.mock = load_token(VIEW_FILE), args.mock
    app.modes = cfg["modes"]
    vcfg = dict(cfg["vision"], model=os.path.join(HERE, cfg["vision"]["model"]), labels=os.path.join(HERE, cfg["vision"]["labels"]))
    app.vision, app.follow_cfg = vision.Vision(app.cam, vcfg, args.mock), cfg["follow"]
    app.vision.set_calibration(fisheye.load(CALIB_FILE))
    app.vision.correct = settings["vision_correct"] == "on"
    app.straight = camera.Straightened(app.cam, lambda: app.vision.calib if app.vision.correct else None)
    app.calib_frames, app.calib_size, app.calib_auto, app.calib_busy = [], None, False, False
    gov.forward_block = app.vision.obstacle   # the brake: forward throttle is refused while something is close ahead
    app.vision.forward_recent = lambda: time.monotonic() - gov.forward_at < 2.0   # the brake's detector runs only around forward driving
    app.vision_labels = set(l.strip() for l in open(vcfg["labels"])) - {"???"}
    set_mode(app, "normal")
    gov.set_power(settings["default_power"])  # the default max power wins at start (and for each new driver)
    port = args.port or cfg["port"]
    last = [0]
    pushed = [time.monotonic()]
    batt = None if args.mock else telemetry.Battery(cfg["battery"])

    def push():  # 2 Hz: drop a silent driver, telemetry to every phone
        now = time.monotonic()
        loop_alive[0] = now
        perf.timing("server.lag", max(0.0, (now - pushed[0] - 0.5) * 1000))   # late = the event loop was busy
        pushed[0] = now
        fps, last[0] = round((app.sent - last[0]) * 2 / max(1, app.viewers)), app.sent  # per video viewer, i.e. what this phone gets
        c = app.controller
        if c and time.monotonic() - app.rx > 5:  # the car is already halted by the lease; this frees the seat
            log("driver went silent for 5 s: dropped")
            drop(app)            # free the seat now: a dead phone's close can take another 5 s
            if c.kind == "phone":
                c.close()
        if app.clients:
            tm = telemetry.system(args.mock)
            if batt:
                tm["batt"] = batt.read()
            tm.update(fps=fps, sim=args.mock, batt_pct=telemetry.battery_percent(tm.get("batt"), cfg["battery"]),
                      pad=app.pad.name if app.pad else None, power=gov.power, trim=app.settings["steer_trim"], defpow=app.settings["default_power"],
                      cam={k[4:]: app.settings[k] for k in CAM_KEYS}, mode=app.mode, driver=driver_label(app), vision=app.vision.snapshot(),
                      calib={"rms": (app.vision.calib or {}).get("rms"), "captured": len(app.calib_frames), "needed": fisheye.NEEDED, "auto": app.calib_auto,
                             "on": app.settings["vision_correct"] == "on"},
                      drive=[round(v, 2) for v in gov.output], brake=gov.braking,
                      obstacle_brake=app.settings["obstacle_brake"])
            msg = json.dumps({"tm": tm})
            for client in list(app.clients):
                send(client, msg)

    def shutdown(why):
        """Turn the car off: neutral, tell every page, controllers off, then the Pi (its stop runs stop.py too)."""
        log("%s: turning the car off" % why)
        drop(app)
        for client in list(app.clients):
            send(client, {"role": "shutdown"})
        if not args.mock:
            def off():
                gamepad.release_controllers()
                subprocess.Popen(["sudo", "-n", "/sbin/shutdown", "-h", "now"])
            threading.Thread(target=off, daemon=True).start()
    app.shutdown = shutdown

    async def serve():
        loop = tornado.ioloop.IOLoop.current()
        app.listen(port)
        tornado.ioloop.PeriodicCallback(push, 500).start()
        seq = itertools.count(1)

        def pad_action(pad, a):
            if a == "take":
                if app.controller is not pad:
                    log("%s: take-control button pressed" % pad.name)
                take(app, pad)
            elif a in ("mode+", "mode-") and app.controller is pad:
                i = MODE_ORDER.index(app.mode) + (1 if a == "mode+" else -1)
                set_mode(app, MODE_ORDER[max(0, min(len(MODE_ORDER) - 1, i))])
            elif a in ("power+", "power-") and app.controller is pad:
                gov.set_power(max(0.1, round(gov.power + (0.1 if a == "power+" else -0.1), 2)))
            elif a == "shutdown":
                shutdown("%s home button held" % pad.name)

        def pad_tick(pad):  # gamepad thread, every 50 ms
            for a in pad.actions():
                loop.add_callback(pad_action, pad, a)
            if app.controller is pad:
                app.rx = time.monotonic()
                gov.command(*pad.command(), seq=next(seq))
            elif app.controller is None and pad.moved():   # nobody driving (still): touching the controller takes the car
                s, t = pad.command()
                loop.add_callback(lambda: app.controller is None and (log("%s moved (steer %+.2f, throttle %+.2f)" % (pad.name, s, t)), take(app, pad)))

        def pad_lost(pad):
            def lost():
                log("%s disconnected" % pad.name)
                app.pad = None
                if app.controller is pad:
                    drop(app)  # Bluetooth dropped: neutral now (the lease would anyway within 250 ms)
            loop.add_callback(lost)

        if not args.mock:
            gamepad.allow_controllers()   # stop.py blocked them when the car software last stopped
            threading.Thread(target=gamepad.run, daemon=True,
                             args=(cfg["gamepad"], lambda pad: (loop.add_callback(setattr, app, "pad", pad), log("%s connected (drives once its sticks and triggers read neutral)" % pad.name)),
                                   pad_tick, pad_lost, lambda name: log("ignoring controller %r: no known button layout, so it can never drive" % name))).start()
        log("car software started; open http://%s.local:%d/?t=%s" % (socket.gethostname().split(".")[0], port, app.token))
        loop_alive[0] = time.monotonic()
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
