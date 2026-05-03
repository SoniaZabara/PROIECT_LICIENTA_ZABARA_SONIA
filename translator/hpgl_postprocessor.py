import math
from typing import Any

from translator.nist_interpreter import (
    RapidMove,
    LinearMove,
    ArcMove,
    Dwell,
    SetFeed,
    SetSpindleSpeed,
    SetUnits,
    SetTool,
    ChangeTool,
    SpindleOn,
    SpindleOff,
    CoolantMistOn,
    CoolantFloodOn,
    CoolantOff,
    ProgramStop,
    ProgramEnd,
    CommentIR,
)

class HPGLPostProcessor:
    # Converts interpreter Intermediate Representation (IR) into LPKF HP-GL

    # Assumption:
    ### The interpreter already converted inch input to mm
    ### Coordinates are emitted in LPKF step units !!!!!!!!!

    STEP_MM = 0.0079375 # 7.9375 micrometers / step

    def __init__(self):
        self.commands: list[str] = []
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
        self.feed = None
        self.spindle_speed = 0.0

    def mm_to_steps(self, value: float) -> int:
        return round(value / self.STEP_MM)

    def feed_to_m_per_s(self, feed_mm_per_min: float) -> float:
        return feed_mm_per_min / 1000.0 / 60.0

    def emit(self, command: str):
        self.commands.append(command)

    def translate(self, ir: list[Any]) -> str:
        self.emit("IN;")
        self.emit("!CM1;")  # milling mode
        self.emit("PU;")

        for item in ir:
            self.translate_item(item)

        return "\n".join(self.commands) + "\n"

    def translate_item(self, item: Any):
        if isinstance(item, CommentIR):
            self.emit(f"/* {item.text} */")

        elif isinstance(item, SetUnits):
            # Interpreter already normalizes inch coordinates to mm
            pass

        elif isinstance(item, SetFeed):
            self.feed = item.feed
            self.emit(f"VS{self.feed_to_m_per_s(item.feed):.6f};")

        elif isinstance(item, SetSpindleSpeed):
            self.spindle_speed = item.speed
            rpm_thousands = max(0, min(60, round(item.speed / 1000)))
            self.emit(f"!RM{rpm_thousands};")

        elif isinstance(item, SpindleOn):
            self.emit("!EM1;")

        elif isinstance(item, SpindleOff):
            self.emit("!EM0;")

        elif isinstance(item, RapidMove):
            self.emit("PU;")
            self.emit_ta(item.x, item.y, item.z)
            self.set_pos(item.x, item.y, item.z)

        elif isinstance(item, LinearMove):
            if item.feed is not None:
                self.emit(f"VS{self.feed_to_m_per_s(item.feed):.6f};")
            self.emit("PD;")
            self.emit_ta(item.x, item.y, item.z)
            self.set_pos(item.x, item.y, item.z)

        elif isinstance(item, ArcMove):
            self.emit_arc(item)
            self.set_pos(item.x, item.y, item.z)

        elif isinstance(item, Dwell):
            self.emit(f"!TW{round(item.seconds * 1000)};")

        elif isinstance(item, (CoolantMistOn, CoolantFloodOn, CoolantOff)):
            # LPKF command depends on hardware wiring, so this not apply for our model
            pass

        elif isinstance(item, ProgramStop):
            self.emit("!ST;")

        elif isinstance(item, ProgramEnd):
            self.emit("PU;")
            self.emit("!EM0;")

        elif isinstance(item, SetTool):
            # G-code T word: selects tool, but does not physically change it!!!
            self.emit(f"/* Select tool {item.tool} */") # IF you have machine with tool change, REPLACE comment with a command ex: self.emit("ST;")

        elif isinstance(item, ChangeTool):
            # G-code M6: tool change
            # LPKF handling depends on your machine/workflow
            self.emit("PU;")
            self.emit("!EM0;")
            self.emit(f"/* Change tool to {item.tool} */")

        else:
            raise RuntimeError(f"Unsupported IR command: {item}")

    def emit_ta(self, x: float, y: float, z: float):
        xs = self.mm_to_steps(x)
        ys = self.mm_to_steps(y)
        zs = self.mm_to_steps(z)
        self.emit(f"!TA{xs},{ys},{zs};")

    def emit_arc(self, arc: ArcMove):
        if arc.plane != "XY":
            raise RuntimeError("LPKF AA supports XY arcs directly. XZ/YZ arcs must be approximated.")

        angle = self.arc_sweep_degrees(
            start_x=self.x,
            start_y=self.y,
            end_x=arc.x,
            end_y=arc.y,
            center_x=arc.center_x,
            center_y=arc.center_y,
            clockwise=arc.clockwise,
            rotation=arc.rotation,
        )

        cx = self.mm_to_steps(arc.center_x)
        cy = self.mm_to_steps(arc.center_y)

        self.emit("PD;")
        self.emit(f"AA{cx},{cy},{angle:.6f};")

        if abs(arc.z - self.z) > 1e-9:
            self.emit(f"!ZA{self.mm_to_steps(arc.z)};")

    def arc_sweep_degrees(
            self,
            start_x: float,
            start_y: float,
            end_x: float,
            end_y: float,
            center_x: float,
            center_y: float,
            clockwise: bool,
            rotation: int,
    ) -> float:
        a0 = math.degrees(math.atan2(start_y - center_y, start_x - center_x))
        a1 = math.degrees(math.atan2(end_y - center_y, end_x - center_x))

        if clockwise:
            sweep = a1 - a0
            if sweep >= 0:
                sweep -= 360
        else:
            sweep = a1 - a0
            if sweep <= 0:
                sweep += 360

        # HP-GL AA: negative angle = clockwise, positive = counterclockwise
        return sweep

    def set_pos(self, x: float, y: float, z: float):
        self.x = x
        self.y = y
        self.z = z