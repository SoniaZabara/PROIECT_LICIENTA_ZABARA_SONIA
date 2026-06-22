import unittest

from translator.hpgl_postprocessor import HPGLPostProcessor
from translator.nist_interpreter import LinearMove, RapidMove, SetFeed


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
            ["VS0.002000", "VS0.001000"],
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


if __name__ == "__main__":
    unittest.main()
