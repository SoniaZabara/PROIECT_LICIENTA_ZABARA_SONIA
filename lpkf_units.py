import math


M60_STEP_MM = 6.35 / 800.0


def round_half_away_from_zero(value: float) -> int:
    """Round a machine parameter to the nearest integer deterministically."""
    if not math.isfinite(value):
        raise ValueError("M60 numeric values must be finite")
    if value >= 0:
        return math.floor(value + 0.5)
    return math.ceil(value - 0.5)


def mm_to_m60_steps(value_mm: float) -> int:
    return round_half_away_from_zero(value_mm / M60_STEP_MM)
