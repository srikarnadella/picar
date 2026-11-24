"""Plays car/sounds/*.wav through aplay, one at a time (new sound replaces the old)."""
import os
import shutil
import subprocess

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")
_proc = None


def names():
    return sorted(f[:-4] for f in os.listdir(DIR) if f.endswith(".wav"))


def play(name):
    global _proc
    if name not in names():  # also blocks path tricks like ../
        return False
    if _proc and _proc.poll() is None:
        _proc.kill()
    player = shutil.which("aplay") or shutil.which("afplay")  # afplay = Mac, for mock runs
    if not player:
        print("no audio player; would play", name)
        return True
    cmd = [player] + (["-q"] if player.endswith("aplay") else []) + [os.path.join(DIR, name + ".wav")]
    _proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True
