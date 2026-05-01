from dataclasses import dataclass
from typing import Optional, Any

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
    x: float
    y: float
    z: float
    i: Optional[float] = None
    j: Optional[float] = None
    r: Optional[float] = None

@dataclass
class SetFeed:
    feed: float

@dataclass
class SetUnits:
    units: str

@dataclass
class SetTool:
    tool: int

@dataclass
class SpindleOn:
    clockwise: bool = True

@dataclass
class SpindleOff:
    pass

@dataclass
class ProgramEnd:
    pass

@dataclass
class CommentIR:
    text: str
    is_message: bool = False

# Interpreter
class NistInterpreter:
    def __init__(self):
        self.units = 'mm'           # 'mm' or 'inch' (G21/G20)
        self.absolute = True        # True = G90, False = G91
        self.motion_mode = 'G0'     # G0, G1, G2, G3,

        self.feed: Optional[float] = None
        self.tool: Optional[int] = None
        # see modal groups for whole possibilities

        #  machine position (current)
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0

        self.params: dict[int, float] = {}
        self.expr = ExpressionEvaluator(self.params)

        # modal group mapping
        # self.modal_groups = {}

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
    def _interpret_line(self, line) -> list[Any]:
        ir: list[Any] = []

        if line.block_delete:
            # skip block-delete lines for now, might handle differently later
            return ir

        words_list, comments, pending_parameter_sets = self._read_line(line)

        words = self._group_words(words_list)

        for comment in comments:
            ir.append(CommentIR(comment.text, comment.is_message))

        self._handle_non_motion_words(words, ir)
        self._handle_motion(words, ir)

        # parameter buffering
        # parameters take effect after all values on same line have been evaluated.
        for index, value in pending_parameter_sets:
            self.params[index] = value

        return ir

    def _read_line(self, line: Line):
        words_list: list[tuple[str, float]] = []
        comments: list[Comment] = []
        pending_parameter_sets: list[tuple[int, float]] = []

        for segment in line.segments:
            if isinstance(segment, MidLineWord):
                letter = segment.mid_line_letter.upper()
                value = self.expr.eval(segment.real_value)
                words_list.append((letter, value))

            elif isinstance(segment, ParameterSet):
                index = self.expr.eval_parameter_index(segment.index)
                value = self.expr.eval(segment.value)
                pending_parameter_sets.append((index, value))

            elif isinstance(segment, Comment):
                comments.append(segment)

        return words_list, comments, pending_parameter_sets

    def _group_words(self, words_list: list[tuple[str, float]]) -> dict[str, list[float]]:
        grouped: dict[str, list[float]] = {}

        for letter, value in words_list:
            grouped.setdefault(letter, []).append(value)

        return grouped

    def _last_word(self, words: dict[str, list[float]], letter: str) -> Optional[float]:
        values = words.get(letter.upper())
        if not values:
            return None
        return values[-1]

    def _handle_non_motion_words(self, words: dict[str, list[float]], ir:list[Any]) -> None:
        # F
        f = self._last_word(words, "F")
        if f is not None:
            self.feed = f
            ir.append(SetFeed(f))

        # S word ignored for now, later: SetSpindleSpeed

        # T
        t = self._last_word(words, "F")
        if t is not None:
            self.tool = self._to_int(t, "T")
            ir.append(SetTool(self.tool))

        # G
        for g in words.get("G", []):
            self._handle_g_code(g, ir)

        for m in words.get("M", []):
            self._handle_m_code(m, ir)

    # handle G code that change modal state
    def _handle_g_code(self, gval: float, ir: list[Any]) -> None:
        g = self._normalize_code(gval)

        if g == "G0":
            self.motion_mode = "G0"
        elif g == "G1":
            self.motion_mode = "G1"
        elif g == "G2":
            self.motion_mode = "G2"
        elif g == "G3":
            self.motion_mode = "G3"

        elif g == "G20":
            self.units = "inch"
            ir.append(SetUnits("inch"))
        elif g == "G21":
            self.units = "mm"
            ir.append(SetUnits("mm"))

        elif g == "G90":
            self.absolute = True
        elif g == "G91":
            self.absolute = False

        elif g == "G80":
            self.motion_mode = "G80"

        # else ignore other G codes for now

    def _handle_m_code(self, mval, ir):
        m = self._to_int(mval, "M")

        if m == 3:
            # spindle on
            ir.append(SpindleOn(clockwise=True))
        elif m == 4:
            ir.append(SpindleOn(clockwise=False))
        elif m == 5:
            # spindle off
            ir.append(SpindleOff())
        elif m in (2, 30):
            ir.append(ProgramEnd())
            self.ended = True

    def _handle_motion(self, words: dict[str, list[float]], ir: list[Any]) -> None:
        coords = self._read_coords(words)

        has_axis_motion = any(coords[a] is not None for a in ("X", "Y", "Z"))

        if not has_axis_motion:
            return

        target_x = self.x
        target_y = self.y
        target_z = self.z

        if coords["X"] is not None:
            target_x = coords["X"] if self.absolute else self.x + coords["X"]

        if coords["Y"] is not None:
            target_y = coords["Y"] if self.absolute else self.y + coords["Y"]

        if coords["Z"] is not None:
            target_z = coords["Z"] if self.absolute else self.z + coords["Z"]

        if self.motion_mode == "G0":
            ir.append(RapidMove(target_x, target_y, target_z))
            self._set_position(target_x, target_y, target_z)

        elif self.motion_mode == "G1":
            ir.append(LinearMove(target_x, target_y, target_z, self.feed))
            self._set_position(target_x, target_y, target_z)

        elif self.motion_mode == ("G2", "G3"):
            clockwise = self.motion_mode == "G2"

            i = coords["I"]
            j = coords["J"]
            r = coords["R"]

            if i is None and j is None and r is None:
                raise RuntimeError("Arc move requires I/J or R")

            ir.append(
                ArcMove(
                    clockwise=clockwise,
                    x=target_x,
                    y=target_y,
                    z=target_z,
                    i=i,
                    j=j,
                    r=r,
                )
            )

            self._set_position(target_x, target_y, target_z)

        elif self.motion_mode == "G80":
            raise RuntimeError("Axis words are not allowed while G80 is active")

    def _read_coords(self, words: dict[str, list[float]]) -> dict[str, Optional[float]]:
        coords: dict[str, Optional[float]] = {}

        for letter in ("X", "Y", "Z", "I", "J", "K", "R"):
            value = self._last_word(words, letter)

            if value is not None and self.units == "inch":
                value *= 25.4

            coords[letter] = value

        return coords

    def _set_position(self, x: float, y: float, z: float) -> None:
        self.x = x
        self.y = y
        self.z = z

    def _to_int(self, value: float, name: str) -> int:
        rounded = round(value)

        if abs(value - rounded) > 0.0001:
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
        if abs(value - round(value)) <= 0.0001:
            return f"G{int(round(value))}"

        return f"G{value:g}"