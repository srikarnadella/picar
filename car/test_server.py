"""End-to-end checks against a real server in --mock mode (Mac; needs .venv with tornado + pillow).
Run: .venv/bin/python -m unittest car.test_server     (about 20 s: some checks wait out real timeouts)"""
import asyncio
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

import tempfile

import tornado.websocket

PORT = 8097
BASE = "127.0.0.1:%d" % PORT
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".token")


def get(path):
    try:
        return urllib.request.urlopen("http://%s%s" % (BASE, path), timeout=5)
    except urllib.error.HTTPError as e:
        return e


async def connect(tok):
    return await tornado.websocket.websocket_connect("ws://%s/ws?t=%s" % (BASE, tok))


async def until(ws, pred, timeout=2.0):
    """Read messages until pred(msg) is true. Returns the message, or None if the server closed us."""
    end = time.time() + timeout
    while time.time() < end:
        m = await asyncio.wait_for(ws.read_message(), end - time.time())
        if m is None or pred(json.loads(m)):
            return m and json.loads(m)
    raise AssertionError("timed out waiting")


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = os.path.join(tempfile.mkdtemp(), "settings.json")   # never touch the real car/settings.json
        cls.proc = subprocess.Popen([sys.executable, "-m", "car.server", "--mock", "--port", str(PORT)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=dict(os.environ, PICAR_SETTINGS=cls.settings,   # never the car's real settings or lens calibration
                                                                                     PICAR_CALIB=os.path.join(os.path.dirname(cls.settings), "calib.json")))
        for _ in range(50):
            try:
                urllib.request.urlopen("http://%s/" % BASE, timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        cls.tok = open(TOKEN_FILE).read().strip()

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        err = cls.proc.communicate(timeout=5)[1].decode()
        assert "Traceback" not in err, err  # the server must never have crashed a handler

    def test_page_is_never_cached_and_auth_is_required(self):
        r = get("/")
        self.assertEqual(r.status, 200)
        self.assertIn("no-cache", r.headers.get("Cache-Control", ""))
        self.assertIn(b"PICAR-01", r.read())
        self.assertEqual(get("/sounds").code, 403)
        self.assertEqual(json.loads(get("/sounds?t=" + self.tok).read()), {"sounds": ["beep"]})
        self.assertEqual(get("/video.mjpg?t=wrong").code, 403)

    def test_video_streams(self):
        r = get("/video.mjpg?t=" + self.tok)
        chunk = r.read(20000)
        r.close()
        self.assertIn(b"--frame", chunk)
        self.assertIn(b"image/jpeg", chunk)

    def test_straightened_view_streams(self):   # not calibrated here: it sends the normal picture
        r = get("/video.mjpg?view=straight&t=" + self.tok)
        chunk = r.read(20000); r.close()
        self.assertIn(b"image/jpeg", chunk)

    def test_bad_token_and_roles(self):
        async def go():
            bad = await connect("nope")
            self.assertIsNone(await bad.read_message())
            self.assertEqual(bad.close_code, 4403)
            self.assertEqual(get("/sounds?t=%C3%A9").code, 403)                # non-ASCII key: refused, not a crash
            a = await connect(self.tok)
            self.assertEqual(await until(a, lambda m: "role" in m), {"role": "driver", "by": "phone", "who": "Phone"})
            b = await connect(self.tok)                           # newest phone drives; the old one keeps watching
            self.assertEqual(await until(a, lambda m: "role" in m), {"role": "watching", "by": "phone", "who": "Phone"})
            self.assertEqual(await until(b, lambda m: "role" in m), {"role": "driver", "by": "phone", "who": "Phone"})
            a.write_message(json.dumps({"n": 1, "s": 0, "t": 0}))   # a watcher is still acked (honest link readout)
            self.assertEqual(await until(a, lambda m: "a" in m), {"a": 1})
            self.assertIn("tm", await until(a, lambda m: "tm" in m, 3))   # ...and still gets telemetry
            a.write_message('{"take":1}')                           # Take control / Halt from the watcher
            self.assertEqual(await until(a, lambda m: "role" in m), {"role": "driver", "by": "phone", "who": "Phone"})
            self.assertEqual(await until(b, lambda m: "role" in m), {"role": "watching", "by": "phone", "who": "Phone"})
            b.close(); a.close()
        asyncio.run(go())

    def test_car_settings_driver_only_validated_and_saved(self):
        async def go():
            a = await connect(self.tok)
            await until(a, lambda m: m.get("role") == "driver")
            b = await connect(self.tok)                           # b drives, a watches
            await until(a, lambda m: m.get("role") == "watching")
            a.write_message(json.dumps({"set": {"steer_trim": 0.2}}))      # a watcher may not
            b.write_message(json.dumps({"set": {"steer_trim": 0.9, "default_power": 0.4, "bogus": 1}}))
            b.write_message(json.dumps({"set": {"default_power": "fast"}}))
            tm = (await until(b, lambda m: "tm" in m and m["tm"].get("trim") == 0.3, 3))["tm"]
            self.assertEqual((tm["trim"], tm["defpow"]), (0.3, 0.4))      # clamped to +-0.3; junk ignored
            with open(self.settings) as f:
                saved = json.load(f)
            self.assertEqual((saved["steer_trim"], saved["default_power"]), (0.3, 0.4))
            a.write_message('{"take":1}')                         # a new driver starts at the default max power
            await until(a, lambda m: m.get("role") == "driver")
            self.assertIsNotNone(await until(a, lambda m: "tm" in m and m["tm"]["power"] == 0.4, 3))
            b.write_message(json.dumps({"set": {"steer_trim": 0, "default_power": 1}}))   # b is a watcher now: ignored
            a.write_message(json.dumps({"set": {"steer_trim": 0, "default_power": 1}}))
            await until(a, lambda m: "tm" in m and m["tm"]["trim"] == 0 and m["tm"]["defpow"] == 1, 3)
            a.close(); b.close()
        asyncio.run(go())

    def test_camera_settings_apply_live_and_quality_restarts(self):
        import io
        from PIL import Image

        def frame_size():
            r = get("/video.mjpg?t=" + self.tok)
            buf = b""
            while buf.count(b"--frame") < 3:                    # skip a frame or two from before the change
                buf += r.read(4096)
            r.close()
            jpg = buf.split(b"--frame")[2].split(b"\r\n\r\n", 1)[1]
            return Image.open(io.BytesIO(jpg)).size

        async def go():
            ws = await connect(self.tok)
            await until(ws, lambda m: m.get("role") == "driver")
            ws.write_message(json.dumps({"set": {"cam_quality": "high", "cam_brightness": 95, "cam_flip": "sideways", "cam_exposure": "night"}}))
            tm = (await until(ws, lambda m: "tm" in m and m["tm"]["cam"]["quality"] == "high", 3))["tm"]
            self.assertEqual({k: tm["cam"][k] for k in ("brightness", "exposure", "flip", "quality")},
                             {"brightness": 80, "exposure": "night", "flip": "none", "quality": "high"})   # clamped; bad flip ignored
            ws.write_message(json.dumps({"set": {"cam_saturation": 140, "cam_contrast": -20, "cam_awb": "tungsten", "cam_sharpness": "x"}}))
            tm = (await until(ws, lambda m: "tm" in m and m["tm"]["cam"]["awb"] == "tungsten", 3))["tm"]
            self.assertEqual((tm["cam"]["saturation"], tm["cam"]["contrast"], tm["cam"]["sharpness"]), (100, -20, 0))   # clamped; junk ignored
            await asyncio.sleep(0.5)
            self.assertEqual(frame_size(), (640, 480))          # quality restarted the camera at the new size
            ws.write_message(json.dumps({"set": {"cam_quality": "normal", "cam_brightness": 50, "cam_exposure": "auto", "cam_saturation": 0, "cam_contrast": 0, "cam_awb": "auto"}}))
            await until(ws, lambda m: "tm" in m and m["tm"]["cam"]["quality"] == "normal", 3)
            await asyncio.sleep(0.5)
            self.assertEqual(frame_size(), (480, 360))
            ws.close()
        asyncio.run(go())

    def test_default_max_power_applies_at_once(self):
        async def go():
            a = await connect(self.tok)
            await until(a, lambda m: m.get("role") == "driver")
            a.write_message('{"set":{"default_power":0.7}}')
            tm = (await until(a, lambda m: "tm" in m and m["tm"]["defpow"] == 0.7, 3))["tm"]
            self.assertEqual(tm["power"], 0.7)                               # the header's max power follows the default now
            a.write_message('{"set":{"default_power":1.0}}')
            await until(a, lambda m: "tm" in m and m["tm"]["power"] == 1.0, 3)
            a.close()
        asyncio.run(go())

    def test_driving_modes(self):
        async def go():
            a = await connect(self.tok)
            await until(a, lambda m: m.get("role") == "driver")
            tm = (await until(a, lambda m: "tm" in m, 3))["tm"]
            self.assertEqual(tm["mode"], "normal")
            a.write_message('{"mode":"indoor"}')
            tm = (await until(a, lambda m: "tm" in m and m["tm"]["mode"] == "indoor", 3))["tm"]
            self.assertEqual(tm["power"], 0.3)                               # the mode sets max power
            a.write_message('{"mode":"warp"}')                               # junk ignored
            b = await connect(self.tok)                                      # b drives now, a watches
            await until(a, lambda m: m.get("role") == "watching")
            a.write_message('{"mode":"sport"}')                              # a watcher can't
            tm = (await until(b, lambda m: "tm" in m, 3))["tm"]
            self.assertEqual((tm["mode"], tm["power"]), ("indoor", 1.0))     # mode kept; a new driver starts at the default max power
            b.write_message('{"mode":"normal"}')
            await until(b, lambda m: "tm" in m and m["tm"]["mode"] == "normal", 3)
            a.close(); b.close()
        asyncio.run(go())

    def test_follow_a_person_then_take_back(self):
        async def go():
            ph = await connect(self.tok)
            await until(ph, lambda m: m.get("role") == "driver")
            ph.write_message('{"follow":"unicorn"}')                       # not a known target: ignored
            ph.write_message('{"follow":"person"}')
            self.assertEqual(await until(ph, lambda m: "role" in m), {"role": "watching", "by": "follow", "who": "Car (following)"})
            tm = (await until(ph, lambda m: "tm" in m and m["tm"]["vision"]["objs"], 6))["tm"]
            self.assertEqual((tm["vision"]["follow"], tm["vision"]["objs"][0]["label"]), ("person", "person"))
            self.assertLessEqual(tm["power"], 0.4)                         # following is capped at 40% power
            self.assertIsNotNone(tm["vision"]["objs"][0]["dist"])            # rough distance for a person
            ph.write_message('{"take":1}')                                   # Halt / Take control ends following
            self.assertEqual(await until(ph, lambda m: "role" in m), {"role": "driver", "by": "phone", "who": "Phone"})
            await until(ph, lambda m: "tm" in m and m["tm"]["vision"]["follow"] is None, 3)
            ph.close()
        asyncio.run(go())

    def test_tap_a_box_to_follow_that_one(self):
        async def go():
            ph = await connect(self.tok)
            await until(ph, lambda m: m.get("role") == "driver")
            ph.write_message(json.dumps({"follow": "person", "box": [0.9, 0.1, 0.2, 0.5]}))   # nonsense box: ignored
            ph.write_message(json.dumps({"follow": "person", "box": [0.78, 0.35, 0.92, 0.75]}))  # the one on the right
            self.assertEqual(await until(ph, lambda m: "role" in m), {"role": "watching", "by": "follow", "who": "Car (following)"})
            tm = (await until(ph, lambda m: "tm" in m and len(m["tm"]["vision"]["objs"]) == 2, 6))["tm"]
            lock = (await until(ph, lambda m: "tm" in m and m["tm"]["vision"]["lock"], 3))["tm"]["vision"]["lock"]
            self.assertGreater((lock[0] + lock[2]) / 2, 0.7)                 # locked on the right-hand person...
            for _ in range(3):                                               # ...and stays there while the other moves
                tm = (await until(ph, lambda m: "tm" in m, 3))["tm"]
                self.assertGreater((tm["vision"]["lock"][0] + tm["vision"]["lock"][2]) / 2, 0.7)
            self.assertGreater(tm["drive"][0], 0)                            # steering right, toward it
            ph.write_message('{"take":1}')
            await until(ph, lambda m: "tm" in m and m["tm"]["vision"]["lock"] is None, 3)
            ph.close()
        asyncio.run(go())

    def test_everyone_sees_who_drives(self):
        async def go():
            lap = await tornado.websocket.websocket_connect("ws://%s/ws?t=%s&dev=laptop" % (BASE, self.tok))
            self.assertEqual(await until(lap, lambda m: "role" in m), {"role": "driver", "by": "phone", "who": "Laptop"})
            ph = await connect(self.tok)                                       # a phone takes over
            self.assertEqual(await until(lap, lambda m: "role" in m), {"role": "watching", "by": "phone", "who": "Phone"})
            self.assertEqual((await until(lap, lambda m: "tm" in m, 3))["tm"]["driver"], "Phone")   # telemetry too, for late joiners
            self.assertEqual(json.loads(get("/health?t=" + self.tok).read())["health"]["driver_label"], "Phone")
            ph.close(); lap.close()
        asyncio.run(go())

    def test_turn_off_from_dashboard_full_access_only(self):
        async def go():
            view = json.loads(get("/share?t=" + self.tok).read())["view"]
            vw = await tornado.websocket.websocket_connect("ws://%s/ws?v=%s" % (BASE, view))
            await until(vw, lambda m: "role" in m)
            vw.write_message('{"power":"off"}')                                 # view-only: ignored
            vw.write_message(json.dumps({"n": 1, "s": 0, "t": 0}))
            self.assertEqual(await until(vw, lambda m: "a" in m or m.get("role") == "shutdown"), {"a": 1})
            d = await connect(self.tok)
            await until(d, lambda m: m.get("role") == "driver")
            d.write_message('{"power":"off"}')                                  # (mock: the Pi is not really shut down)
            self.assertEqual((await until(d, lambda m: "role" in m and m["role"] == "shutdown"))["role"], "shutdown")
            self.assertEqual((await until(vw, lambda m: "role" in m and m["role"] == "shutdown"))["role"], "shutdown")
            self.assertTrue(any("turned off from a dashboard" in msg for _, msg in json.loads(get("/health?t=" + self.tok).read())["events"]))
            d.close(); vw.close()
        asyncio.run(go())

    def test_hidden_page_never_takes_the_car(self):
        async def go():
            h = await tornado.websocket.websocket_connect("ws://%s/ws?t=%s&join=never" % (BASE, self.tok))
            self.assertEqual(await until(h, lambda m: "role" in m), {"role": "watching", "by": None, "who": None})   # nobody drives, still only watches
            h.write_message('{"take":1}')                                                             # an explicit Take control works
            self.assertEqual(await until(h, lambda m: "role" in m), {"role": "driver", "by": "phone", "who": "Phone"})
            h.close()
        asyncio.run(go())

    def test_app_joins_as_watcher_when_someone_drives(self):
        async def go():
            ph = await connect(self.tok)
            await until(ph, lambda m: m.get("role") == "driver")
            app = await tornado.websocket.websocket_connect("ws://%s/ws?t=%s&join=watch" % (BASE, self.tok))
            self.assertEqual(await until(app, lambda m: "role" in m), {"role": "watching", "by": "phone", "who": "Phone"})
            ph.close()                                                  # nobody drives now...
            app.close()
            lone = await tornado.websocket.websocket_connect("ws://%s/ws?t=%s&join=watch" % (BASE, self.tok))
            self.assertEqual((await until(lone, lambda m: "role" in m))["role"], "driver")   # ...so it may drive
            lone.close()
        asyncio.run(go())

    def test_view_only_link(self):
        async def go():
            self.assertEqual(get("/share?v=x").code, 403)
            view = json.loads(get("/share?t=" + self.tok).read())["view"]
            drv = await connect(self.tok)
            await until(drv, lambda m: m.get("role") == "driver")
            vw = await tornado.websocket.websocket_connect("ws://%s/ws?v=%s" % (BASE, view))
            self.assertEqual(await until(vw, lambda m: "role" in m), {"role": "viewer", "by": "phone", "who": "Phone"})
            for junk in ('{"take":1}', '{"p":0.3}', '{"set":{"steer_trim":0.2}}', '{"sound":"beep"}'):
                vw.write_message(junk)
            vw.write_message(json.dumps({"n": 1, "s": 1, "t": 1}))
            self.assertEqual(await until(vw, lambda m: "a" in m), {"a": 1})   # acked for its latency readout...
            self.assertIn("tm", await until(vw, lambda m: "tm" in m, 3))      # ...and gets telemetry...
            tm = (await until(drv, lambda m: "tm" in m, 3))["tm"]
            self.assertEqual((tm["trim"], tm["power"]), (0.0, 1.0))           # ...but changed nothing
            r = get("/video.mjpg?v=" + view); self.assertIn(b"--frame", r.read(20000)); r.close()
            self.assertEqual(get("/sounds?v=" + view).code, 403)
            self.assertEqual(get("/health?v=" + view).code, 403)
            stream = get("/video.mjpg?v=" + view); stream.read(1000)          # a viewer watching video on the old key
            req = urllib.request.Request("http://%s/share?t=%s" % (BASE, self.tok), data=b"", method="POST")
            new = json.loads(urllib.request.urlopen(req).read())["view"]       # New link: old viewers are cut off
            self.assertNotEqual(new, view)
            self.assertIsNone(await until(vw, lambda m: False, 3))
            end = time.time() + 5                                              # ...and so is their video
            while time.time() < end and stream.read(65536):
                pass
            self.assertLess(time.time(), end, "old view-only video kept streaming")
            stream.close()
            self.assertEqual(get("/video.mjpg?v=" + view).code, 403)
            drv.close()
        asyncio.run(go())

    def test_lens_calibration_messages(self):
        async def go():
            ph = await connect(self.tok)
            await until(ph, lambda m: m.get("role") == "driver")
            r = get("/video.mjpg?t=" + self.tok); r.read(20000)              # someone watching, so the camera runs
            ph.write_message('{"calib":"capture"}')                          # the mock camera has no board in view
            self.assertEqual((await until(ph, lambda m: "calib" in m, 5))["calib"], {"found": False, "count": 0, "needed": 12})
            ph.write_message('{"calib":"finish"}')
            self.assertIn("at least 6", (await until(ph, lambda m: "calib" in m, 3))["calib"]["error"])
            tm = (await until(ph, lambda m: "tm" in m, 3))["tm"]
            self.assertEqual((tm["calib"]["rms"], tm["calib"]["on"]), (None, True))
            ph.write_message('{"calib":"auto"}')                              # hands-free: no board in view, nothing captured
            self.assertTrue((await until(ph, lambda m: "tm" in m and m["tm"]["calib"]["auto"], 3))["tm"]["calib"]["auto"])
            ph.write_message('{"calib":"stop"}')
            self.assertTrue((await until(ph, lambda m: "calib" in m and "auto_off" in m["calib"], 5))["calib"]["auto_off"])
            r.close(); ph.close()
        asyncio.run(go())

    def test_health_and_events(self):
        self.assertEqual(get("/health").code, 403)
        d = json.loads(get("/health?t=" + self.tok).read())
        self.assertIn("process", d["health"]["perf"])                       # performance monitor
        for key in ("software_up_s", "driver", "watching", "viewing", "camera_on", "outages", "trim", "defpow"):
            self.assertIn(key, d["health"])
        self.assertTrue(any("car software started" in msg for _, msg in d["events"]), d["events"])

    def test_malformed_messages_never_drop_control(self):
        async def go():
            ws = await connect(self.tok)
            for i, junk in enumerate(['[1,2]', '42', '"x"', '{"n":"x"}', '{"n":null}', '{"n":true}', '{"p":"fast"}', '{"sound":["x"]}', 'not json'], 1):
                ws.write_message(junk)
                ws.write_message(json.dumps({"n": i, "s": 0, "t": 0}))
                self.assertEqual(await until(ws, lambda m: "a" in m), {"a": i}, junk)
        asyncio.run(go())

    def test_long_session_holds_then_silent_phone_is_dropped(self):
        async def go():
            ws = await connect(self.tok)
            ws.write_message(json.dumps({"p": 0.5}))
            t0, i, tms = time.time(), 0, 0
            while time.time() - t0 < 7:                           # past the old 5.0 s drop
                i += 1
                ws.write_message(json.dumps({"n": i, "s": 0, "t": 1}))
                m = await until(ws, lambda m: "a" in m or "tm" in m)
                self.assertIsNotNone(m, "dropped after %.1f s" % (time.time() - t0))
                tms += "tm" in m
                await asyncio.sleep(0.05)
            self.assertGreaterEqual(tms, 5)
            ws.write_message(json.dumps({"n": i + 1, "s": 0, "t": 0}))
            tm = (await until(ws, lambda m: "tm" in m))["tm"]   # mock battery is simulated: volts and percent present
            self.assertTrue(6 < tm["batt"] < 8.5 and 0 <= tm["batt_pct"] <= 100, tm)
            t1 = time.time()                                      # go silent: server drops us after ~5 s
            self.assertIsNone(await until(ws, lambda m: False, 8))
            self.assertAlmostEqual(time.time() - t1, 5, delta=1)
        asyncio.run(go())


class Settings(unittest.TestCase):
    def test_hand_edited_nonsense_falls_back_to_defaults(self):   # stop.py reads this too: it must never crash on a bad file
        from car import drivers
        path = os.path.join(tempfile.mkdtemp(), "settings.json")
        with open(path, "w") as f:
            json.dump({"steer_trim": "x", "default_power": 9, "cam_exposure": "disco", "cam_brightness": 55.4, "obstacle_brake": "off"}, f)
        old, drivers.SETTINGS = drivers.SETTINGS, path
        try:
            s = drivers.load_settings()
            with open(path, "w") as f:
                f.write("[1, 2]")                                     # valid JSON, wrong shape
            self.assertEqual(drivers.load_settings(), drivers.DEFAULTS)
        finally:
            drivers.SETTINGS = old
        self.assertEqual((s["steer_trim"], s["default_power"], s["cam_exposure"], s["cam_brightness"], s["obstacle_brake"]),
                         (0.0, 1.0, "auto", 55, "off"))   # bad -> default, out of range -> clamped, good -> kept


if __name__ == "__main__":
    unittest.main()
