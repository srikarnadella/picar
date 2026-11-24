"""Performance monitor (Settings > Health and logs): how long each part takes and how often it runs.
Idea from donkeycar's perfmon/fps parts. Averages and worst cases over the last WINDOW seconds. Python 3.7."""
import collections
import resource
import threading
import time

WINDOW = 10.0
_lock = threading.Lock()
_times = {}    # name -> deque of (when, ms)
_events = {}   # name -> deque of when
_cpu = [time.monotonic(), 0.0]


def timing(name, ms):
    with _lock:
        _times.setdefault(name, collections.deque(maxlen=400)).append((time.monotonic(), ms))


def tick(name):
    with _lock:
        _events.setdefault(name, collections.deque(maxlen=3000)).append(time.monotonic())


class timer:
    """with perf.timer("vision.detect"): ..."""
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.t = time.monotonic()

    def __exit__(self, *exc):
        timing(self.name, (time.monotonic() - self.t) * 1000)


def _rss_mb():
    try:
        with open("/proc/self/status") as f:
            return round(int(next(l for l in f if l.startswith("VmRSS")).split()[1]) / 1024, 1)
    except (OSError, StopIteration):
        return None


def snapshot():
    now = time.monotonic()
    out = {}
    with _lock:
        for name, dq in _times.items():
            recent = [ms for t, ms in dq if now - t <= WINDOW]
            if recent:
                out[name] = {"avg": round(sum(recent) / len(recent), 1), "max": round(max(recent), 1)}
        for name, dq in _events.items():
            out[name] = {"rate": round(sum(1 for t in dq if now - t <= WINDOW) / WINDOW, 1)}
    ru = resource.getrusage(resource.RUSAGE_SELF)
    used = ru.ru_utime + ru.ru_stime
    pct = 100.0 * (used - _cpu[1]) / max(1e-3, now - _cpu[0]) if _cpu[1] else None
    _cpu[0], _cpu[1] = now, used
    out["process"] = {"cpu_pct": round(pct, 1) if pct is not None else None, "rss_mb": _rss_mb()}
    return out
