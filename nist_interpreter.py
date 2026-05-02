from dataclasses import dataclass
from typing import Optional, Any
import math

from nist_transformer import (
    Program,
    Line,
    MidLineWord,
    ParameterSet,
    Comment,
)

from expression_evaluator import ExpressionEvaluator

# IR (Intermediate Representation) dataclasses
@dataclass
class RapidMove:
    x: float
    y: float
    z: float


@dataclass
class LinearMove:
    x: float
    y: float
    z: float
    feed: Optional[float]

@dataclass
class ArcMove:
    clockwise: bool    #clockwise True = G2, False = G3
    plane: str
    x: float
    y: float
    z: float
    center_x: float
    center_y: float
    center_z: float
    rotation: int
    feed: Optional[float]

@dataclass
class Dwell:
    seconds: float

@dataclass
class SetFeed:
    feed: float

@dataclass
class SetSpindleSpeed:
    speed: float

@dataclass
class SetUnits:
    units: str

@dataclass
class SetTool:
    tool: int

@dataclass
class ChangeTool:
    tool: int

@dataclass
class SpindleOn:
    clockwise: bool = True

@dataclass
class SpindleOff:
    pass

@dataclass
class CoolantMistOn:
    pass

@dataclass
class CoolantFloodOn:
    pass

@dataclass
class CoolantOff:
    pass

@dataclass
class ProgramStop:
    optional: bool = False

@dataclass
class ProgramEnd:
    pass

@dataclass
class CommentIR:
    text: str
    is_message: bool = False

# Interpreter
class NistInterpreter:
    EPS = 1e-4 # 0.0001

    G_GROUPS = {
        # modal
        1: {"G0", "G1", "G2", "G3", "G38.2", "G80", "G81", "G82", "G83", "G84", "G85", "G86", "G87", "G88", "G89"}, # motion
        2: {"G17", "G18", "G19"}, # plane selection
        3: {"G90", "G91"}, # distance mode
        5: {"G93", "G94"}, # feed rate mode
        6: {"G20", "G21"}, # units
        7: {"G40", "G41", "G42"}, # cutter radius compensation
        8: {"G43", "G49"}, # tool length offset
        10: {"G98", "G99"}, # return mode in canned cycles
        12: {"G54", "G55", "G56", "G57", "G58", "G59", "G59.1", "G59.2", "G59.3"}, # coordinate sistem selection
        13: {"G61", "G61.1", "G64"}, # path control mode
        # non-modal
        0: {"G4", "G10", "G28", "G30", "G53", "G92", "G92.1", "G92.2", "G92.3"},
    }

    M_GROUPS = {
        4: {0, 1, 2, 30, 60}, # stopping
        6: {6}, # tool change
        7: {3, 4, 5}, # spindle turning
        8: {7, 8, 9}, # coolant (special case: M7 and M8 may be active at the same time)
        9: {48, 49}, # enable/disable feed and speed override switches
    }

    def __init__(self, block_delete_enabled: bool = False):
        self.block_delete_enabled = block_delete_enabled

        self.units = "mm"           # 'mm' or 'inch' (G21/G20)
        self.distance_mode = "absolute" # G90/G91
        self.feed_mode = "units_per_min" # G94/G93
        self.motion_mode = 'G1'     # G0, G1, G2, G3,
        self.plane = "XY"   #G17

        self.feed: Optional[float] = None
        self.spindle_speed: float = 0.0
        self.selected_tool: int = 0
        self.current_tool: int = 0

        #  machine position (current)
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0

        self.params: dict[int, float] = {}
        self.expr = ExpressionEvaluator(self.params)

        self.ended = False

    # accept Program AST -> list of IR
    def interpret(self, program: Program) -> list[Any]:
        output : list[Any] = []

        if program is None:
            return output

        for line in program.lines:
            if self.ended:
                break

            output.extend(self._interpret_line(line))

        return output

    # interpret a single Line AST (_ because is internal)
    def _interpret_line(self, line: Line) -> list[Any]:
        if line.block_delete and self.block_delete_enabled:
            return []

        block = self._read_line(line)
        self._validate_repeats(block)
        self._validate_modal_groups(block)

        ir: list[Any] = []

        if block["comments"]:
            c = block["comments"][-1]
            ir.append(CommentIR(c.text, c.is_message))

        self._exec_feed_mode(block)
        self._exec_feed(block, ir)
        self._exec_spindle_speed(block, ir)
        self._exec_select_tool(block, ir)
        self._exec_tool_change(block, ir)
        self._exec_spindle(block, ir)
        self._exec_coolant(block, ir)
        self._exec_dwell(block, ir)
        self._exec_plane(block)
        self._exec_units(block, ir)
        self._exec_distance_mode(block)

        self._exec_motion(block, ir)
        self._exec_stopping(block, ir)

        # parameter buffering
        # parameters take effect after all values on same line have been evaluated.
        for index, value in block["param_sets"]:
            self.params[index] = value

        return ir

    def _read_line(self, line: Line):
        words_list: list[tuple[str, float]] = []
        comments: list[Comment] = []
        param_sets: list[tuple[int, float]] = []

        for segment in line.segments:
            if isinstance(segment, MidLineWord):
                letter = segment.mid_line_letter.upper()
                value = self.expr.eval(segment.real_value)
                words_list.append((letter, value))

            elif isinstance(segment, ParameterSet):
                index = self.expr.eval_parameter_index(segment.index)
                value = self.expr.eval(segment.value)
                param_sets.append((index, value))

            elif isinstance(segment, Comment):
                comments.append(segment)

        words: dict[str, list[float]] = {}
        for letter, value in words_list:
            words.setdefault(letter, []).append(value)

        return {"words": words,"comments": comments, "param_sets": param_sets}

    def _validate_repeats(self, block: dict[str, Any]) -> None:
        words = block["words"]
        for letter, values in words.items():
            if letter not in {"G", "M"} and len(values) > 1:
                raise RuntimeError(f"Repeated {letter} word on one line")

        if len(words.get("M", [])) > 4:
            raise RuntimeError("More than four M words on one line")

    def _validate_modal_groups(self, block: dict[str, Any]) -> None:
        g_seen: dict[int, str] = {}
        for raw in block["words"].get("G", []):
            g = self._normalize_code(raw)
            group = self._g_group(g)
            if group is None:
                raise RuntimeError(f"Unsupported G code {g}")
            if group != 0 and group in g_seen:
                raise RuntimeError(f"Two G codes from modal group {group}: {g_seen[group]} and {g}")
            if group != 0:
                g_seen[group] = g

        m_seen: dict[int, int] = {}
        for raw in block["words"].get("M", []):
            m = self._to_int(raw, "M")
            group = self._m_group(m)
            if group is None:
                raise RuntimeError(f"Unsupported M code M{m}")
            if group in m_seen:
                # M7 and M8 may both be active
                if not (group == 8 and {m_seen[group], m} == {7, 8}):
                    raise RuntimeError(f"Two M codes from modal group {group}: M{m_seen[group]} and M{m}")
            m_seen[group] = m

    def _exec_feed_mode(self, block):
        for g in self._g_codes(block, group=5):
            if g == "G93":
                self.feed_mode = "inverse_time"
            elif g == "G94":
                self.feed_mode = "units_per_min"

    def _exec_feed(self, block, ir):
        f = self._last(block, "F")
        if f is not None:
            if f < 0:
                raise RuntimeError("Feed rate cannot be negative")
            if self.units == "inch":
                f *= 25.4
            self.feed = f
            ir.append(SetFeed(f))

    def _exec_spindle_speed(self, block, ir):
        s = self._last(block, "S")
        if s is not None:
            if s < 0:
                raise RuntimeError("Spindle speed cannot be negative")
            self.spindle_speed = s
            ir.append(SetSpindleSpeed(s))

    def _exec_select_tool(self, block, ir):
        t = self._last(block, "T")  # fixed: not F
        if t is not None:
            tool = self._to_int(t, "T")
            if tool < 0:
                raise RuntimeError("Tool number cannot be negative")
            self.selected_tool = tool
            ir.append(SetTool(tool))

    def _exec_tool_change(self, block, ir):
        if 6 in self._m_codes(block):
            self.current_tool = self.selected_tool
            ir.append(ChangeTool(self.current_tool))
            ir.append(SpindleOff())

    def _exec_spindle(self, block, ir):
        for m in self._m_codes(block):
            if m == 3:
                ir.append(SpindleOn(clockwise=True))
            elif m == 4:
                ir.append(SpindleOn(clockwise=False))
            elif m == 5:
                ir.append(SpindleOff())

    def _exec_coolant(self, block, ir):
        ms = self._m_codes(block)
        if 7 in ms:
            ir.append(CoolantMistOn())
        if 8 in ms:
            ir.append(CoolantFloodOn())
        if 9 in ms:
            ir.append(CoolantOff())

    def _exec_dwell(self, block, ir):
        if "G4" in self._g_codes(block, group=0):
            p = self._last(block, "P")
            if p is None:
                raise RuntimeError("G4 requires P word")
            if p < 0:
                raise RuntimeError("G4 P value cannot be negative")
            ir.append(Dwell(p))

    def _exec_plane(self, block):
        for g in self._g_codes(block, group=2):
            if g == "G17":
                self.plane = "XY"
            elif g == "G18":
                self.plane = "XZ"
            elif g == "G19":
                self.plane = "YZ"

    def _exec_units(self, block, ir):
        for g in self._g_codes(block, group=6):
            if g == "G20":
                self.units = "inch"
                ir.append(SetUnits("inch"))
            elif g == "G21":
                self.units = "mm"
                ir.append(SetUnits("mm"))

    def _exec_distance_mode(self, block):
        for g in self._g_codes(block, group=3):
            if g == "G90":
                self.distance_mode = "absolute"
            elif g == "G91":
                self.distance_mode = "incremental"

    def _exec_motion(self, block, ir):
        # Update modal motion first.
        for g in self._g_codes(block, group=1):
            self.motion_mode = g

        coords = self._coords(block)
        has_xyz = any(coords[a] is not None for a in ("X", "Y", "Z"))

        if self.motion_mode == "G80":
            if has_xyz:
                raise RuntimeError("Axis words are not allowed while G80 is active")
            return

        if not has_xyz:
            return

        if self.feed_mode == "inverse_time" and self.motion_mode in {"G1", "G2", "G3"} and self._last(block,"F") is None:
            raise RuntimeError("G93 inverse-time mode requires F on every G1/G2/G3 motion line")

        tx = self._target("X", coords)
        ty = self._target("Y", coords)
        tz = self._target("Z", coords)

        if self.motion_mode == "G0":
            ir.append(RapidMove(tx, ty, tz))
            self._set_position(tx, ty, tz)

        elif self.motion_mode == "G1":
            ir.append(LinearMove(tx, ty, tz, self.feed))
            self._set_position(tx, ty, tz)

        elif self.motion_mode in {"G2", "G3"}:
            arc = self._make_arc(coords, tx, ty, tz)
            arc.clockwise = self.motion_mode == "G2"
            arc.feed = self.feed
            ir.append(arc)
            self._set_position(tx, ty, tz)

        else:
            raise RuntimeError(f"Motion mode {self.motion_mode} not implemented yet")

    def _exec_stopping(self, block, ir):
        for m in self._m_codes(block):
            if m == 0:
                ir.append(ProgramStop(optional=False))
            elif m == 1:
                ir.append(ProgramStop(optional=True))
            elif m in {2, 30}:
                ir.append(SpindleOff())
                ir.append(CoolantOff())
                ir.append(ProgramEnd())
                self._reset_after_program_end()
                self.ended = True
            elif m == 60:
                ir.append(ProgramStop(optional=False))

    def _make_arc(self, coords, tx, ty, tz) -> ArcMove:
        if self.plane == "XY":
            start = (self.x, self.y)
            end = (tx, ty)
            offsets = (coords["I"], coords["J"])
            axis_end = tz
            center3 = lambda c1, c2: (c1, c2, tz)
        elif self.plane == "XZ":
            start = (self.x, self.z)
            end = (tx, tz)
            offsets = (coords["I"], coords["K"])
            axis_end = ty
            center3 = lambda c1, c2: (c1, ty, c2)
        else:  # YZ
            start = (self.y, self.z)
            end = (ty, tz)
            offsets = (coords["J"], coords["K"])
            axis_end = tx
            center3 = lambda c1, c2: (tx, c1, c2)

        r = coords["R"]
        if r is not None:
            c1, c2 = self._arc_center_from_radius(start, end, r, clockwise=(self.motion_mode == "G2"))
            rotation = -1 if self.motion_mode == "G2" else 1
        else:
            if offsets[0] is None and offsets[1] is None:
                raise RuntimeError("Center-format arc requires plane offsets: I/J, I/K, or J/K")
            off1 = offsets[0] or 0.0
            off2 = offsets[1] or 0.0
            c1, c2 = start[0] + off1, start[1] + off2
            self._check_arc_radius(start, end, (c1, c2))
            rotation = -1 if self.motion_mode == "G2" else 1

        cx, cy, cz = center3(c1, c2)
        return ArcMove(
            clockwise=(self.motion_mode == "G2"),
            plane=self.plane,
            x=tx,
            y=ty,
            z=tz,
            center_x=cx,
            center_y=cy,
            center_z=cz,
            rotation=rotation,
            feed=self.feed,
        )

    def _arc_center_from_radius(self, start, end, r, clockwise: bool):
        sx, sy = start
        ex, ey = end
        dx, dy = ex - sx, ey - sy
        chord = math.hypot(dx, dy)
        if chord <= self.EPS:
            raise RuntimeError("Radius-format arc endpoint cannot equal current point")
        if abs(r) < chord / 2:
            raise RuntimeError("Arc radius too small for endpoints")

        mx, my = (sx + ex) / 2, (sy + ey) / 2
        h = math.sqrt(max(r * r - (chord / 2) ** 2, 0.0))
        ux, uy = -dy / chord, dx / chord

        # Positive R means <=180 degrees; negative R means >180 degrees.
        sign = -1 if clockwise else 1
        if r < 0:
            sign *= -1

        return mx + sign * ux * h, my + sign * uy * h

    def _check_arc_radius(self, start, end, center):
        r1 = math.hypot(start[0] - center[0], start[1] - center[1])
        r2 = math.hypot(end[0] - center[0], end[1] - center[1])
        tolerance = 0.002 if self.units == "mm" else 0.0002
        if abs(r1 - r2) > tolerance:
            raise RuntimeError(f"Arc radii differ too much: start={r1}, end={r2}")

    def _coords(self, block) -> dict[str, Optional[float]]:
        # here happens unit convertion
        result = {}

        for letter in ("X", "Y", "Z", "I", "J", "K", "R"):
            value = self._last(block, letter)

            if value is not None and self.units == "inch":
                value *= 25.4

            result[letter] = value

        return result

    def _target(self, axis: str, coords: dict[str, Optional[float]]) -> float:
        current = {"X": self.x, "Y": self.y, "Z": self.z}[axis]
        value = coords[axis]
        if value is None:
            return current
        if self.distance_mode == "absolute":
            return value
        return current + value

    def _set_position(self, x: float, y: float, z: float) -> None:
        self.x = x
        self.y = y
        self.z = z

    def _reset_after_program_end(self):
        self.motion_mode = "G1"
        self.plane = "XY"
        self.distance_mode = "absolute"
        self.feed_mode = "units_per_min"

    def _last(self, block, letter: str) -> Optional[float]:
        values = block["words"].get(letter.upper())
        return values[-1] if values else None

    def _g_codes(self, block, group: Optional[int] = None) -> list[str]:
        codes = [self._normalize_code(v) for v in block["words"].get("G", [])]
        if group is None:
            return codes
        return [g for g in codes if self._g_group(g) == group]

    def _m_codes(self, block) -> list[int]:
        return [self._to_int(v, "M") for v in block["words"].get("M", [])]

    def _g_group(self, g: str) -> Optional[int]:
        for group, codes in self.G_GROUPS.items():
            if g in codes:
                return group
        return None

    def _m_group(self, m: int) -> Optional[int]:
        for group, codes in self.M_GROUPS.items():
            if m in codes:
                return group
        return None

    def _to_int(self, value: float, name: str) -> int:
        rounded = round(value)

        if abs(value - rounded) > self.EPS:
            raise RuntimeError(f"{name} value must be close to integer, got {value}")

        return int(rounded)

    def _normalize_code(self, value: float) -> str:
        """
        Converts:
        0 -> G0
        1 -> G1
        38.2 -> G38.2
        59.3 -> G59.3
        """
        if abs(value - round(value)) <= self.EPS:
            return f"G{int(round(value))}"

        return f"G{value:g}"