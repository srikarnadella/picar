# Pi Car Command Center: design (current state)

A phone-controlled assistant-on-wheels. The Pi serves a web dashboard: drive, live video, sounds.
Works with no internet and no AI. Updated 2026-11-23 to match the code.

## Hardware (verified on the car)

- Raspberry Pi 3B Rev 1.2, Raspbian 10 (Buster, end of life), Python 3.7.3. Pi on a USB power bank,
  motors on the RC battery.
- PCA9685 at I2C `0x40`, 60 Hz. Steering servo on channel 1, ESC on channel 0.
- Camera: OV5647 (Pi Camera v1), wide-angle fisheye, legacy `picamera` 1.13.
- All Python packages are at the newest version that supports Python 3.7.

## Code

```
car/governor.py   the ONLY code that moves the car: lease failsafe, limits, max power, ESC reverse tap
car/drivers.py    pulse math (calibration + dead zone), Pca9685 driver (detects chip resets), Mock
car/server.py     tornado: dashboard, /ws control, /video.mjpg, /sounds, telemetry, systemd watchdog
car/camera.py     one shared capture, runs only while watched, restarts itself on failure
car/camera.py     (also) Straightened: the fisheye-corrected video stream, made once for every viewer who chose it
car/vision.py     detection (TFLite SSD-MobileNet), colour tracking, follower (stops at once when the target is lost)
car/fisheye.py    lens calibration (checkerboard, hands-free) and straightening; car/calib.json is made on the car
car/perf.py       performance monitor: timings and rates per part, last 10 s (Settings > Health)
car/gamepad.py    PS3 controller (Linux joystick API)
car/telemetry.py  CPU temp/load, Wi-Fi dBm, supply flags, battery (ADS1115), health (sim values with --mock)
car/sounds.py     car/sounds/*.wav through aplay
car/stop.py       writes neutral; systemd runs it after every stop or crash
car/selftest.py   wheels-off check: left, right, forward, backward (refuses while the service runs)
car/config.json   calibration, limits, reverse tap, driving modes, battery, vision, follow, camera
dashboard/        single page + self-hosted fonts + board.png (the checkerboard to print)
app/              Picar for Mac: the dashboard plus a terminal with one-click car commands (app/build_app.sh)
deploy/           sync.sh (shows changes, asks, syncs, restarts), picar.service
```

## Safety model

1. **Lease.** A command is valid for 250 ms. No fresh command: neutral. The page sends 20 per second.
2. **Connection.** Closing, or a new phone taking over, means neutral at once. A phone silent for 5 s is
   dropped (tornado's own ping is not used: it closed every session at 5 s).
3. **Driver health.** Outputs are re-written every second. A chip that reset (brownout) is detected and
   re-initialised. If writes keep failing for over 1 s the systemd watchdog is no longer fed, so systemd
   restarts the service, and `stop.py` writes neutral.
4. **Process death.** The PCA9685 keeps its last pulse, so `ExecStopPost` always writes neutral.
5. **Limits are car-side.** `limits` in config are the hard cap. The dashboard's max-power slider
   (25-100%) scales throttle inside the governor and can never exceed them.
6. **Known gap.** If the Pi loses power the ESC's own signal-loss behaviour decides. Verify before floor
   driving.

## Calibration (config.json, `cal`)

| Value | Meaning |
|---|---|
| steer_left 500 / steer_right 250 | full-lock pulses; swap them if steering is reversed |
| throttle_stop 300 | neutral; tune if the wheels creep |
| throttle_fwd 500 / throttle_rev 210 | full forward / reverse |
| throttle_min 0.2 | ESC dead zone: any non-zero throttle starts here (wheels needed about 0.3) |
| reverse_tap 0.19 for 0.3 s, then 0.5 s neutral | arms reverse on this ESC (268 ticks; tested) |

## Measured

- Server idle 0.7% CPU, 21 MB. Video 480x360, quality 30, 12 fps, about 5 Mbps (was 14 Mbps, which
  saturated the Pi 3B's Wi-Fi and pushed command latency past 300 ms).
- I2C "Remote I/O" errors appeared near 390-400 ticks forward (recovered). The Pi logged an undervoltage
  at boot while its power bank was charging. Fix the Pi's supply before floor driving.

## Open

- Reverse arming never times out. Test on the car: reverse, neutral 3 s, reverse. If the second reverse
  only brakes, add a timeout in `Governor._esc`.
- Hotspot mode, remote access (Tailscale), speaker (I2S amp), battery sensor wiring: not built yet.
- Following has not been re-tested since the ramming fix (power cap 0.4, stop when lost). Test with the
  wheels off first.

## Tests

`.venv/bin/python -m unittest car/test_*.py app/test_*.py` (about 30 s; the server tests start a mock
server), or the Run tests button in Picar. On the Pi: `python -m car.selftest` with the wheels off the ground.
