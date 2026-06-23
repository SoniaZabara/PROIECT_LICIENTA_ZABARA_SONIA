import unittest

from translator.hpgl_postprocessor import HPGLPostProcessor
from translator.nist_interpreter import ArcMove, LinearMove, RapidMove, SetFeed


class HPGLPostProcessorTests(unittest.TestCase):
    def commands(self, ir):
        output = HPGLPostProcessor().translate(ir)
        return [command.strip() for command in output.split(";") if command.strip()]

    def test_z_changes_become_single_tool_state_transitions(self):
        commands = self.commands(
            [
                SetFeed(120.0),
                RapidMove(0.0, 0.0, 2.0),
                RapidMove(2.2163, 15.3067, 2.0),
                SetFeed(60.0),
                LinearMove(2.2163, 15.3067, -2.4, 60.0),
                SetFeed(120.0),
                LinearMove(2.5014, 15.3771, -2.4, 120.0),
                LinearMove(3.1993, 15.4116, -2.4, 120.0),
                RapidMove(3.1993, 15.4116, 2.0),
                RapidMove(0.0, 0.0, 2.0),
            ]
        )

        self.assertEqual(commands.count("PD"), 1)
        self.assertEqual(commands.count("PU"), 2)  # initialization and retract
        self.assertEqual(
            [command for command in commands if command.startswith("PA")],
            ["PA279,1928", "PA315,1937", "PA403,1942", "PA0,0"],
        )

    def test_feed_is_only_emitted_when_it_changes(self):
        commands = self.commands(
            [
                SetFeed(120.0),
                SetFeed(120.0),
                LinearMove(0.0, 0.0, -1.0, 120.0),
                SetFeed(60.0),
                LinearMove(0.0, 0.0, -2.0, 60.0),
            ]
        )

        self.assertEqual(
            [command for command in commands if command.startswith("VS")],
            ["VS2000", "VS1000"],
        )

    def test_rapid_move_below_surface_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Rapid move requested below"):
            HPGLPostProcessor().translate([RapidMove(0.0, 0.0, -1.0)])

    def test_ramped_linear_move_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "cannot represent a ramped move"):
            HPGLPostProcessor().translate(
                [LinearMove(1.0, 1.0, -1.0, 120.0)]
            )

    def test_postprocessor_instance_can_be_reused(self):
        post = HPGLPostProcessor()
        moves = [RapidMove(0.0, 0.0, 1.0), RapidMove(1.0, 1.0, 1.0)]
        first = post.translate(moves)
        second = post.translate(moves)

        self.assertEqual(first, second)

    def test_work_origin_offsets_absolute_moves_in_machine_steps(self):
        output = HPGLPostProcessor(work_origin_steps=(3780, 24000)).translate(
            [
                RapidMove(0.0, 0.0, 2.0),
                RapidMove(10.0, 5.0, 2.0),
            ]
        )
        commands = [command.strip() for command in output.split(";") if command.strip()]

        self.assertEqual(
            [command for command in commands if command.startswith("PA")],
            ["PA3780,24000", "PA5040,24630"],
        )

    def test_missing_work_origin_preserves_machine_zero_coordinates(self):
        output = HPGLPostProcessor(work_origin_steps=None).translate(
            [
                RapidMove(0.0, 0.0, 2.0),
                RapidMove(10.0, 5.0, 2.0),
            ]
        )
        commands = [command.strip() for command in output.split(";") if command.strip()]

        self.assertEqual(
            [command for command in commands if command.startswith("PA")],
            ["PA1260,630"],
        )

    def test_work_origin_is_applied_to_arc_center(self):
        arc = ArcMove(
            clockwise=False,
            plane="XY",
            x=20.0,
            y=10.0,
            z=2.0,
            center_x=10.0,
            center_y=10.0,
            center_z=2.0,
            rotation=1,
            feed=120.0,
        )
        output = HPGLPostProcessor(work_origin_steps=(3780, 24000)).translate(
            [RapidMove(0.0, 0.0, 2.0), RapidMove(10.0, 0.0, 2.0), arc]
        )
        commands = [command.strip() for command in output.split(";") if command.strip()]

        self.assertIn("AA5040,25260,90", commands)

    def test_generated_hpgl_numeric_parameters_are_whole_numbers(self):
        arc = ArcMove(
            clockwise=True,
            plane="XY",
            x=4.0,
            y=3.0,
            z=-1.0,
            center_x=2.0,
            center_y=2.0,
            center_z=-1.0,
            rotation=1,
            feed=20.0,
        )

        output = HPGLPostProcessor().translate(
            [
                SetFeed(20.0),
                RapidMove(0.0, 0.0, 1.0),
                RapidMove(1.0, 1.0, 1.0),
                LinearMove(1.0, 1.0, -1.0, 20.0),
                arc,
            ]
        )

        self.assertNotRegex(output, r"\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
