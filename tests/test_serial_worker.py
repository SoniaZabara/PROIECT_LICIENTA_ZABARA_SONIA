import unittest
import time

from lpkf_controller import SerialWorker, SpindleStatusError


class FakeSerial:
    is_open = True
    cts = True
    rts = True
    out_waiting = 0
    in_waiting = 0

    def __init__(self):
        self.writes: list[str] = []

    def write(self, data: bytes) -> int:
        self.writes.append(data.decode("ascii"))
        return len(data)

    def read(self, count: int) -> bytes:
        return b""


class BlockedCtsSerial(FakeSerial):
    cts = False


class BufferedSerial(FakeSerial):
    def __init__(self, data: bytes):
        super().__init__()
        self._buffer = bytearray(data)

    @property
    def in_waiting(self) -> int:
        return len(self._buffer)

    def read(self, count: int) -> bytes:
        chunk = self._buffer[:count]
        del self._buffer[:count]
        return bytes(chunk)


class RecordingSerialWorker(SerialWorker):
    def __init__(self):
        super().__init__()
        self.ser = FakeSerial()
        self.events: list[tuple[str, str]] = []

    def _write_command(self, command: str) -> str:
        command = self._normalize_command(command)
        self.events.append(("write", command))
        return command

    def _send_and_wait_ack(
        self,
        command: str,
        ack_timeout_s: float = SerialWorker.ACK_TIMEOUT_S,
        cts_timeout_s: float = SerialWorker.CTS_TIMEOUT_S,
    ) -> str:
        command = self._normalize_command(command)
        self.events.append(("ack", command))
        return command

    def _send_rm_status_command(
        self,
        command: str,
        requested_thousands: int,
        timeout_s: float = SerialWorker.ACK_TIMEOUT_S,
    ) -> str:
        command = self._normalize_command(command)
        self.events.append(("rm", command))
        return command

    def _read_lines_until(self, deadline: float) -> bool:
        return False


class InterruptedSerialWorker(RecordingSerialWorker):
    def _write_command(self, command: str) -> str:
        raise RuntimeError("Streaming interrupted by user.")


class FailingSpindleWorker(RecordingSerialWorker):
    def _send_rm_status_command(
        self,
        command: str,
        requested_thousands: int,
        timeout_s: float = SerialWorker.ACK_TIMEOUT_S,
    ) -> str:
        command = self._normalize_command(command)
        self.events.append(("rm", command))
        if requested_thousands != 0:
            raise SpindleStatusError(
                f"Spindle did not reach requested speed for {command}; "
                "machine returned status 2 after 0."
            )
        return command


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

    def test_read_available_does_not_wait_when_idle(self):
        worker = SerialWorker()
        worker.ser = FakeSerial()

        start = time.perf_counter()
        worker._read_available()
        elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 0.1)

    def test_read_available_drains_waiting_bytes_without_delay(self):
        worker = SerialWorker()
        worker.ser = BufferedSerial(b"P 1,2,3\r")
        received: list[str] = []
        worker.line_received.connect(received.append)

        worker._read_available()

        self.assertEqual(received, ["P 1,2,3"])

    def test_initialization_precedes_echo_enable(self):
        worker = RecordingSerialWorker()

        worker.stream_commands(
            ["IN", "!CM1", "PU"],
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

    def test_streaming_can_use_echo_acknowledgement(self):
        worker = RecordingSerialWorker()

        worker.stream_commands(
            ["IN", "PU"],
            use_echo_ack=True,
            iw_command="IW0,0,68216,48533",
        )

        self.assertEqual(
            worker.events,
            [
                ("write", "IN;"),
                ("ack", "!CT1;"),
                ("ack", "IW0,0,68216,48533;"),
                ("ack", "PU;"),
                ("write", "!CT0;"),
            ],
        )

    def test_streaming_can_disable_echo_without_using_delay_pacing(self):
        worker = RecordingSerialWorker()

        worker.stream_commands(
            ["IN", "PU"],
            use_echo_ack=False,
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

    def test_echo_policy_skips_c_ack_for_open_channel(self):
        worker = RecordingSerialWorker()

        worker.stream_commands(["!OC", "!CC"], use_echo_ack=True)

        self.assertEqual(
            worker.events,
            [
                ("ack", "!CT1;"),
                ("write", "!OC;"),
                ("ack", "!CC;"),
                ("write", "!CT0;"),
            ],
        )

    def test_echo_policy_waits_for_rm_status_instead_of_c_ack(self):
        worker = RecordingSerialWorker()

        worker.stream_commands(["!OC", "!RM30", "!CC"], use_echo_ack=True)

        self.assertEqual(
            worker.events,
            [
                ("ack", "!CT1;"),
                ("write", "!OC;"),
                ("rm", "!RM30;"),
                ("ack", "!CC;"),
                ("write", "!CT0;"),
            ],
        )

    def test_spindle_status_failure_stops_stream_and_spindle(self):
        worker = FailingSpindleWorker()
        errors: list[str] = []
        finished: list[bool] = []
        worker.error.connect(errors.append)
        worker.streaming_finished.connect(lambda: finished.append(True))

        worker.stream_commands(
            ["!OC", "!RM30", "!CC", "PA100,100"],
            use_echo_ack=True,
        )

        self.assertEqual(
            worker.events,
            [
                ("ack", "!CT1;"),
                ("write", "!OC;"),
                ("rm", "!RM30;"),
                ("ack", "!CC;"),
                ("write", "!OC;"),
                ("rm", "!RM0;"),
                ("ack", "!CC;"),
            ],
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("Spindle did not reach requested speed", errors[0])
        self.assertEqual(finished, [])
        self.assertFalse(worker._streaming)
        self.assertFalse(worker._stop_streaming)

    def test_stop_interrupt_during_stream_emits_stopped_not_error(self):
        worker = InterruptedSerialWorker()
        stopped: list[bool] = []
        errors: list[str] = []
        worker.streaming_stopped.connect(lambda: stopped.append(True))
        worker.error.connect(errors.append)

        worker.stream_commands(["PU"], use_echo_ack=False)

        self.assertEqual(stopped, [True])
        self.assertEqual(errors, [])
        self.assertFalse(worker._streaming)
        self.assertFalse(worker._stop_streaming)

    def test_initialization_without_iw_is_rejected_before_reset(self):
        worker = RecordingSerialWorker()
        errors: list[str] = []
        worker.error.connect(errors.append)

        worker.stream_commands(["IN"])

        self.assertEqual(worker.events, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("no IW command", errors[0])

    def test_echo_is_rearmed_after_an_interior_initialization(self):
        worker = RecordingSerialWorker()

        worker.stream_commands(
            ["PU", "IN", "PD"],
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

    def test_write_waits_for_cts_and_fails_without_transmitting_when_blocked(self):
        worker = SerialWorker()
        serial_port = BlockedCtsSerial()
        worker.ser = serial_port

        with self.assertRaisesRegex(TimeoutError, "CTS"):
            worker._write_command("PU;", cts_timeout_s=0.02)

        self.assertEqual(serial_port.writes, [])

    def test_wait_command_extends_next_cts_timeout(self):
        worker = SerialWorker()
        serial_port = FakeSerial()
        worker.ser = serial_port

        worker._write_command("!TW180000;", cts_timeout_s=0.02)

        self.assertEqual(serial_port.writes, ["!TW180000;"])
        self.assertEqual(worker._next_timeout_for_command(30.0), 210.0)

    def test_rm_status_accepts_reached_speed_after_zero(self):
        worker = SerialWorker()
        serial_port = BufferedSerial(b"0\r8\r")
        worker.ser = serial_port

        worker._send_rm_status_command("!RM30;", 30, timeout_s=0.1)

        self.assertEqual(serial_port.writes, ["!RM30;"])

    def test_rm_status_rejects_warmup_speed_after_zero(self):
        worker = SerialWorker()
        serial_port = BufferedSerial(b"0\r2\r")
        worker.ser = serial_port

        with self.assertRaisesRegex(SpindleStatusError, "did not reach"):
            worker._send_rm_status_command("!RM30;", 30, timeout_s=0.1)

        self.assertEqual(serial_port.writes, ["!RM30;"])

    def test_rm_zero_accepts_stop_status_after_zero(self):
        worker = SerialWorker()
        serial_port = BufferedSerial(b"0\r9\r")
        worker.ser = serial_port

        worker._send_rm_status_command("!RM0;", 0, timeout_s=0.1)

        self.assertEqual(serial_port.writes, ["!RM0;"])

    def test_cts_timeout_error_reports_flow_state(self):
        worker = SerialWorker()
        serial_port = BlockedCtsSerial()
        worker.ser = serial_port

        with self.assertRaisesRegex(TimeoutError, "CTS=OFF, RTS=ON, out_waiting=0"):
            worker._write_command("PU;", cts_timeout_s=0.02)

        self.assertEqual(serial_port.writes, [])

    def test_stale_stop_flag_does_not_block_manual_writes_after_streaming(self):
        worker = SerialWorker()
        serial_port = FakeSerial()
        worker.ser = serial_port
        worker._streaming = False
        worker._stop_streaming = True

        worker._write_command("IN;", cts_timeout_s=0.02)

        self.assertEqual(serial_port.writes, ["IN;"])

    def test_stop_flag_still_interrupts_active_stream_writes(self):
        worker = SerialWorker()
        serial_port = FakeSerial()
        worker.ser = serial_port
        worker._streaming = True
        worker._stop_streaming = True

        with self.assertRaisesRegex(RuntimeError, "Streaming interrupted"):
            worker._write_command("PU;", cts_timeout_s=0.02)

        self.assertEqual(serial_port.writes, [])


if __name__ == "__main__":
    unittest.main()
