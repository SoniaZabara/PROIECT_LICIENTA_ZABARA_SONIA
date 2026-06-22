M60_STEP_MM = 6.35 / 800.0


def mm_to_m60_steps(value_mm: float) -> int:
    return round(value_mm / M60_STEP_MM)

