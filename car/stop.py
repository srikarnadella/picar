"""Writes neutral to the PCA9685 and exits. Run by systemd after the server stops or crashes
(the chip keeps its last pulse when our process dies). Also usable by hand: python -m car.stop"""
import json
import os

from car.drivers import Pca9685, load_settings

cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")))
cfg["cal"]["steer_trim"] = load_settings()["steer_trim"]
Pca9685(cfg["cal"])  # constructor writes neutral (wheels straight, trim included)
print("neutral written")
