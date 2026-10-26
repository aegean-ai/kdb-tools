"""The unit table: each unit CURIE with its kind and its factor to the SI unit of that kind.

A Property names its unit; a class constraint states a value in a unit of the same kind. The
table is closed, so a typo in a unit is a finding rather than a silent mismatch.
"""

from __future__ import annotations

from math import pi

UNITS: dict[str, tuple[str, float]] = {
    "unit:RAD": ("angle", 1.0),
    "unit:DEG": ("angle", pi / 180),
    "unit:RAD-PER-SEC": ("angular speed", 1.0),
    "unit:DEG-PER-SEC": ("angular speed", pi / 180),
    "unit:REV-PER-MIN": ("angular speed", 2 * pi / 60),
    "unit:N-M": ("torque", 1.0),
    "unit:N-CentiM": ("torque", 0.01),
    "unit:KiloGM_F-CentiM": ("torque", 0.0980665),
    "unit:N": ("force", 1.0),
    "unit:KiloGM": ("mass", 1.0),
    "unit:GM": ("mass", 1e-3),
    "unit:M": ("length", 1.0),
    "unit:MilliM": ("length", 1e-3),
    "unit:M-PER-SEC": ("speed", 1.0),
    "unit:MilliM-PER-SEC": ("speed", 1e-3),
    "unit:MilliM3": ("volume", 1e-9),
    "unit:CentiM3": ("volume", 1e-6),
    "unit:M3": ("volume", 1.0),
    "unit:KiloGM-PER-M3": ("density", 1.0),
    "unit:GM-PER-CentiM3": ("density", 1000.0),
    "unit:KiloGM-M2": ("inertia", 1.0),
    "unit:GM-CentiM2": ("inertia", 1e-7),
    "unit:A": ("current", 1.0),
    "unit:V": ("voltage", 1.0),
    "unit:W": ("power", 1.0),
    "unit:SEC": ("time", 1.0),
    "unit:MicroSEC": ("time", 1e-6),
    "unit:PERCENT": ("ratio", 0.01),
    "unit:UNITLESS": ("ratio", 1.0),
}

SLACK = 1e-9  # relative, so a value on the edge of its tolerance survives unit conversion


def kind(unit) -> str | None:
    """The unit's kind, or None for a unit that is not in the table."""
    return UNITS[unit][0] if isinstance(unit, str) and unit in UNITS else None


def to_si(value: float, unit: str) -> float:
    return value * UNITS[unit][1]


def check_value(constraint: dict, value: float, unit: str) -> bool:
    """True when value, stated in unit, is within the constraint's tolerance or bounds."""
    cu = constraint["unit"]
    if kind(cu) is None or kind(cu) != kind(unit):
        return False
    v = to_si(value, unit)
    f = UNITS[cu][1]
    if "bounds" in constraint:
        lo, hi = (to_si(x, cu) for x in constraint["bounds"])
        slack = SLACK * max(abs(lo), abs(hi), abs(v))
        return lo - slack <= v <= hi + slack
    nominal, tol = to_si(constraint["nominal"], cu), constraint["tolerance"] * f
    return abs(v - nominal) <= tol + SLACK * max(abs(nominal), abs(v), tol)
