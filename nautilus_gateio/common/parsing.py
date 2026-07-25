"""Small shared helpers for converting Gate.io payload values."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

NANOSECONDS_IN_SECOND = 1_000_000_000
NANOSECONDS_IN_MILLISECOND = 1_000_000

#: Timestamps at or above this magnitude are milliseconds, below it seconds.
#: ``10**12`` seconds is the year 33658 and ``10**12`` milliseconds is 2001, so
#: no real Gate.io timestamp is ambiguous.
MS_THRESHOLD = Decimal(10) ** 12


def to_float(value: Any, default: float = 0.0) -> float:
    """Parse a possibly-``None``/empty Gate.io numeric string into a float."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_decimal(value: Any, default: str = "0") -> Decimal:
    """Parse into ``Decimal`` without going through binary floating point."""
    if value is None or value == "":
        return Decimal(default)
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def secs_to_nanos(value: Any) -> int:
    """Unix seconds (possibly fractional, possibly a string) -> nanoseconds."""
    return int(to_float(value) * NANOSECONDS_IN_SECOND)


def millis_to_nanos(value: Any) -> int:
    """Unix milliseconds -> nanoseconds."""
    return int(to_float(value) * NANOSECONDS_IN_MILLISECOND)


def timestamp_to_nanos(value: Any) -> int:
    """Convert a Gate.io timestamp field to UNIX nanoseconds.

    This is the single conversion for the whole package: Gate.io mixes units even
    within one payload (WebSocket messages carry milliseconds in ``*_ms`` fields
    and fractional seconds elsewhere, and a few REST endpoints report seconds in
    a field whose name says milliseconds), so the magnitude is what disambiguates
    them. The arithmetic is decimal so that a millisecond timestamp survives the
    conversion exactly, which binary floating point cannot guarantee at this
    magnitude.
    """
    number = to_decimal(value)
    if number <= 0:
        return 0
    if number >= MS_THRESHOLD:
        return int(number * NANOSECONDS_IN_MILLISECOND)
    return int(number * NANOSECONDS_IN_SECOND)


def nanos_to_secs(value: int) -> int:
    return int(value // NANOSECONDS_IN_SECOND)


def precision_from_increment(increment: Any) -> int:
    """Derive a decimal precision from a tick/step size such as ``"0.001"``.

    Gate.io publishes some constraints as increments (``order_price_round``) and
    others as scales (``precision``); this normalises the former to the latter.
    """
    text = str(increment or "").strip()
    if not text:
        return 0
    if "e" in text.lower():
        return max(0, -Decimal(text).as_tuple().exponent)
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1].rstrip("0"))
