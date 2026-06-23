import tempfile
import unittest
from pathlib import Path

from lpkf_config import (
    CALIBRATED_HOME_OFFSET_UNITS,
    HardClipLimits,
    LPKFIni,
    OperatingWindow,
    PAUSE_LIMIT_MARGIN_UNITS,
    XYPosition,
    hardclip_from_oh_values,
)
from lpkf_units import (
    M60_STEP_MM,
    mm_to_m60_steps,
    round_half_away_from_zero,
)


class LPKFConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "lpkf.ini"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_m60_step_width_matches_manual(self):
        self.assertEqual(M60_STEP_MM, 0.0079375)
        self.assertEqual(mm_to_m60_steps(30.0), 3780)

    def test_machine_step_rounding_uses_nearest_with_symmetric_ties(self):
        self.assertEqual(round_half_away_from_zero(10.49), 10)
        self.assertEqual(round_half_away_from_zero(10.5), 11)
        self.assertEqual(round_half_away_from_zero(-10.5), -11)

    def test_odd_hardclip_midpoint_produces_whole_home_coordinates(self):
        positions = HardClipLimits(0, 0, 0, 68216, 48533, 0).positions()

        self.assertEqual(positions["default_home"], XYPosition(0, 24267))
        self.assertEqual(positions["calibrated_home"], XYPosition(3780, 24267))

    def test_legacy_fractional_home_is_normalized_when_loaded(self):
        self.path.write_text(
            "[positions]\ncalibrated_home_x = 3780\n"
            "calibrated_home_y = 24266.5\n",
            encoding="utf-8",
        )

        self.assertEqual(
            LPKFIni(self.path).load_position("calibrated_home"),
            XYPosition(3780, 24267),
        )

    def test_short_oh_response_uses_zero_minimums(self):
        self.assertEqual(
            hardclip_from_oh_values([68214, 48533, 0]),
            HardClipLimits(0, 0, 0, 68214, 48533, 0),
        )

    def test_full_oh_response_preserves_all_six_coordinates(self):
        self.assertEqual(
            hardclip_from_oh_values([10, 20, -5, 68214, 48533, 5]),
            HardClipLimits(10, 20, -5, 68214, 48533, 5),
        )

    def test_derived_positions_match_requested_coordinates(self):
        limits = HardClipLimits(100, 200, -50, 10100, 20200, 50)
        positions = limits.positions()

        self.assertEqual(
            positions["pause"],
            XYPosition(
                10100 - PAUSE_LIMIT_MARGIN_UNITS,
                20200 - PAUSE_LIMIT_MARGIN_UNITS,
            ),
        )
        self.assertEqual(positions["default_home"], XYPosition(100, 10100))
        self.assertEqual(
            positions["calibrated_home"],
            XYPosition(100 + CALIBRATED_HOME_OFFSET_UNITS, 10100),
        )
        self.assertEqual(positions["zero"], XYPosition(100, 200))
        self.assertEqual(CALIBRATED_HOME_OFFSET_UNITS, 3780)

    def test_pause_position_is_inset_from_both_maximum_limits(self):
        limits = HardClipLimits(0, 0, 0, 68216, 48533, 0)

        self.assertEqual(PAUSE_LIMIT_MARGIN_UNITS, 630)
        self.assertEqual(limits.positions()["pause"], XYPosition(67586, 47903))

    def test_pause_margin_can_be_configured_in_ini(self):
        self.path.write_text(
            "[positions]\npause_limit_margin_mm = 10\n",
            encoding="utf-8",
        )
        limits = HardClipLimits(0, 0, 0, 68216, 48533, 0)
        store = LPKFIni(self.path)

        store.save_limits(limits)

        self.assertEqual(store.load_pause_margin_units(), 1260)
        self.assertEqual(store.load_position("pause"), XYPosition(66956, 47273))

    def test_oh_limits_and_positions_round_trip_through_ini(self):
        limits = HardClipLimits(0, 0, -100, 30000, 20000, 100)
        store = LPKFIni(self.path)

        store.save_limits(limits)

        self.assertEqual(store.load_limits(), limits)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("xmin = 0", text)
        self.assertIn("xmax = 30000", text)
        self.assertIn("calibrated_home_x = 3780", text)
        self.assertIn("pause_limit_margin_mm = 5", text)
        self.assertEqual(
            store.load_position("calibrated_home"),
            limits.positions()["calibrated_home"],
        )

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
