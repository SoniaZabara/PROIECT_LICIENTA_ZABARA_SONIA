import configparser
from dataclasses import dataclass
from pathlib import Path

from lpkf_units import mm_to_m60_steps

CALIBRATED_HOME_OFFSET_MM = 30.0
CALIBRATED_HOME_OFFSET_UNITS = mm_to_m60_steps(CALIBRATED_HOME_OFFSET_MM)


@dataclass(frozen=True)
class XYPosition:
    x: float
    y: float


@dataclass(frozen=True)
class HardClipLimits:
    xmin: float
    ymin: float
    zmin: float
    xmax: float
    ymax: float
    zmax: float

    def validate(self) -> None:
        if self.xmax <= self.xmin:
            raise ValueError("Xmax must be greater than Xmin")
        if self.ymax <= self.ymin:
            raise ValueError("Ymax must be greater than Ymin")
        if self.zmax < self.zmin:
            raise ValueError("Zmax must not be less than Zmin")

    def contains_xy(self, position: XYPosition) -> bool:
        return (
            self.xmin <= position.x <= self.xmax
            and self.ymin <= position.y <= self.ymax
        )

    def positions(self) -> dict[str, XYPosition]:
        default_home = XYPosition(self.xmin, self.ymax / 2.0)
        return {
            "pause": XYPosition(self.xmax, self.ymax),
            "default_home": default_home,
            "calibrated_home": XYPosition(
                default_home.x + CALIBRATED_HOME_OFFSET_UNITS,
                default_home.y,
            ),
            "zero": XYPosition(self.xmin, self.ymin),
        }


def hardclip_from_oh_values(values: list[float]) -> HardClipLimits:
    if len(values) >= 6:
        limits = HardClipLimits(*values[:6])
    elif len(values) >= 3:
        xmax, ymax, zmax = values[:3]
        limits = HardClipLimits(0.0, 0.0, 0.0, xmax, ymax, zmax)
    else:
        raise ValueError("OH response must contain either three or six coordinates")

    limits.validate()
    return limits


@dataclass(frozen=True)
class OperatingWindow:
    xmin: float
    ymin: float
    xmax: float
    ymax: float

    def validate(self, hardclip: HardClipLimits) -> None:
        if self.xmax <= self.xmin or self.ymax <= self.ymin:
            raise ValueError("IW maximum coordinates must be greater than minimum coordinates")
        for position in (
            XYPosition(self.xmin, self.ymin),
            XYPosition(self.xmax, self.ymax),
        ):
            if not hardclip.contains_xy(position):
                raise ValueError("IW window must stay inside the hardclip limits")


class LPKFIni:
    def __init__(self, path: Path):
        self.path = path

    def load_limits(self) -> HardClipLimits | None:
        parser = self._read()
        if not parser.has_section("hardclip"):
            return None

        limits = HardClipLimits(
            xmin=parser.getfloat("hardclip", "xmin"),
            ymin=parser.getfloat("hardclip", "ymin"),
            zmin=parser.getfloat("hardclip", "zmin"),
            xmax=parser.getfloat("hardclip", "xmax"),
            ymax=parser.getfloat("hardclip", "ymax"),
            zmax=parser.getfloat("hardclip", "zmax"),
        )
        limits.validate()
        return limits

    def save_limits(self, limits: HardClipLimits) -> None:
        limits.validate()
        parser = self._read()
        parser["hardclip"] = {
            "xmin": format_number(limits.xmin),
            "ymin": format_number(limits.ymin),
            "zmin": format_number(limits.zmin),
            "xmax": format_number(limits.xmax),
            "ymax": format_number(limits.ymax),
            "zmax": format_number(limits.zmax),
        }

        parser["positions"] = {
            f"{name}_{axis}": format_number(getattr(position, axis))
            for name, position in limits.positions().items()
            for axis in ("x", "y")
        }
        parser["positions"]["calibrated_home_offset_mm"] = format_number(
            CALIBRATED_HOME_OFFSET_MM
        )
        self._write(parser)

    def load_window(self) -> OperatingWindow | None:
        parser = self._read()
        if not parser.has_section("operating_window"):
            return None
        return OperatingWindow(
            xmin=parser.getfloat("operating_window", "xmin"),
            ymin=parser.getfloat("operating_window", "ymin"),
            xmax=parser.getfloat("operating_window", "xmax"),
            ymax=parser.getfloat("operating_window", "ymax"),
        )

    def save_window(self, window: OperatingWindow) -> None:
        parser = self._read()
        parser["operating_window"] = {
            "xmin": format_number(window.xmin),
            "ymin": format_number(window.ymin),
            "xmax": format_number(window.xmax),
            "ymax": format_number(window.ymax),
        }
        self._write(parser)

    def _read(self) -> configparser.ConfigParser:
        parser = configparser.ConfigParser()
        if self.path.exists():
            parser.read(self.path, encoding="utf-8")
        return parser

    def _write(self, parser: configparser.ConfigParser) -> None:
        with self.path.open("w", encoding="utf-8") as handle:
            parser.write(handle)


def format_number(value: float) -> str:
    return f"{value:g}"
