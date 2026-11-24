"""Run: .venv/bin/python -m unittest car.test_telemetry"""
import unittest

from car.telemetry import Battery, battery_percent

CFG = {"address": 72, "divider": 3.128, "empty": 6.6, "full": 8.4}


class FakeADS:
    def __init__(self, raw):
        self.raw, self.writes = raw, []

    def writeList(self, reg, data):
        self.writes.append((reg, list(data)))

    def readS16BE(self, reg):
        assert reg == 0x00
        return self.raw


class Dead:
    def writeList(self, reg, data):
        raise OSError(121, "Remote I/O error")


class T(unittest.TestCase):
    def test_first_read_starts_conversion_next_returns_volts(self):
        ads = FakeADS(20000)                         # 20000 * 4.096/32768 = 2.5 V at the pin
        b = Battery(CFG, dev=ads)
        self.assertIsNone(b.read())
        self.assertEqual(ads.writes[0], (0x01, [0xC3, 0x83]))
        self.assertAlmostEqual(b.read(), 2.5 * 3.128, places=3)

    def test_smoothing_and_negative_noise(self):
        ads = FakeADS(20000)
        b = Battery(CFG, dev=ads)
        b.read(); b.read()
        ads.raw = -5                                   # noise around 0 V never goes negative
        self.assertAlmostEqual(b.read(), 2.5 * 3.128 * 0.7, places=3)

    def test_missing_board_is_none_not_a_crash(self):
        b = Battery(CFG, dev=Dead())
        self.assertIsNone(b.read())
        self.assertIsNone(b.dev)                       # will look again in 10 s

    def test_percent(self):
        self.assertIsNone(battery_percent(None, CFG))
        self.assertEqual(battery_percent(8.4, CFG), 100)
        self.assertEqual(battery_percent(6.6, CFG), 0)
        self.assertAlmostEqual(battery_percent(7.5, CFG), 50)
        self.assertEqual(battery_percent(9.0, CFG), 100)


if __name__ == "__main__":
    unittest.main()
