import tempfile
import unittest
from pathlib import Path

from lpkf_config import (
    CALIBRATED_HOME_OFFSET_UNITS,
    HardClipLimits,
    LPKFIni,
    OperatingWindow,
    XYPosition,
)
from lpkf_units import M60_STEP_MM, mm_to_m60_steps


class LPKFConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "lpkf.ini"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_m60_step_width_matches_manual(self):
        self.assertEqual(M60_STEP_MM, 0.0079375)
        self.assertEqual(mm_to_m60_steps(30.0), 3780)

    def test_derived_positions_match_requested_coordinates(self):
        limits = HardClipLimits(100, 200, -50, 10100, 20200, 50)
        positions = limits.positions()

        self.assertEqual(positions["pause"], XYPosition(100, 200))
        self.assertEqual(positions["default_home"], XYPosition(100, 10100))
        self.assertEqual(
            positions["calibrated_home"],
            XYPosition(100 + CALIBRATED_HOME_OFFSET_UNITS, 10100),
        )
        self.assertEqual(positions["zero"], XYPosition(10100, 20200))
        self.assertEqual(CALIBRATED_HOME_OFFSET_UNITS, 3780)

    def test_oh_limits_and_positions_round_trip_through_ini(self):
        limits = HardClipLimits(0, 0, -100, 30000, 20000, 100)
        store = LPKFIni(self.path)

        store.save_limits(limits)

        self.assertEqual(store.load_limits(), limits)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("xmin = 0", text)
        self.assertIn("xmax = 30000", text)
        self.assertIn("calibrated_home_x = 3780", text)

    def test_iw_window_must_stay_inside_hardclip(self):
        limits = HardClipLimits(0, 0, -100, 30000, 20000, 100)
        OperatingWindow(100, 100, 1000, 1000).validate(limits)

        with self.assertRaisesRegex(ValueError, "inside the hardclip"):
            OperatingWindow(-1, 100, 1000, 1000).validate(limits)

    def test_iw_window_round_trip_preserves_hardclip_section(self):
        limits = HardClipLimits(0, 0, -100, 30000, 20000, 100)
        window = OperatingWindow(100, 200, 29000, 19000)
        store = LPKFIni(self.path)
        store.save_limits(limits)
        store.save_window(window)

        self.assertEqual(store.load_limits(), limits)
        self.assertEqual(store.load_window(), window)


if __name__ == "__main__":
    unittest.main()
