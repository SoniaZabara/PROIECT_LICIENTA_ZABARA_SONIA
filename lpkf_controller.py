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
    #QSpinBox,
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

class SerialWorker(QObject):
    connected = Signal(str)
    disconnected = Signal()
    error = Signal(str)
    log = Signal(str)
    line_received = Signal(str)
    position_received = Signal(float, float, float)
    limits_received = Signal(float, float, float)
    status_received = Signal(str)
    streaming_progress = Signal(int, int)
    streaming_finished = Signal()

    def __init__(self):
        super().__init__()
        self.ser: Optional[serial.Serial] = None
        self._streaming = False
        self._stop_streaming = False

    @Slot(object)
    def connect_port(self, cfg: SerialConfig):
        try:
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
            self.disconnected.emit()
        except Exception as exc:
            self.error.emit(f"Disconnect error: {exc}")

    def _ensure_connected(self) -> bool:
        if not self.ser or not self.ser.is_open:
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
            payload = command.encode("ascii", errors="ignore")
            self.ser.write(payload)
            self.ser.flush()
            self.log.emit(f">> {command}")
            time.sleep(0.05)
            self._read_available()
        except Exception as exc:
            self.error.emit(f"Send error: {exc}")

    @Slot()
    def read_available(self):
        self._read_available()

    def _read_available(self):
        if not self.ser or not self.ser.is_open:
            return

        try:
            chunks: list[bytes] = []
            deadline = time.time() + 0.25
            while time.time() < deadline:
                waiting = self.ser.in_waiting
                if waiting:
                    chunks.append(self.ser.read(waiting))
                    time.sleep(0.03)
                else:
                    time.sleep(0.02)

            if not chunks:
                return

            text = b"".join(chunks).decode("ascii", errors="replace")
            for line in re.split(r"[\r\n]+", text):
                line = line.strip()
                if not line:
                    continue
                self.log.emit(f"<< {line}")
                self.line_received.emit(line)
                self._parse_machine_response(line)
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
                x, y, z = float(nums[0]), float(nums[1]), float(nums[2])
                self.limits_received.emit(x, y, z)
            except ValueError:
                pass

        elif line.startswith("S"):
            self.status_received.emit(line)

    @Slot(list, float)
    def stream_commands(self, commands: list[str], delay_s: float = 0.02):
        if not self._ensure_connected():
            return
        if self._streaming:
            self.error.emit("Already streaming a file.")
            return

        self._streaming = True
        self._stop_streaming = False
        total = len(commands)

        try:
            for index, command in enumerate(commands, start=1):
                if self._stop_streaming:
                    self.log.emit("Streaming interrupted by user.")
                    break

                command = command.strip()
                if not command:
                    continue
                if not command.endswith(";") and not command.endswith(":"):
                    command += ";"

                self.ser.write(command.encode("ascii", errors="ignore"))
                self.ser.flush()
                self.log.emit(f">> {command}")
                self.streaming_progress.emit(index, total)

                # read small responses
                self._read_available()
                time.sleep(delay_s)

            self.streaming_finished.emit()
        except Exception as exc:
            self.error.emit(f"Streaming error: {exc}")
        finally:
            self._streaming = False
            self._stop_streaming = False

    @Slot()
    def request_stop_streaming(self):
        self._stop_streaming = True

def split_hpgl_commands(text: str) -> list[str]:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = text.replace("\n", "").replace("\r", "")
    parts = re.split(r"[;:]", text)
    return [p.strip() for p in parts if p.strip()]


def translate_gcode_to_hpgl(path: Path) -> str:
    parser = NistParser()
    parse_tree = parser.parse(input_path=str(path))

    transformer = NistTransformer()
    ast_tree = transformer.transform(parse_tree)

    interpreter = NistInterpreter()
    ir = interpreter.interpret(ast_tree)

    post = HPGLPostProcessor()
    return post.translate(ir)

class MainWindow(QMainWindow):
    connect_requested = Signal(object)
    disconnect_requested = Signal()
    send_requested = Signal(str)
    read_requested = Signal()
    stream_requested = Signal(list, float)
    stop_stream_requested = Signal()

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
        self.stop_stream_requested.connect(self.worker.request_stop_streaming)

        self.worker.connected.connect(self.on_connected)
        self.worker.disconnected.connect(self.on_disconnected)
        self.worker.error.connect(self.show_error)
        self.worker.log.connect(self.append_log)
        self.worker.position_received.connect(self.update_position)
        self.worker.limits_received.connect(self.update_limits)
        self.worker.status_received.connect(self.update_machine_status)
        self.worker.streaming_progress.connect(self.update_progress)
        self.worker.streaming_finished.connect(self.on_stream_finished)

        self.hpgl_commands: list[str] = []
        self._build_ui()
        self._build_menu()
        self.refresh_ports()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(lambda: self.read_requested.emit())
        self.poll_timer.start(1000)

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

        spindle_on = QPushButton("Spindle ON !EM1;")
        spindle_off = QPushButton("Spindle OFF !EM0;")
        stop_btn = QPushButton("Pause !ST;")
        go_btn = QPushButton("Resume !GO;")
        clear_btn = QPushButton("Clear buffer !CB;")
        init_btn = QPushButton("Initialize IN;")

        spindle_on.clicked.connect(lambda: self.confirm_and_send("Turn spindle ON?", "!EM1;"))
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
        self.stream_delay = QDoubleSpinBox()
        self.stream_delay.setRange(0.0, 2.0)
        self.stream_delay.setDecimals(3)
        self.stream_delay.setValue(0.02)
        self.stream_delay.setSuffix(" s delay")
        stream_btn = QPushButton("Stream file")
        stream_btn.clicked.connect(self.stream_file)
        stop_stream_btn = QPushButton("Stop streaming")
        stop_stream_btn.clicked.connect(lambda: self.stop_stream_requested.emit())
        file_buttons.addWidget(load_gcode_btn)
        file_buttons.addWidget(load_btn)
        file_buttons.addWidget(self.stream_delay)
        file_buttons.addWidget(stream_btn)
        file_buttons.addWidget(stop_stream_btn)

        self.preview = QPlainTextEdit()
        self.preview.setPlaceholderText("Loaded or translated HP-GL commands will appear here.")
        self.preview.setFont(QFont("Courier New", 10))
        file_layout.addLayout(file_buttons)
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

    def update_position(self, x: float, y: float, z: float):
        self.x_label.setText(f"X: {x:g}")
        self.y_label.setText(f"Y: {y:g}")
        # Z is intentionally not updated in the interface
        # because spindle motion tool is up/down only
        self.z_label.setText("Z: —")

    def update_limits(self, x: float, y: float, z: float):
        self.limits_label.setText(f"Limits: X {x:g}, Y {y:g}, Z {z:g}")

    def update_machine_status(self, status: str):
        self.status_label.setText(f"Status: {status}")

    def update_progress(self, index: int, total: int):
        self.progress_label.setText(f"File: {index}/{total} commands")

    def on_stream_finished(self) -> None:
        self.append_log("Streaming finished.")
        self.statusBar().showMessage("Streaming finished", 5000)

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
            hpgl = translate_gcode_to_hpgl(path)
            self.hpgl_commands = split_hpgl_commands(hpgl)
            self.preview.setPlainText(";\n".join(self.hpgl_commands) + (";" if self.hpgl_commands else ""))
            self.progress_label.setText(f"File: {len(self.hpgl_commands)} translated commands")
            self.append_log(f"Translated {path.name}: {len(self.hpgl_commands)} HP-GL commands.")
        except Exception as exc:
            self.show_error(f"Could not translate G-code file: {exc}")

    def stream_file(self) -> None:
        if not self.hpgl_commands:
            self.show_error("No HP-GL job loaded.")
            return

        answer = QMessageBox.question(
            self,
            "Start streaming?",
            "Start sending the loaded HP-GL file to the machine?\n\n"
            "Make sure the tool is raised, the work area is clear, and the spindle state is correct.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.stream_requested.emit(self.hpgl_commands, float(self.stream_delay.value()))

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

