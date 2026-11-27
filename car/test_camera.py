"""Run: .venv/bin/python -m unittest car.test_camera"""
import unittest

from car.camera import libcamera_controls
from car.drivers import DEFAULTS


class T(unittest.TestCase):
    def test_dashboard_settings_map_onto_libcamera(self):
        self.assertEqual(libcamera_controls(DEFAULTS), {"Brightness": 0.0, "Contrast": 1.0, "Saturation": 1.0, "Sharpness": 1.0,
                                                        "AeExposureMode": "Normal", "AwbMode": "Auto"})   # defaults = libcamera's normal
        c = libcamera_controls(dict(DEFAULTS, cam_brightness=80, cam_contrast=-100, cam_saturation=100, cam_exposure="night", cam_awb="tungsten"))
        self.assertEqual((c["Brightness"], c["Contrast"], c["Saturation"], c["AeExposureMode"], c["AwbMode"]), (0.6, 0.0, 2.0, "Long", "Tungsten"))


if __name__ == "__main__":
    unittest.main()
