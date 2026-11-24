"""Recent car events for the dashboard (Settings > Health and logs). Also printed, so they reach the systemd journal."""
import collections
import time

events = collections.deque(maxlen=200)


def log(*parts):
    msg = " ".join(str(p) for p in parts)
    events.append((round(time.time(), 1), msg))
    print(msg, flush=True)
