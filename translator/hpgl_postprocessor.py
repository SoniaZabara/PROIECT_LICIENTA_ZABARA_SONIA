import math
from typing import Any

from lpkf_units import M60_STEP_MM, mm_to_m60_steps
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
    # The interpreter already converted inch input to mm.
    # The M60 HP-GL increment is 6.35 mm / 800 steps (7.9375 um/step).
    # Z is represented conceptually by PU/PD instead of a numeric coordinate.

    PA_STEP_MM = M60_STEP_MM
    SURFACE_Z = 0.0
    EPS = 1e-6

    def __init__(self, include_comments: bool = False):
        self.commands: list[str] = []
        self.include_comments = include_comments
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
        self.feed = None
        self.emitted_feed = None
        self.spindle_speed = 0.0
        self.tool_down = False

    def mm_to_pa_units(self, value: float) -> int:
        return mm_to_m60_steps(value)

    def feed_to_m_per_s(self, feed_mm_per_min: float) -> float:
        return feed_mm_per_min / 1000.0 / 60.0

    def emit(self, command: str):
        self.commands.append(command)

    def translate(self, ir: list[Any]) -> str:
        # A postprocessor instance can safely be reused for another job.
        self.commands = []
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
        self.feed = None
        self.emitted_feed = None
        self.spindle_speed = 0.0
        self.tool_down = False

        self.emit("IN;")
        self.emit("!CM1;")  # milling mode
        self.emit("PU;")

        for item in ir:
            self.translate_item(item)

        return "\n".join(self.commands) + "\n"

    def translate_item(self, item: Any):
        if isinstance(item, CommentIR):
            if self.include_comments:
                self.emit(f"/* {item.text} */")

        elif isinstance(item, SetUnits):
            # Interpreter already normalizes inch coordinates to mm
            pass

        elif isinstance(item, SetFeed):
            self.feed = item.feed
            self.emit_feed_if_changed(item.feed)

        elif isinstance(item, SetSpindleSpeed):
            self.spindle_speed = item.speed
            rpm_thousands = max(0, min(60, round(item.speed / 1000)))
            self.emit(f"!RM{rpm_thousands};")

        elif isinstance(item, SpindleOn):
            self.emit("!EM1;")

        elif isinstance(item, SpindleOff):
            self.emit("!EM0;")

        elif isinstance(item, RapidMove):
            if self.is_cutting_z(item.z):
                raise RuntimeError("Rapid move requested below the cutting surface")

            self.reject_ramped_move(item.x, item.y, item.z, "Rapid")
            self.emit_tool_state(False)
            self.emit_xy_if_changed(item.x, item.y)
            self.set_pos(item.x, item.y, item.z)

        elif isinstance(item, LinearMove):
            self.reject_ramped_move(item.x, item.y, item.z, "Linear")
            self.emit_feed_if_changed(item.feed)
            self.emit_tool_state(self.is_cutting_z(item.z))
            self.emit_xy_if_changed(item.x, item.y)
            self.set_pos(item.x, item.y, item.z)

        elif isinstance(item, ArcMove):
            self.reject_ramped_move(item.x, item.y, item.z, "Arc")
            self.emit_feed_if_changed(item.feed)
            self.emit_tool_state(self.is_cutting_z(item.z))
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
            self.emit_tool_state(False)
            self.emit("!EM0;")

        elif isinstance(item, SetTool):
            # G-code T word: selects tool, but does not physically change it!!!
            if self.include_comments:
                self.emit(f"/* Select tool {item.tool} */") # IF you have machine with tool change, REPLACE comment with a command ex: self.emit("ST;")

        elif isinstance(item, ChangeTool):
            # G-code M6: tool change
            # LPKF handling depends on your machine/workflow
            self.emit_tool_state(False)
            self.emit("!EM0;")
            if self.include_comments:
                self.emit(f"/* Change tool to {item.tool} */")

        else:
            raise RuntimeError(f"Unsupported IR command: {item}")

    def is_cutting_z(self, z: float) -> bool:
        return z < self.SURFACE_Z - self.EPS

    def emit_feed_if_changed(self, feed: float | None):
        if feed is None:
            return

        if self.emitted_feed is None or not math.isclose(
            feed, self.emitted_feed, abs_tol=self.EPS
        ):
            self.emit(f"VS{self.feed_to_m_per_s(feed):.6f};")
            self.emitted_feed = feed

    def emit_tool_state(self, tool_down: bool):
        if tool_down == self.tool_down:
            return

        self.emit("PD;" if tool_down else "PU;")
        self.tool_down = tool_down

    def emit_xy_if_changed(self, x: float, y: float):
        if math.isclose(x, self.x, abs_tol=self.EPS) and math.isclose(
            y, self.y, abs_tol=self.EPS
        ):
            return

        xs = self.mm_to_pa_units(x)
        ys = self.mm_to_pa_units(y)
        self.emit(f"PA{xs},{ys};")

    def reject_ramped_move(self, x: float, y: float, z: float, move_name: str):
        xy_changed = not (
            math.isclose(x, self.x, abs_tol=self.EPS)
            and math.isclose(y, self.y, abs_tol=self.EPS)
        )
        z_changed = not math.isclose(z, self.z, abs_tol=self.EPS)

        if xy_changed and z_changed:
            raise RuntimeError(
                f"{move_name} move changes XY and Z simultaneously; "
                "conceptual PU/PD output cannot represent a ramped move"
            )

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

        cx = self.mm_to_pa_units(arc.center_x)
        cy = self.mm_to_pa_units(arc.center_y)

        self.emit(f"AA{cx},{cy},{angle:.6f};")

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
