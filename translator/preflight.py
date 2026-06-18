from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Optional

from translator.expression_evaluator import ExpressionEvaluator
from translator.nist_parser import NistParser
from translator.nist_transformer import (
    Comment,
    Line,
    MidLineWord,
    NistTransformer,
    ParameterSet,
)


@dataclass
class PreflightIssue:
    severity: str
    message: str
    line_number: Optional[int] = None

    def format(self) -> str:
        prefix = self.severity
        if self.line_number is not None:
            return f"{prefix}: line {self.line_number}: {self.message}"
        return f"{prefix}: {self.message}"


@dataclass
class PreflightReport:
    source: str
    issues: list[PreflightIssue] = field(default_factory=list)

    def add(self, severity: str, message: str, line_number: Optional[int] = None) -> None:
        self.issues.append(PreflightIssue(severity, message, line_number))

    def has_blocks(self) -> bool:
        return any(issue.severity == "BLOCK" for issue in self.issues)

    def warnings(self) -> list[PreflightIssue]:
        return [issue for issue in self.issues if issue.severity == "WARN"]

    def text(self) -> str:
        if not self.issues:
            return f"Preflight OK: {self.source}"

        lines = [f"Preflight report: {self.source}"]
        lines.extend(issue.format() for issue in self.issues)
        return "\n".join(lines)


class GCodePreflight:
    LOW_FEED_MM_MIN = 1.0
    HIGH_FEED_MM_MIN = 1000.0
    LOW_SPINDLE_RPM = 1000.0
    HIGH_SPINDLE_RPM = 60000.0

    BLOCK_G_CODES = {
        "G38.2": "Probing cycle G38.2 is not supported.",
        "G41": "Cutter compensation G41 is not implemented.",
        "G42": "Cutter compensation G42 is not implemented.",
        "G43": "Tool length offset G43 is not implemented.",
        "G10": "Coordinate/offset command G10 is not implemented.",
        "G28": "Reference return G28 is not implemented.",
        "G30": "Reference return G30 is not implemented.",
        "G53": "Machine-coordinate move G53 is not implemented.",
    }
    CANNED_CYCLES = {f"G{i}" for i in range(81, 90)}
    COORDINATE_SYSTEMS = {
        "G54",
        "G55",
        "G56",
        "G57",
        "G58",
        "G59",
        "G59.1",
        "G59.2",
        "G59.3",
    }
    ALLOWED_G_CODES = {
        "G0",
        "G1",
        "G2",
        "G3",
        "G4",
        "G17",
        "G18",
        "G19",
        "G20",
        "G21",
        "G40",
        "G49",
        "G80",
        "G90",
        "G91",
        "G93",
        "G94",
    }
    ALLOWED_M_CODES = {2, 30}
    WARN_M_CODES = {0, 1, 3, 4, 5, 6, 7, 8, 9, 60}

    def __init__(self):
        self.params: dict[int, float] = {}
        self.expr = ExpressionEvaluator(self.params)
        self.units = "mm"
        self.plane = "XY"

    def check_file(self, path: Path) -> PreflightReport:
        report = PreflightReport(source=f"G-code {path.name}")
        program = NistParser().parse(input_path=str(path))
        ast = NistTransformer().transform(program)

        for physical_line, line in enumerate(ast.lines, start=1):
            self._check_line(line, report, physical_line)

        return report

    def _check_line(self, line: Line, report: PreflightReport, physical_line: int) -> None:
        words, param_sets = self._read_line(line)
        line_number = line.line_number or physical_line

        g_codes = [self._normalize_g(value) for value in words.get("G", [])]
        m_codes = [self._to_int(value) for value in words.get("M", [])]

        if "Z" in words:
            report.add(
                "BLOCK",
                "Numeric Z motion is not allowed for this pen-up/pen-down M60 profile.",
                line_number,
            )

        for g in g_codes:
            if g in self.CANNED_CYCLES:
                report.add("BLOCK", f"Canned cycle {g} is not supported.", line_number)
            elif g in self.COORDINATE_SYSTEMS:
                report.add("BLOCK", f"Coordinate system {g} is not implemented.", line_number)
            elif g.startswith("G92"):
                report.add("BLOCK", f"Coordinate offset {g} is not implemented.", line_number)
            elif g in self.BLOCK_G_CODES:
                report.add("BLOCK", self.BLOCK_G_CODES[g], line_number)
            elif g not in self.ALLOWED_G_CODES:
                report.add("BLOCK", f"{g} is not allowed by this M60 machine profile.", line_number)

        line_plane = self.plane
        for g in g_codes:
            if g == "G17":
                line_plane = "XY"
            elif g == "G18":
                line_plane = "XZ"
            elif g == "G19":
                line_plane = "YZ"

        if line_plane != "XY" and any(g in {"G2", "G3"} for g in g_codes):
            report.add("BLOCK", f"Arc motion in {line_plane} plane is not supported.", line_number)

        if "T" in words:
            report.add("WARN", "Tool select T... has no automatic effect on this machine.", line_number)

        if 6 in m_codes:
            report.add("WARN", "M6 requires manual tool change and operator confirmation.", line_number)

        for m in m_codes:
            if m in {3, 4, 5}:
                report.add("WARN", f"Spindle command M{m} requires operator awareness.", line_number)
            elif m in {7, 8, 9}:
                report.add("WARN", f"Coolant command M{m} is probably ignored/no hardware.", line_number)
            elif m in {0, 1, 60}:
                report.add("WARN", f"Program stop M{m} requires operator awareness.", line_number)
            elif m not in self.ALLOWED_M_CODES and m not in self.WARN_M_CODES:
                report.add("BLOCK", f"M{m} is not allowed by this M60 machine profile.", line_number)

        feed = self._last(words, "F")
        if feed is not None:
            feed_mm_min = feed * 25.4 if self.units == "inch" else feed
            if feed_mm_min < self.LOW_FEED_MM_MIN:
                report.add("WARN", f"Very low feed rate F{feed:g}.", line_number)
            elif feed_mm_min > self.HIGH_FEED_MM_MIN:
                report.add("WARN", f"Very high feed rate F{feed:g}.", line_number)

        spindle = self._last(words, "S")
        if spindle is not None:
            report.add("WARN", f"Spindle speed S{spindle:g} will be mapped to !RM.", line_number)
            if spindle < self.LOW_SPINDLE_RPM:
                report.add("WARN", f"Very low spindle speed S{spindle:g}.", line_number)
            elif spindle > self.HIGH_SPINDLE_RPM:
                report.add("WARN", f"Very high spindle speed S{spindle:g}.", line_number)

        for g in g_codes:
            if g == "G20":
                self.units = "inch"
            elif g == "G21":
                self.units = "mm"
            elif g == "G17":
                self.plane = "XY"
            elif g == "G18":
                self.plane = "XZ"
            elif g == "G19":
                self.plane = "YZ"

        for index, value in param_sets:
            self.params[index] = value

    def _read_line(self, line: Line) -> tuple[dict[str, list[float]], list[tuple[int, float]]]:
        words: dict[str, list[float]] = {}
        param_sets: list[tuple[int, float]] = []

        for segment in line.segments:
            if isinstance(segment, MidLineWord):
                letter = segment.mid_line_letter.upper()
                value = self.expr.eval(segment.real_value)
                words.setdefault(letter, []).append(value)
            elif isinstance(segment, ParameterSet):
                index = self.expr.eval_parameter_index(segment.index)
                value = self.expr.eval(segment.value)
                param_sets.append((index, value))
            elif isinstance(segment, Comment):
                pass

        return words, param_sets

    def _normalize_g(self, value: float) -> str:
        if abs(value - round(value)) <= 1e-4:
            return f"G{int(round(value))}"
        return f"G{value:g}"

    def _to_int(self, value: float) -> int:
        return int(round(value))

    def _last(self, words: dict[str, list[float]], letter: str) -> Optional[float]:
        values = words.get(letter.upper())
        return values[-1] if values else None


def preflight_gcode_file(path: Path) -> PreflightReport:
    return GCodePreflight().check_file(path)


def preflight_hpgl_commands(commands: list[str], source: str = "HP-GL job") -> PreflightReport:
    report = PreflightReport(source=source)

    for index, command in enumerate(commands, start=1):
        normalized = re.sub(r"\s+", "", command).upper()
        if not normalized:
            continue

        if normalized.startswith("!TA"):
            values = _numbers(normalized)
            if len(values) >= 3 and values[2] != 0:
                report.add("BLOCK", f"!TA command has nonzero Z value: {command}", index)
        elif normalized.startswith(("!ZA", "!ZR", "!TR")):
            report.add("BLOCK", f"Numeric Z/3D command is not allowed: {command}", index)
        elif normalized.startswith("!EM"):
            report.add("WARN", f"Spindle motor command requires operator awareness: {command}", index)
        elif normalized.startswith("!RM"):
            values = _numbers(normalized)
            report.add("WARN", f"Spindle speed command will set high-frequency spindle speed: {command}", index)
            if values and (values[0] < 1 or values[0] > 60):
                report.add("WARN", f"!RM value outside expected 1..60 range: {command}", index)

    return report


def _numbers(text: str) -> list[int]:
    return [int(value) for value in re.findall(r"[-+]?\d+", text)]
