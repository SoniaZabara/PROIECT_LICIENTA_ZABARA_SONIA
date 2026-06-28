"""
Features:
- PySide6 GUI
- pySerial RS-232 connection
- Serial worker thread so the GUI stays responsive
- raw command console
- OS; status query
- OH; hard-clip/machine-range query
- Manual jog controls
- Spindle on/off
- Pause / resume / clear buffer commands
- G-code loading and translation to HP-GL
- HP-GL file loading, preview, and safe line-by-line streaming

Typical LPKF M60 settings for serial communication from the manual:
    9600 baud
    8 data bits
    1 stop bit
    NO parity
    hardware handshake
    NO FIFO
    has a buffer (I don't know how big)
"""

import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import serial
import serial.tools.list_ports
from lpkf_config import (
    HardClipLimits,
    LPKFIni,
    OperatingWindow,
    XYPosition,
    format_number,
    format_step,
    hardclip_from_oh_values,
)
from translator.hpgl_postprocessor import HPGLPostProcessor
from translator.nist_interpreter import NistInterpreter
from translator.nist_parser import NistParser
from translator.nist_transformer import NistTransformer
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot, Qt
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QDoubleSpinBox,
    QVBoxLayout,
    QWidget,
)

@dataclass
class SerialConfig:
    port: str
    baudrate: int = 9600
    bytesize: int = serial.EIGHTBITS
    parity: str = serial.PARITY_NONE
    stopbits: int = serial.STOPBITS_ONE
    rtscts: bool = True # RTS/CTS hardware handshake; RTS = Request to Send (from PC); CTS = Clear to Send (from machine), PC only sends when CTS active
    dsrdtr: bool = False # DSR/DTR handshake; DTR = Data Terminal Ready (PC -> device); DSR = Data Set Ready; indicates device readiness
    timeout: float = 0.2
    write_timeout: float = 2.0


class SpindleStatusError(RuntimeError):
    pass


class SerialWorker(QObject):
    connected = Signal(str)
    disconnected = Signal()
    error = Signal(str)
    log = Signal(str)
    line_received = Signal(str)
    position_received = Signal(float, float, float)
    limits_received = Signal(float, float, float, float, float, float)
    status_received = Signal(str)
    streaming_progress = Signal(int, int)
    streaming_finished = Signal()
    streaming_stopped = Signal()
    flow_status_changed = Signal(bool, bool, bool)

    ACK_TIMEOUT_S = 30.0
    CTS_TIMEOUT_S = 30.0
    WAIT_COMMAND_MARGIN_S = 30.0

    def __init__(self):
        super().__init__()
        self.ser: Optional[serial.Serial] = None
        self._streaming = False
        self._stop_streaming = False
        self._rx_buffer = ""
        self._last_machine_error: str | None = None
        self._last_flow_status: tuple[bool, bool, bool] | None = None
        self._next_send_timeout_s: float | None = None
        self._rm_status_codes: list[int] | None = None

    @Slot(object)
    def connect_port(self, cfg: SerialConfig):
        try:
            self._stop_streaming = False
            if self.ser and self.ser.is_open:
                self.ser.close()

            self.ser = serial.Serial(
                port=cfg.port,
                baudrate=cfg.baudrate,
                bytesize=cfg.bytesize,
                parity=cfg.parity,
                stopbits=cfg.stopbits,
                rtscts=cfg.rtscts,
                dsrdtr=cfg.dsrdtr,
                timeout=cfg.timeout,
                write_timeout=cfg.write_timeout,
            )
            self.log.emit(f"Connected to {cfg.port} at {cfg.baudrate} baud")
            self._emit_flow_status(force=True)
            self.connected.emit(cfg.port)
        except Exception as exc:
            self.error.emit(f"Could not open serial port: {exc}")

    @Slot()
    def disconnect_port(self):
        try:
            self._stop_streaming = True
            if self.ser and self.ser.is_open:
                self.ser.close()
            self.log.emit(f"Disconnected.")
            self._emit_flow_status(force=True)
            self.disconnected.emit()
        except Exception as exc:
            self.error.emit(f"Disconnect error: {exc}")

    def _ensure_connected(self) -> bool:
        if not self.ser or not self.ser.is_open:
            self._emit_flow_status(force=True)
            self.error.emit("Serial port is not connected.")
            return False
        return True

    @Slot(str)
    def send_command(self, command: str):
        if not self._ensure_connected():
            return

        command = command.strip()
        if not command:
            return

        if not command.endswith(";") and not command.endswith(":"):
            command += ";"

        try:
            self._write_command(command)
            self._read_available(wait_s=0.25)
        except Exception as exc:
            self.error.emit(f"Send error: {exc}")

    @Slot()
    def read_available(self):
        self._read_available()

    def _handle_received_line(self, line: str) -> bool:
        line = line.strip()
        if not line:
            return False

        # IN may return the single-character reset response Z without a
        # terminator. If !CT1 follows immediately, its C acknowledgement can
        # arrive in the same serial frame as "ZC".
        if line == "ZC":
            self._handle_received_line("Z")
            return self._handle_received_line("C")

        self.log.emit(f"<< {line}")
        if line == "C":
            return True

        if self._rm_status_codes is not None and re.fullmatch(r"\d+", line):
            self._rm_status_codes.extend(int(char) for char in line)

        if re.fullmatch(r"E\d+", line, flags=re.IGNORECASE):
            self._last_machine_error = line.upper()

        self.line_received.emit(line)
        self._parse_machine_response(line)
        return False

    def _read_waiting_input(self) -> bool:
        if not self.ser or not self.ser.is_open:
            self._emit_flow_status(force=True)
            return False

        acknowledged = False
        waiting = self.ser.in_waiting
        if not waiting:
            return False

        self._rx_buffer += self.ser.read(waiting).decode("ascii", errors="replace")
        parts = re.split(r"[\r\n]+", self._rx_buffer)
        if self._rx_buffer.endswith(("\r", "\n")):
            complete_lines = parts
            self._rx_buffer = ""
        else:
            complete_lines = parts[:-1]
            self._rx_buffer = parts[-1]

        for line in complete_lines:
            acknowledged = self._handle_received_line(line) or acknowledged

        return acknowledged

    def _finish_short_response_fragment(self) -> bool:
        acknowledged = False
        if self._rx_buffer.strip() == "C":
            acknowledged = self._handle_received_line(self._rx_buffer) or acknowledged
            self._rx_buffer = ""
        elif self._rx_buffer.strip() == "Z":
            self._handle_received_line(self._rx_buffer)
            self._rx_buffer = ""
        elif (
            self._rm_status_codes is not None
            and re.fullmatch(r"\d+", self._rx_buffer.strip())
        ):
            self._handle_received_line(self._rx_buffer)
            self._rx_buffer = ""
        return acknowledged

    def _read_lines_until(self, deadline: float) -> bool:
        if not self.ser or not self.ser.is_open:
            self._emit_flow_status(force=True)
            return False

        acknowledged = False

        while True:
            self._emit_flow_status()
            acknowledged = self._read_waiting_input() or acknowledged
            if time.time() >= deadline:
                break
            if not self.ser.in_waiting:
                time.sleep(0.01)

        return self._finish_short_response_fragment() or acknowledged

    def _read_available(self, wait_s: float = 0.0):
        try:
            self._read_lines_until(time.time() + wait_s)
            self._emit_flow_status()
        except Exception as exc:
            self.error.emit(f"Read error: {exc}")

    def _parse_machine_response(self, line: str):
        nums = re.findall(r"[-+]?\d+(?:\.\d+)?", line)
        if line.startswith("P") and len(nums) >= 3:
            try:
                x, y, z = float(nums[0]), float(nums[1]), float(nums[2])
                self.position_received.emit(x, y, z)
            except ValueError:
                pass

        elif line.startswith("W") and len(nums) >= 3:
            try:
                limits = hardclip_from_oh_values([float(value) for value in nums])
                self.limits_received.emit(
                    limits.xmin,
                    limits.ymin,
                    limits.zmin,
                    limits.xmax,
                    limits.ymax,
                    limits.zmax,
                )
            except ValueError:
                pass

        elif line.startswith("S"):
            self.status_received.emit(line)

    def _normalize_command(self, command: str) -> str:
        command = command.strip()
        if command and not command.endswith(";") and not command.endswith(":"):
            command += ";"
        return command

    def _is_connected(self) -> bool:
        return bool(self.ser and self.ser.is_open)

    def _read_cts(self) -> bool:
        if not self._is_connected():
            return False
        try:
            return bool(self.ser.cts)
        except Exception:
            return False

    def _read_rts(self) -> bool:
        if not self._is_connected():
            return False
        try:
            return bool(self.ser.rts)
        except Exception:
            return False

    def _read_out_waiting(self) -> int:
        if not self._is_connected():
            return 0
        try:
            return int(self.ser.out_waiting)
        except Exception:
            return 0

    def _send_allowed_now(self) -> bool:
        return self._is_connected() and self._read_cts() and self._read_out_waiting() == 0

    def _emit_flow_status(self, force: bool = False) -> None:
        status = (self._read_cts(), self._read_rts(), self._send_allowed_now())
        if force or status != self._last_flow_status:
            self._last_flow_status = status
            self.flow_status_changed.emit(*status)

    def _flow_state_text(self) -> str:
        return (
            f"CTS={'ON' if self._read_cts() else 'OFF'}, "
            f"RTS={'ON' if self._read_rts() else 'OFF'}, "
            f"out_waiting={self._read_out_waiting()}"
        )

    def _next_timeout_for_command(self, default_timeout_s: float) -> float:
        timeout_s = default_timeout_s
        if self._next_send_timeout_s is not None:
            timeout_s = max(timeout_s, self._next_send_timeout_s)
            self._next_send_timeout_s = None
        return timeout_s

    def _remember_post_command_wait(self, command: str) -> None:
        match = re.fullmatch(r"!TW\s*([0-9]+(?:\.[0-9]+)?)\s*;", command, re.IGNORECASE)
        if not match:
            return

        wait_s = float(match.group(1)) / 1000.0
        self._next_send_timeout_s = wait_s + self.WAIT_COMMAND_MARGIN_S
        self.log.emit(
            f"Next CTS wait allows {self._next_send_timeout_s:g}s after {command}."
        )

    def _wait_for_send_allowed(self, timeout_s: float, command: str) -> None:
        deadline = time.time() + timeout_s
        blocked_logged = False
        while time.time() < deadline:
            if self._streaming and self._stop_streaming:
                raise RuntimeError("Streaming interrupted by user.")
            self._emit_flow_status()
            if self._send_allowed_now():
                return
            if not blocked_logged:
                self.log.emit(
                    f"Waiting for CTS before sending {command}; no bytes will be written while blocked."
                )
                blocked_logged = True
            self._read_lines_until(min(time.time() + 0.05, deadline))

        self._emit_flow_status(force=True)
        raise TimeoutError(
            f"CTS/output drain timeout after {timeout_s:g}s before sending "
            f"{command}; {self._flow_state_text()}"
        )

    def _wait_for_output_drain(self, timeout_s: float, command: str) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._streaming and self._stop_streaming:
                raise RuntimeError("Streaming interrupted by user.")
            self._emit_flow_status()
            if self._read_out_waiting() == 0:
                return
            self._read_lines_until(min(time.time() + 0.05, deadline))

        self._emit_flow_status(force=True)
        raise TimeoutError(
            f"Serial output did not drain after {timeout_s:g}s for "
            f"{command}; {self._flow_state_text()}"
        )

    def _write_command(self, command: str, cts_timeout_s: float = CTS_TIMEOUT_S) -> str:
        command = self._normalize_command(command)
        for part in re.split(r"[;:]", command):
            if part.strip().upper() in {"PA", "PR"}:
                raise ValueError(
                    f"{part.strip().upper()} requires at least one X,Y coordinate pair; "
                    "use PAx,y for an absolute move or PRdx,dy for a relative move."
                )
        effective_timeout_s = self._next_timeout_for_command(cts_timeout_s)
        self._wait_for_send_allowed(effective_timeout_s, command)
        payload = command.encode("ascii", errors="ignore")
        written = self.ser.write(payload)
        if written != len(payload):
            raise serial.SerialTimeoutException(
                f"Serial write incomplete for {command}: {written}/{len(payload)} bytes"
            )
        self._wait_for_output_drain(effective_timeout_s, command)
        self.log.emit(f">> {command}")
        self._remember_post_command_wait(command)
        self._emit_flow_status()
        return command

    def _wait_for_ack(self, command: str, timeout_s: float) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._streaming and self._stop_streaming:
                raise RuntimeError("Streaming interrupted by user.")
            acknowledged = self._read_lines_until(min(time.time() + 0.1, deadline))
            if self._last_machine_error is not None:
                raise RuntimeError(
                    f"Machine returned {self._last_machine_error} for {command}"
                )
            if acknowledged:
                return

        raise TimeoutError(f"No C acknowledgement received for {command}")

    def _send_and_wait_ack(
        self,
        command: str,
        ack_timeout_s: float = ACK_TIMEOUT_S,
        cts_timeout_s: float = CTS_TIMEOUT_S,
    ) -> str:
        self._last_machine_error = None
        sent = self._write_command(command, cts_timeout_s)
        self._wait_for_ack(sent, ack_timeout_s)
        return sent

    def _ack_timeout_for_command(self, command: str) -> float:
        timeout_s = self.ACK_TIMEOUT_S
        match = re.fullmatch(r"!TW\s*([0-9]+(?:\.[0-9]+)?)\s*;", command, re.IGNORECASE)
        if match:
            timeout_s = max(
                timeout_s,
                float(match.group(1)) / 1000.0 + self.WAIT_COMMAND_MARGIN_S,
            )
        return timeout_s

    def _rm_speed_for_command(self, command: str) -> int | None:
        match = re.fullmatch(r"!RM\s*([0-9]+)\s*;", command, re.IGNORECASE)
        if not match:
            return None
        return int(match.group(1))

    def _is_no_ack_echo_command(self, command: str) -> bool:
        return bool(re.fullmatch(r"!OC\s*;", command, re.IGNORECASE))

    def _send_with_echo_policy(self, command: str) -> str:
        command = self._normalize_command(command)
        rm_speed = self._rm_speed_for_command(command)

        if self._is_no_ack_echo_command(command):
            sent = self._write_command(command)
            self._read_available()
            return sent

        if rm_speed is not None:
            return self._send_rm_status_command(command, rm_speed)

        return self._send_and_wait_ack(
            command,
            ack_timeout_s=self._ack_timeout_for_command(command),
        )

    def _send_rm_status_command(
        self,
        command: str,
        requested_thousands: int,
        timeout_s: float = ACK_TIMEOUT_S,
    ) -> str:
        self._last_machine_error = None
        sent = self._write_command(command)
        self._wait_for_rm_status(sent, requested_thousands, timeout_s)
        return sent

    def _wait_for_rm_status(
        self,
        command: str,
        requested_thousands: int,
        timeout_s: float,
    ) -> None:
        deadline = time.time() + timeout_s
        saw_ready_marker = False
        self._rm_status_codes = []

        try:
            while time.time() < deadline:
                if self._streaming and self._stop_streaming:
                    raise RuntimeError("Streaming interrupted by user.")

                self._read_lines_until(min(time.time() + 0.1, deadline))
                if self._last_machine_error is not None:
                    raise SpindleStatusError(
                        f"Machine returned {self._last_machine_error} for {command}"
                    )

                while self._rm_status_codes:
                    code = self._rm_status_codes.pop(0)
                    if not saw_ready_marker:
                        if code != 0:
                            raise SpindleStatusError(
                                f"Unexpected first spindle response {code} for "
                                f"{command}; expected 0."
                            )
                        saw_ready_marker = True
                        continue

                    self._validate_rm_final_status(
                        command,
                        requested_thousands,
                        code,
                    )
                    return

            if saw_ready_marker:
                raise TimeoutError(
                    f"No final spindle status received for {command} after 0."
                )
            raise TimeoutError(f"No spindle status received for {command}")
        finally:
            self._rm_status_codes = None

    def _validate_rm_final_status(
        self,
        command: str,
        requested_thousands: int,
        status_code: int,
    ) -> None:
        if requested_thousands == 0:
            if status_code == 9:
                self.log.emit(f"Spindle stop confirmed by status 9 for {command}.")
                return
            raise SpindleStatusError(
                f"Unexpected spindle stop status {status_code} for {command}; "
                "expected 9."
            )

        if status_code == 8:
            self.log.emit(f"Spindle speed confirmed by status 8 for {command}.")
            return

        if status_code in {1, 2}:
            raise SpindleStatusError(
                f"Spindle did not reach requested speed for {command}; "
                f"machine returned status {status_code} after 0."
            )

        raise SpindleStatusError(
            f"Unexpected spindle status {status_code} for {command}; expected 8."
        )

    def _stop_spindle_after_stream_error(self) -> str | None:
        if not self.ser or not self.ser.is_open:
            return "Could not stop spindle after streaming error: serial port is not connected."

        close_error: str | None = None
        try:
            self.log.emit(
                "Closing spindle channel after streaming error with !CC;."
            )
            self._send_and_wait_ack("!CC;")
        except Exception as exc:
            close_error = f"Could not close spindle channel after streaming error: {exc}"

        try:
            self.log.emit(
                "Stopping spindle after streaming error with !OC; !RM0; !CC;."
            )
            self._write_command("!OC;")
            self._read_available()
            self._send_rm_status_command("!RM0;", 0)
            self._send_and_wait_ack("!CC;")
        except Exception as exc:
            stop_error = f"Could not stop spindle after streaming error: {exc}"
            return f"{close_error} {stop_error}" if close_error else stop_error

        return close_error

    @Slot(list, bool, str)
    def stream_commands(
        self,
        commands: list[str],
        use_echo_ack: bool = True,
        iw_command: str = "",
    ):
        if not self._ensure_connected():
            return
        if self._streaming:
            self.error.emit("Already streaming a file.")
            return

        self._streaming = True
        self._stop_streaming = False
        total = len(commands)
        iw_command = self._normalize_command(iw_command)

        echo_enabled = False
        completed = False
        stopped_by_user = False
        stream_result: str | None = None
        stream_error: str | None = None
        safety_stop_error: str | None = None
        cleanup_error: str | None = None

        try:
            if any(self._normalize_command(command).upper() == "IN;" for command in commands):
                if not iw_command:
                    raise RuntimeError(
                        "IN resets the input window, but no IW command "
                        "was supplied for restoration."
                    )

            for index, command in enumerate(commands, start=1):
                if self._stop_streaming:
                    self.log.emit("Streaming interrupted by user.")
                    stopped_by_user = True
                    break

                command = self._normalize_command(command)
                if not command:
                    continue
                is_initialize = command.upper() == "IN;"

                if is_initialize:
                    # IN restores defaults, including non-echo mode. Do not
                    # wait for a C that the reset will never produce.
                    self._write_command(command)
                    self._read_available()
                    echo_enabled = False

                if use_echo_ack:
                    if not echo_enabled:
                        self.log.emit("Enabling echo acknowledgement mode with !CT1;")
                        self._send_with_echo_policy("!CT1;")
                        echo_enabled = True

                    if not is_initialize:
                        self._send_with_echo_policy(command)
                    elif iw_command:
                        self.log.emit(f"Restoring input window with {iw_command}")
                        self._send_with_echo_policy(iw_command)
                else:
                    if not is_initialize:
                        self._write_command(command)
                        self._read_available()
                    elif iw_command:
                        self.log.emit(f"Restoring input window with {iw_command}")
                        self._write_command(iw_command)
                        self._read_available()

                self.streaming_progress.emit(index, total)

            if stopped_by_user:
                stream_result = "stopped"
            else:
                completed = True
                stream_result = "finished"
        except Exception as exc:
            if str(exc) == "Streaming interrupted by user.":
                self.log.emit("Streaming interrupted by user.")
                stopped_by_user = True
                stream_result = "stopped"
            elif isinstance(exc, SpindleStatusError):
                stream_error = f"Streaming error: {exc}"
                safety_stop_error = self._stop_spindle_after_stream_error()
            else:
                stream_error = f"Streaming error: {exc}"
        finally:
            if use_echo_ack and echo_enabled and completed and self.ser and self.ser.is_open:
                try:
                    self.log.emit("Disabling echo acknowledgement mode with !CT0;")
                    self._write_command("!CT0;")
                    self._read_lines_until(time.time() + 0.5)
                except Exception as exc:
                    cleanup_error = f"Could not disable echo mode: {exc}"
            elif use_echo_ack and echo_enabled:
                self.log.emit(
                    "Echo acknowledgement mode may still be enabled; recover manually before streaming again."
                )
            self._streaming = False
            self._stop_streaming = False
            self._emit_flow_status(force=True)

        reported_error = False
        if stream_error is not None:
            self.error.emit(stream_error)
            reported_error = True
        if safety_stop_error is not None:
            self.error.emit(safety_stop_error)
            reported_error = True
        if cleanup_error is not None:
            self.error.emit(cleanup_error)
            reported_error = True

        if reported_error:
            return

        if stream_result == "stopped":
            self.streaming_stopped.emit()
        elif stream_result == "finished":
            self.streaming_finished.emit()

    @Slot()
    def request_stop_streaming(self):
        self._stop_streaming = True

def split_hpgl_commands(text: str) -> list[str]:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = text.replace("\n", "").replace("\r", "")
    parts = re.split(r"[;:]", text)
    return [p.strip() for p in parts if p.strip()]


def translate_gcode_to_hpgl(
    path: Path,
    work_origin_steps: tuple[float, float] | None = None,
) -> str:
    parser = NistParser()
    parse_tree = parser.parse(input_path=str(path))

    transformer = NistTransformer()
    ast_tree = transformer.transform(parse_tree)

    interpreter = NistInterpreter()
    ir = interpreter.interpret(ast_tree)

    post = HPGLPostProcessor(work_origin_steps=work_origin_steps)
    return post.translate(ir)

class MainWindow(QMainWindow):
    connect_requested = Signal(object)
    disconnect_requested = Signal()
    send_requested = Signal(str)
    read_requested = Signal()
    stream_requested = Signal(list, bool, str)
    stop_stream_requested = Signal()
    POLL_INTERVAL_MS = 1000

    def __init__(self):
        super().__init__()
        self.setWindowTitle("LPKF ProtoMat M60 Controller")
        self.resize(1150, 760)

        self.worker_thread = QThread(self)
        self.worker = SerialWorker()
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.start()

        self.connect_requested.connect(self.worker.connect_port)
        self.disconnect_requested.connect(self.worker.disconnect_port)
        self.send_requested.connect(self.worker.send_command)
        self.read_requested.connect(self.worker.read_available)
        self.stream_requested.connect(self.worker.stream_commands)
        self.stop_stream_requested.connect(
            self.worker.request_stop_streaming,
            Qt.ConnectionType.DirectConnection,
        )

        self.worker.connected.connect(self.on_connected)
        self.worker.disconnected.connect(self.on_disconnected)
        self.worker.error.connect(self.on_worker_error)
        self.worker.log.connect(self.append_log)
        self.worker.position_received.connect(self.update_position)
        self.worker.limits_received.connect(self.update_limits)
        self.worker.status_received.connect(self.update_machine_status)
        self.worker.streaming_progress.connect(self.update_progress)
        self.worker.streaming_finished.connect(self.on_stream_finished)
        self.worker.streaming_stopped.connect(self.on_stream_stopped)
        self.worker.flow_status_changed.connect(self.update_flow_status)

        self.hpgl_commands: list[str] = []
        self.lpkf_ini = LPKFIni(Path(__file__).resolve().with_name("lpkf.ini"))
        self.hardclip_limits: Optional[HardClipLimits] = None
        self.operating_window: Optional[OperatingWindow] = None
        self.streaming_active = False
        self._ini_load_error: Optional[str] = None
        try:
            self.hardclip_limits = self.lpkf_ini.load_limits()
            self.operating_window = self.lpkf_ini.load_window()
            if self.hardclip_limits is not None and self.operating_window is not None:
                self.operating_window.validate(self.hardclip_limits)
        except Exception as exc:
            self._ini_load_error = str(exc)
            self.hardclip_limits = None
            self.operating_window = None

        self._build_ui()
        self._build_menu()
        self.refresh_ports()
        self.refresh_top_actions()
        self.show_loaded_limits()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.poll_serial)
        self.poll_timer.start(self.POLL_INTERVAL_MS)

    def poll_serial(self) -> None:
        if self.streaming_active:
            return
        self.read_requested.emit()

    def set_streaming_active(self, active: bool) -> None:
        self.streaming_active = active
        if hasattr(self, "poll_timer"):
            if active:
                self.poll_timer.stop()
            elif not self.poll_timer.isActive():
                self.poll_timer.start(self.POLL_INTERVAL_MS)
        self.refresh_top_actions()

    def _build_menu(self):
        file_menu = self.menuBar().addMenu("File")

        load_action = QAction("Load HP-GL...", self)
        load_action.triggered.connect(self.load_hpgl_file)
        file_menu.addAction(load_action)

        load_gcode_action = QAction("Load G-code...", self)
        load_gcode_action.triggered.connect(self.load_gcode_file)
        file_menu.addAction(load_gcode_action)

        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        self.pause_action = QAction("PAUSE", self)
        self.pause_action.triggered.connect(lambda: self.move_to_named_position("pause"))
        self.default_home_action = QAction("DEFAULT HOME", self)
        self.default_home_action.triggered.connect(
            lambda: self.move_to_named_position("default_home")
        )
        self.calibrated_home_action = QAction("CALIBRATED HOME", self)
        self.calibrated_home_action.triggered.connect(
            lambda: self.move_to_named_position("calibrated_home")
        )
        self.zero_action = QAction("ZERO", self)
        self.zero_action.triggered.connect(lambda: self.move_to_named_position("zero"))
        self.iw_action = QAction("SET IW", self)
        self.iw_action.triggered.connect(self.open_iw_dialog)

        for action in (
            self.pause_action,
            self.default_home_action,
            self.calibrated_home_action,
            self.zero_action,
            self.iw_action,
        ):
            self.menuBar().addAction(action)

    def _build_ui(self) :
        root = QWidget()
        self.setCentralWidget(root)
        main = QHBoxLayout(root)

        left = QVBoxLayout()
        right = QVBoxLayout()
        main.addLayout(left, 1)
        main.addLayout(right, 2)

        # Connection
        conn_group = QGroupBox("Serial connection")
        conn_layout = QFormLayout(conn_group)

        self.port_combo = QComboBox()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh_ports)
        port_row = QHBoxLayout()
        port_row.addWidget(self.port_combo)
        port_row.addWidget(self.refresh_btn)

        self.baud_label = QLabel("9600")
        self.baud_label.setToolTip("The LPKF ProtoMat M60 uses 9600 baud.")

        self.hw_handshake = QCheckBox("RTS/CTS hardware handshake (required)")
        self.hw_handshake.setChecked(True)
        self.hw_handshake.setEnabled(False)
        self.hw_handshake.setToolTip("Required for communication with the LPKF ProtoMat M60.")

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.connect_serial)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(lambda: self.disconnect_requested.emit())
        self.disconnect_btn.setEnabled(False)
        buttons = QHBoxLayout()
        buttons.addWidget(self.connect_btn)
        buttons.addWidget(self.disconnect_btn)

        conn_layout.addRow("Port", port_row)
        conn_layout.addRow("Baud", self.baud_label)
        conn_layout.addRow("Flow", self.hw_handshake)
        conn_layout.addRow(buttons)
        left.addWidget(conn_group)

        # Status
        status_group = QGroupBox("Status / position")
        status_layout = QGridLayout(status_group)
        self.x_label = QLabel("X: —")
        self.y_label = QLabel("Y: —")
        self.z_label = QLabel("Z: —")
        self.z_label.setEnabled(False)
        self.z_label.setToolTip("Not used for the M60 MVP interface; the spindle is controlled as up/down, not as a variable Z position.")
        self.progress_label = QLabel("File: —")
        self.limits_label = QLabel("Limits: -")
        self.status_label = QLabel("Status: -")
        status_layout.addWidget(self.x_label, 0, 0)
        status_layout.addWidget(self.y_label, 0, 1)
        status_layout.addWidget(self.z_label, 0, 2)
        status_layout.addWidget(self.progress_label, 1, 0, 1, 3)
        status_layout.addWidget(self.limits_label, 2, 0, 1, 3)
        status_layout.addWidget(self.status_label, 3, 0, 1, 3)

        query_status = QPushButton("Query OS;")
        query_status.clicked.connect(lambda: self.send_requested.emit("OS;"))
        query_limits = QPushButton("Query OH;")
        query_limits.clicked.connect(lambda: self.send_requested.emit("OH;"))
        query_pos = QPushButton("Query !ON0;")
        query_pos.clicked.connect(lambda: self.send_requested.emit("!ON0;"))
        status_layout.addWidget(query_status, 4, 0)
        status_layout.addWidget(query_limits, 4, 1)
        status_layout.addWidget(query_pos, 4, 2)
        left.addWidget(status_group)

        # Manual control
        jog_group = QGroupBox("Manual control")
        jog_layout = QGridLayout(jog_group)

        self.step_spin = QDoubleSpinBox()
        self.step_spin.setRange(1, 10000)
        self.step_spin.setValue(100)
        self.step_spin.setSuffix(" steps")
        self.step_spin.setDecimals(0)

        jog_layout.addWidget(QLabel("XY step"), 0, 0)
        jog_layout.addWidget(self.step_spin, 0, 1, 1, 2)
        z_step_label = QLabel("Z step")
        z_step_label.setEnabled(False)
        z_step_placeholder = QLabel("Not used")
        z_step_placeholder.setEnabled(False)
        jog_layout.addWidget(z_step_label, 1, 0)
        jog_layout.addWidget(z_step_placeholder, 1, 1, 1, 2)

        btn_y_plus = QPushButton("Y+")
        btn_x_minus = QPushButton("X−")
        btn_x_plus = QPushButton("X+")
        btn_y_minus = QPushButton("Y−")
        btn_z_plus = QPushButton("Z+")
        btn_z_minus = QPushButton("Z−")
        btn_z_plus.setEnabled(False)
        btn_z_minus.setEnabled(False)
        btn_z_plus.setToolTip("Not implemented for the M60 MVP interface.")
        btn_z_minus.setToolTip("Not implemented for the M60 MVP interface.")

        btn_y_plus.clicked.connect(lambda: self.jog(0, +self.step()))
        btn_y_minus.clicked.connect(lambda: self.jog(0, -self.step()))
        btn_x_plus.clicked.connect(lambda: self.jog(+self.step(), 0))
        btn_x_minus.clicked.connect(lambda: self.jog(-self.step(), 0))

        jog_layout.addWidget(btn_y_plus, 2, 1)
        jog_layout.addWidget(btn_x_minus, 3, 0)
        jog_layout.addWidget(btn_x_plus, 3, 2)
        jog_layout.addWidget(btn_y_minus, 4, 1)
        jog_layout.addWidget(btn_z_plus, 2, 3)
        jog_layout.addWidget(btn_z_minus, 4, 3)

        pen_up = QPushButton("PU; tool up")
        pen_down = QPushButton("PD; tool down")
        pen_up.clicked.connect(lambda: self.send_requested.emit("PU;"))
        pen_down.clicked.connect(lambda: self.send_requested.emit("PD;"))
        jog_layout.addWidget(pen_up, 5, 0, 1, 2)
        jog_layout.addWidget(pen_down, 5, 2, 1, 2)

        left.addWidget(jog_group)

        # Machine control
        machine_group = QGroupBox("Machine commands")
        machine_layout = QGridLayout(machine_group)

        spindle_on = QPushButton("Vacuum ON !EM1;")
        spindle_off = QPushButton("Vacuum OFF !EM0;")
        stop_btn = QPushButton("Pause !ST;")
        go_btn = QPushButton("Resume !GO;")
        clear_btn = QPushButton("Clear buffer !CB;")
        init_btn = QPushButton("Initialize IN;")

        spindle_on.clicked.connect(lambda: self.confirm_and_send("Turn vacuum ON?", "!EM1;"))
        spindle_off.clicked.connect(lambda: self.send_requested.emit("!EM0;"))
        stop_btn.clicked.connect(lambda: self.send_requested.emit("!ST;"))
        go_btn.clicked.connect(lambda: self.send_requested.emit("!GO;"))
        clear_btn.clicked.connect(lambda: self.confirm_and_send("Clear machine command buffer?", "!CB;"))
        init_btn.clicked.connect(lambda: self.confirm_and_send("Initialize machine control?", "IN;"))

        machine_layout.addWidget(spindle_on, 0, 0)
        machine_layout.addWidget(spindle_off, 0, 1)
        machine_layout.addWidget(stop_btn, 1, 0)
        machine_layout.addWidget(go_btn, 1, 1)
        machine_layout.addWidget(clear_btn, 2, 0)
        machine_layout.addWidget(init_btn, 2, 1)
        left.addWidget(machine_group)

        # Raw command
        raw_group = QGroupBox("Raw command")
        raw_layout = QHBoxLayout(raw_group)
        self.raw_input = QLineEdit()
        self.raw_input.setPlaceholderText("Example: PU;PA1000,1000; or OS;")
        self.raw_input.returnPressed.connect(self.send_raw)
        raw_send = QPushButton("Send")
        raw_send.clicked.connect(self.send_raw)
        raw_layout.addWidget(self.raw_input)
        raw_layout.addWidget(raw_send)
        left.addWidget(raw_group)
        left.addStretch(1)

        # Job file
        file_group = QGroupBox("Job file / HP-GL output")
        file_layout = QVBoxLayout(file_group)
        file_buttons = QHBoxLayout()
        load_gcode_btn = QPushButton("Load G-code")
        load_gcode_btn.setToolTip("Translate an RS274/NGC G-code file to HP-GL.")
        load_gcode_btn.clicked.connect(self.load_gcode_file)
        load_btn = QPushButton("Load HP-GL")
        load_btn.clicked.connect(self.load_hpgl_file)
        stream_btn = QPushButton("Stream file")
        stream_btn.clicked.connect(self.stream_file)
        stop_stream_btn = QPushButton("Stop streaming")
        stop_stream_btn.clicked.connect(lambda: self.stop_stream_requested.emit())
        self.apply_home_offset = QCheckBox("Apply calibrated HOME offset")
        self.apply_home_offset.setChecked(True)
        self.apply_home_offset.setToolTip(
            "When enabled, G-code X0/Y0 is translated to calibrated HOME. "
            "When disabled, G-code coordinates are relative to machine ZERO. "
            "This setting is applied when a G-code file is loaded."
        )
        self.echo_ack_streaming = QCheckBox("Use C echo (!CT1)")
        self.echo_ack_streaming.setChecked(False)
        self.echo_ack_streaming.setToolTip(
            "Optional protocol acknowledgement layer. When enabled, streaming waits for C after each command. "
            "When disabled, streaming still waits for CTS before every write and does not use fixed delays."
        )
        file_buttons.addWidget(load_gcode_btn)
        file_buttons.addWidget(load_btn)
        file_buttons.addWidget(self.apply_home_offset)
        file_buttons.addWidget(self.echo_ack_streaming)
        file_buttons.addWidget(stream_btn)
        file_buttons.addWidget(stop_stream_btn)

        flow_status = QHBoxLayout()
        self.cts_indicator = QLabel("CTS: —")
        self.rts_indicator = QLabel("RTS: —")
        self.send_allowed_indicator = QLabel("Send: —")
        for indicator in (
            self.cts_indicator,
            self.rts_indicator,
            self.send_allowed_indicator,
        ):
            indicator.setMinimumWidth(95)
            indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cts_indicator.setToolTip("Actual Clear To Send state read from pySerial.")
        self.rts_indicator.setToolTip("RTS state reported by pySerial.")
        self.send_allowed_indicator.setToolTip(
            "Streaming may write only when the port is open, CTS is active, and no previous bytes remain queued."
        )
        flow_status.addWidget(QLabel("Serial flow"))
        flow_status.addWidget(self.cts_indicator)
        flow_status.addWidget(self.rts_indicator)
        flow_status.addWidget(self.send_allowed_indicator)
        flow_status.addStretch(1)
        self.update_flow_status(False, False, False)

        self.preview = QPlainTextEdit()
        self.preview.setPlaceholderText("Loaded or translated HP-GL commands will appear here.")
        self.preview.setFont(QFont("Courier New", 10))
        file_layout.addLayout(file_buttons)
        file_layout.addLayout(flow_status)
        file_layout.addWidget(self.preview, 2)
        right.addWidget(file_group, 2)

        # Log
        log_group = QGroupBox("Serial log")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Courier New", 10))
        log_layout.addWidget(self.log_view)
        right.addWidget(log_group, 1)

    def step(self) -> int:
        return int(self.step_spin.value())

    def refresh_top_actions(self) -> None:
        if not hasattr(self, "pause_action"):
            return

        enabled = self.hardclip_limits is not None and not self.streaming_active
        positions = (
            self.hardclip_limits.positions(self.lpkf_ini.load_pause_margin_units())
            if self.hardclip_limits
            else {}
        )
        configured_home = self.lpkf_ini.load_position("calibrated_home")
        if configured_home is not None:
            positions["calibrated_home"] = configured_home
        actions = {
            "pause": self.pause_action,
            "default_home": self.default_home_action,
            "calibrated_home": self.calibrated_home_action,
            "zero": self.zero_action,
        }
        for name, action in actions.items():
            target = positions.get(name)
            action.setEnabled(
                enabled
                and target is not None
                and self.hardclip_limits is not None
                and self.hardclip_limits.contains_xy(target)
            )
        self.iw_action.setEnabled(enabled)

    def show_loaded_limits(self) -> None:
        if self._ini_load_error:
            self.show_error(f"Could not trust lpkf.ini: {self._ini_load_error}")
            self.limits_label.setText("Limits: run OH;")
            return
        if self.hardclip_limits is None:
            self.append_log("No lpkf.ini hardclip data. Run OH; before using position buttons.")
            self.limits_label.setText("Limits: run OH;")
            return

        self.display_limits(self.hardclip_limits)
        self.append_log(f"Loaded hardclip limits from {self.lpkf_ini.path.name}.")

    def display_limits(self, limits: HardClipLimits) -> None:
        self.limits_label.setText(
            "Limits: "
            f"X {format_number(limits.xmin)}..{format_number(limits.xmax)}, "
            f"Y {format_number(limits.ymin)}..{format_number(limits.ymax)}, "
            f"Z {format_number(limits.zmin)}..{format_number(limits.zmax)}"
        )

    def move_to_named_position(self, name: str) -> None:
        if self.hardclip_limits is None:
            self.show_error("Hardclip limits are unknown. Run OH; first.")
            return

        position = self.hardclip_limits.positions(
            self.lpkf_ini.load_pause_margin_units()
        )[name]
        if name == "calibrated_home":
            position = self.lpkf_ini.load_position(name) or position
        if not self.hardclip_limits.contains_xy(position):
            self.show_error(f"{name} is outside the hardclip limits.")
            return

        label = name.replace("_", " ").upper()
        answer = QMessageBox.question(
            self,
            f"Move to {label}?",
            f"Raise the pen and move to {label} at "
            f"X={format_number(position.x)}, Y={format_number(position.y)}?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.send_requested.emit(
            f"PU;PA{format_step(position.x)},{format_step(position.y)};"
        )

    def open_iw_dialog(self) -> None:
        if self.hardclip_limits is None:
            self.show_error("Hardclip limits are unknown. Run OH; before setting IW.")
            return

        initial = self.operating_window or OperatingWindow(
            self.hardclip_limits.xmin,
            self.hardclip_limits.ymin,
            self.hardclip_limits.xmax,
            self.hardclip_limits.ymax,
        )

        dialog = QDialog(self)
        dialog.setWindowTitle("Set IW operating window")
        layout = QFormLayout(dialog)
        fields: dict[str, QDoubleSpinBox] = {}
        for label, value in (
            ("Xmin", initial.xmin),
            ("Ymin", initial.ymin),
            ("Xmax", initial.xmax),
            ("Ymax", initial.ymax),
        ):
            spin = QDoubleSpinBox()
            spin.setRange(-2_000_000_000, 2_000_000_000)
            spin.setDecimals(3)
            spin.setValue(value)
            fields[label] = spin
            layout.addRow(label, spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        window = OperatingWindow(
            xmin=float(format_step(fields["Xmin"].value())),
            ymin=float(format_step(fields["Ymin"].value())),
            xmax=float(format_step(fields["Xmax"].value())),
            ymax=float(format_step(fields["Ymax"].value())),
        )
        try:
            window.validate(self.hardclip_limits)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid IW window", str(exc))
            return

        command = (
            f"IW{format_step(window.xmin)},{format_step(window.ymin)},"
            f"{format_step(window.xmax)},{format_step(window.ymax)};"
        )
        self.send_requested.emit(command)
        self.operating_window = window
        try:
            self.lpkf_ini.save_window(window)
            self.append_log(f"Saved IW operating window to {self.lpkf_ini.path.name}.")
        except Exception as exc:
            self.show_error(f"Could not save IW operating window: {exc}")

    def refresh_ports(self):
        current = self.port_combo.currentText()
        self.port_combo.clear()
        ports = list(serial.tools.list_ports.comports())
        for p in ports:
            self.port_combo.addItem(f"{p.device} - {p.description}", p.device)
        if current:
            idx = self.port_combo.findText(current, Qt.MatchFlag.MatchStartsWith)
            if idx >= 0:
                self.port_combo.setCurrentIndex(idx)

    def connect_serial(self):
        port = self.port_combo.currentData()
        if not port:
            self.show_error("No serial port selected.")
            return
        cfg = SerialConfig(
            port=port,
            baudrate=9600,
            rtscts=True,
        )
        self.connect_requested.emit(cfg)

    def on_connected(self, port: str):
        self.connect_btn.setEnabled(False)
        self.disconnect_btn.setEnabled(True)
        self.statusBar().showMessage(f"Connected to {port}")

    def on_disconnected(self):
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self.statusBar().showMessage("Disconnected")

    def append_log(self, text: str):
        self.log_view.appendPlainText(text)

    def show_error(self, text: str):
        self.append_log(f"ERROR: {text}")

    def on_worker_error(self, text: str):
        if self.streaming_active:
            self.set_streaming_active(False)
        self.show_error(text)

    def set_indicator(self, label: QLabel, name: str, active: bool) -> None:
        if active:
            label.setText(f"{name}: ON")
            label.setStyleSheet(
                "QLabel { background: #1f8f45; color: white; border: 1px solid #145c2d; padding: 2px; }"
            )
        else:
            label.setText(f"{name}: OFF")
            label.setStyleSheet(
                "QLabel { background: #5f6368; color: white; border: 1px solid #3c4043; padding: 2px; }"
            )

    @Slot(bool, bool, bool)
    def update_flow_status(self, cts: bool, rts: bool, send_allowed: bool) -> None:
        self.set_indicator(self.cts_indicator, "CTS", cts)
        self.set_indicator(self.rts_indicator, "RTS", rts)
        self.set_indicator(self.send_allowed_indicator, "Send", send_allowed)

    def update_position(self, x: float, y: float, z: float):
        self.x_label.setText(f"X: {x:g}")
        self.y_label.setText(f"Y: {y:g}")
        # Z is intentionally not updated in the interface
        # because spindle motion tool is up/down only
        self.z_label.setText("Z: —")

    def update_limits(
        self,
        xmin: float,
        ymin: float,
        zmin: float,
        xmax: float,
        ymax: float,
        zmax: float,
    ):
        limits = HardClipLimits(xmin, ymin, zmin, xmax, ymax, zmax)
        try:
            limits.validate()
            self.lpkf_ini.save_limits(limits)
        except Exception as exc:
            self.show_error(f"Could not save OH hardclip response: {exc}")
            return

        self.hardclip_limits = limits
        self.display_limits(limits)
        self.refresh_top_actions()
        self.append_log(f"Saved OH hardclip response to {self.lpkf_ini.path.name}.")

    def update_machine_status(self, status: str):
        self.status_label.setText(f"Status: {status}")

    def update_progress(self, index: int, total: int):
        self.progress_label.setText(f"File: {index}/{total} commands")

    def on_stream_finished(self) -> None:
        self.set_streaming_active(False)
        self.append_log("Streaming finished.")
        self.statusBar().showMessage("Streaming finished", 5000)

    def on_stream_stopped(self) -> None:
        self.set_streaming_active(False)
        self.append_log("Streaming stopped by user. Confirm machine state before recovery.")
        self.statusBar().showMessage("Streaming stopped", 5000)

    def send_raw(self):
        text = self.raw_input.text().strip()
        if text:
            self.send_requested.emit(text)
            self.raw_input.clear()

    def jog(self, dx: int, dy: int):
        # Relative movement with tool up.
        self.send_requested.emit(f"PU;PR{dx},{dy};")

    def confirm_and_send(self, question: str, command: str):
        answer = QMessageBox.question(self, "Confirm", question)
        if answer == QMessageBox.StandardButton.Yes:
            self.send_requested.emit(command)

    def load_hpgl_file(self):
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Load HP-GL file",
            "",
            "HP-GL files (*.plt *.hpgl *.hgl *.txt);;All files (*.*)",
        )
        if not path_str:
            return

        path = Path(path_str)
        try:
            text = path.read_text(encoding="ascii", errors="ignore")
            self.hpgl_commands = split_hpgl_commands(text)
            self.preview.setPlainText(";\n".join(self.hpgl_commands) + (";" if self.hpgl_commands else ""))
            self.progress_label.setText(f"File: {len(self.hpgl_commands)} commands loaded")
            self.append_log(f"Loaded {path.name}: {len(self.hpgl_commands)} commands.")
        except Exception as exc:
            self.show_error(f"Could not load file: {exc}")

    def load_gcode_file(self):
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Load G-code file",
            "",
            "G-code files (*.gcode *.gc *.ngc *.nc *.tap *.txt);;All files (*.*)",
        )
        if not path_str:
            return

        path = Path(path_str)
        try:
            work_origin: tuple[float, float] | None = None
            origin_description = "machine ZERO"

            if self.apply_home_offset.isChecked():
                home = self.lpkf_ini.load_position("calibrated_home")
                if home is None:
                    raise RuntimeError(
                        "Calibrated HOME is not configured in lpkf.ini. "
                        "Store the measured HOME before translating a job."
                    )
                if self.hardclip_limits is None:
                    raise RuntimeError("Hardclip limits are unknown. Run OH before translating a job.")
                if not self.hardclip_limits.contains_xy(home):
                    raise RuntimeError("Configured calibrated HOME is outside the hardclip limits.")

                work_origin = (home.x, home.y)
                origin_description = f"HOME X={home.x:g}, Y={home.y:g}"
            else:
                answer = QMessageBox.warning(
                    self,
                    "HOME offset disabled",
                    "The translated job will use machine ZERO as G-code X0/Y0.\n\n"
                    "Continue only if this is intentional.",
                    QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if answer != QMessageBox.StandardButton.Ok:
                    return
                self.append_log(
                    "WARNING: Translating G-code without the calibrated HOME offset."
                )

            hpgl = translate_gcode_to_hpgl(path, work_origin)
            self.hpgl_commands = split_hpgl_commands(hpgl)
            self.preview.setPlainText(";\n".join(self.hpgl_commands) + (";" if self.hpgl_commands else ""))
            self.progress_label.setText(f"File: {len(self.hpgl_commands)} translated commands")
            self.append_log(
                f"Translated {path.name} from {origin_description}: "
                f"{len(self.hpgl_commands)} HP-GL commands."
            )
        except Exception as exc:
            self.show_error(f"Could not translate G-code file: {exc}")

    def stream_file(self) -> None:
        if not self.hpgl_commands:
            self.show_error("No HP-GL job loaded.")
            return

        contains_initialize = any(
            command.strip().rstrip(";:").upper() == "IN"
            for command in self.hpgl_commands
        )
        iw_command = ""
        if contains_initialize:
            window = self.operating_window
            if window is None:
                if self.hardclip_limits is None:
                    self.show_error(
                        "No operating window is configured. Run OH or configure "
                        "IW before streaming a file containing IN."
                    )
                    return
                window = OperatingWindow(
                    self.hardclip_limits.xmin,
                    self.hardclip_limits.ymin,
                    self.hardclip_limits.xmax,
                    self.hardclip_limits.ymax,
                )

            try:
                if self.hardclip_limits is not None:
                    window.validate(self.hardclip_limits)
            except ValueError as exc:
                self.show_error(f"Cannot restore IW before streaming: {exc}")
                return

            iw_command = (
                f"IW{format_step(window.xmin)},{format_step(window.ymin)},"
                f"{format_step(window.xmax)},{format_step(window.ymax)};"
            )

        answer = QMessageBox.question(
            self,
            "Start streaming?",
            "Start sending the loaded HP-GL file to the machine?\n\n"
            "Make sure the tool is raised, the work area is clear, and the spindle state is correct.\n"
            "MAKE SURE THE SPINDLE IS WARMED UP AND READY TO CUT BEFORE STREAMING.\n"
            "failure to do so may damage the machine, workpiece and especially the SPINDLE which will underperfom and not reach it's target speed!!!",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.set_streaming_active(True)
        self.stream_requested.emit(
            self.hpgl_commands,
            self.echo_ack_streaming.isChecked(),
            iw_command,
        )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self.disconnect_requested.emit()
        self.worker_thread.quit()
        self.worker_thread.wait(2000)
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
