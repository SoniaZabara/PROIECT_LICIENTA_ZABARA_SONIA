import unittest

from lpkf_controller import SerialWorker


class FakeSerial:
    is_open = True


class RecordingSerialWorker(SerialWorker):
    def __init__(self):
        super().__init__()
        self.ser = FakeSerial()
        self.events: list[tuple[str, str]] = []

    def _write_command(self, command: str) -> str:
        command = self._normalize_command(command)
        self.events.append(("write", command))
        return command

    def _send_and_wait_ack(self, command: str, timeout_s: float) -> str:
        command = self._normalize_command(command)
        self.events.append(("ack", command))
        return command

    def _read_lines_until(self, deadline: float) -> bool:
        return False


class SerialWorkerTests(unittest.TestCase):
    def test_concatenated_reset_and_ack_responses_are_split(self):
        worker = SerialWorker()
        received: list[str] = []
        logs: list[str] = []
        worker.line_received.connect(received.append)
        worker.log.connect(logs.append)

        acknowledged = worker._handle_received_line("ZC")

        self.assertTrue(acknowledged)
        self.assertEqual(received, ["Z"])
        self.assertEqual(logs, ["<< Z", "<< C"])

    def test_initialization_precedes_echo_enable(self):
        worker = RecordingSerialWorker()

        worker.stream_commands(
            ["IN", "!CM1", "PU"],
            wait_for_ack=True,
            iw_command="IW0,0,68216,48533",
        )

        self.assertEqual(
            worker.events,
            [
                ("write", "IN;"),
                ("ack", "!CT1;"),
                ("ack", "IW0,0,68216,48533;"),
                ("ack", "!CM1;"),
                ("ack", "PU;"),
                ("write", "!CT0;"),
            ],
        )

    def test_delay_streaming_also_restores_iw_after_initialization(self):
        worker = RecordingSerialWorker()

        worker.stream_commands(
            ["IN", "PU"],
            delay_s=0,
            wait_for_ack=False,
            iw_command="IW0,0,68216,48533",
        )

        self.assertEqual(
            worker.events,
            [
                ("write", "IN;"),
                ("write", "IW0,0,68216,48533;"),
                ("write", "PU;"),
            ],
        )

    def test_initialization_without_iw_is_rejected_before_reset(self):
        worker = RecordingSerialWorker()
        errors: list[str] = []
        worker.error.connect(errors.append)

        worker.stream_commands(["IN"], wait_for_ack=True)

        self.assertEqual(worker.events, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("no IW command", errors[0])

    def test_echo_is_rearmed_after_an_interior_initialization(self):
        worker = RecordingSerialWorker()

        worker.stream_commands(
            ["PU", "IN", "PD"],
            wait_for_ack=True,
            iw_command="IW0,0,68216,48533",
        )

        self.assertEqual(
            worker.events,
            [
                ("ack", "!CT1;"),
                ("ack", "PU;"),
                ("write", "IN;"),
                ("ack", "!CT1;"),
                ("ack", "IW0,0,68216,48533;"),
                ("ack", "PD;"),
                ("write", "!CT0;"),
            ],
        )

    def test_parameterless_plot_mode_is_rejected(self):
        worker = SerialWorker()
        worker.ser = FakeSerial()

        with self.assertRaisesRegex(ValueError, "PA requires"):
            worker._write_command("PA;PR1000,0;")


if __name__ == "__main__":
    unittest.main()
