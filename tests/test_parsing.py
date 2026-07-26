"""Tests for the shared payload conversion helpers in ``common/parsing.py``.

The timestamp helper is the single conversion point for the whole package:
Gate.io mixes seconds and milliseconds even within one payload, so magnitude is
what disambiguates them, and the arithmetic must be decimal so that a
millisecond timestamp survives the conversion exactly.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

import nautilus_gateio
from nautilus_gateio.common.parsing import (
    MS_THRESHOLD,
    NANOSECONDS_IN_MILLISECOND,
    NANOSECONDS_IN_SECOND,
    millis_to_nanos,
    nanos_to_secs,
    precision_from_increment,
    secs_to_nanos,
    timestamp_to_nanos,
    to_decimal,
    to_float,
    to_int,
)


class TestToFloat:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("1.5", 1.5),
            (1.5, 1.5),
            (2, 2.0),
            ("0", 0.0),
            ("-3.25", -3.25),
            ("1e-8", 1e-8),
        ],
    )
    def test_parses_numeric_values(self, value, expected):
        assert to_float(value) == expected

    @pytest.mark.parametrize("value", [None, "", "not-a-number", [], {}])
    def test_falls_back_to_the_default(self, value):
        assert to_float(value) == 0.0
        assert to_float(value, default=7.5) == 7.5


class TestToInt:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("5", 5), (5, 5), ("5.9", 5), (5.9, 5), ("-5.9", -5), ("0", 0)],
    )
    def test_parses_and_truncates_toward_zero(self, value, expected):
        assert to_int(value) == expected

    @pytest.mark.parametrize("value", [None, "", "abc"])
    def test_falls_back_to_the_default(self, value):
        assert to_int(value) == 0
        assert to_int(value, default=-1) == -1


class TestToDecimal:
    def test_avoids_binary_floating_point_error(self):
        """``Decimal(str(...))`` is what keeps 0.1 exactly 0.1."""
        assert to_decimal("0.1") == Decimal("0.1")
        assert to_decimal("0.1") + to_decimal("0.2") == Decimal("0.3")

    @pytest.mark.parametrize("value", [None, "", "abc"])
    def test_falls_back_to_the_default(self, value):
        assert to_decimal(value) == Decimal(0)
        assert to_decimal(value, default="42") == Decimal(42)

    def test_preserves_the_published_scale(self):
        assert str(to_decimal("1.10")) == "1.10"


class TestFixedUnitConversions:
    def test_seconds_to_nanoseconds(self):
        assert secs_to_nanos("1") == NANOSECONDS_IN_SECOND
        assert secs_to_nanos(1.5) == 1_500_000_000

    def test_milliseconds_to_nanoseconds(self):
        assert millis_to_nanos("1") == NANOSECONDS_IN_MILLISECOND
        assert millis_to_nanos(1500) == 1_500_000_000

    def test_nanoseconds_back_to_seconds(self):
        assert nanos_to_secs(1_500_000_000) == 1
        assert nanos_to_secs(999_999_999) == 0


class TestTimestampToNanos:
    """Magnitude selects the unit: below 10**12 is seconds, at or above is milliseconds."""

    def test_threshold_value(self):
        assert MS_THRESHOLD == Decimal(10) ** 12

    @pytest.mark.parametrize("value", [1700000000, "1700000000", 1700000000.0])
    def test_unix_seconds_are_scaled_by_a_billion(self, value):
        assert timestamp_to_nanos(value) == 1_700_000_000_000_000_000

    @pytest.mark.parametrize("value", [1700000000123, "1700000000123"])
    def test_unix_milliseconds_are_scaled_by_a_million(self, value):
        assert timestamp_to_nanos(value) == 1_700_000_000_123_000_000

    def test_fractional_seconds_keep_their_sub_second_part(self):
        assert timestamp_to_nanos("1700000000.123456") == 1_700_000_000_123_456_000

    def test_millisecond_timestamps_convert_exactly(self):
        """Binary floating point cannot guarantee this at millisecond magnitude."""
        for offset in range(1000):
            millis = 1_700_000_000_000 + offset
            assert timestamp_to_nanos(millis) == millis * NANOSECONDS_IN_MILLISECOND

    def test_seconds_and_milliseconds_for_the_same_instant_agree(self):
        assert timestamp_to_nanos(1700000000) == timestamp_to_nanos(1700000000000)

    @pytest.mark.parametrize("value", [None, "", 0, "0", -1, "-5"])
    def test_missing_or_non_positive_values_become_zero(self, value):
        assert timestamp_to_nanos(value) == 0

    def test_values_either_side_of_the_threshold(self):
        just_below = int(MS_THRESHOLD) - 1
        just_at = int(MS_THRESHOLD)
        assert timestamp_to_nanos(just_below) == just_below * NANOSECONDS_IN_SECOND
        assert timestamp_to_nanos(just_at) == just_at * NANOSECONDS_IN_MILLISECOND

    def test_result_is_always_an_int(self):
        for value in ("1700000000", 1700000000123, "1700000000.5"):
            assert isinstance(timestamp_to_nanos(value), int)


class TestPrecisionFromIncrement:
    @pytest.mark.parametrize(
        ("increment", "expected"),
        [
            ("1", 0),
            ("0.1", 1),
            ("0.01", 2),
            ("0.001", 3),
            ("0.00000001", 8),
            ("10", 0),
            ("0.5", 1),
        ],
    )
    def test_decimal_increments(self, increment, expected):
        assert precision_from_increment(increment) == expected

    @pytest.mark.parametrize(
        ("increment", "expected"),
        [("1e-8", 8), ("1E-8", 8), ("1e-2", 2), ("1e0", 0)],
    )
    def test_scientific_notation(self, increment, expected):
        assert precision_from_increment(increment) == expected

    def test_trailing_zeros_do_not_inflate_the_precision(self):
        assert precision_from_increment("0.100") == 1
        assert precision_from_increment("1.000") == 0

    @pytest.mark.parametrize("increment", [None, "", "   "])
    def test_missing_increment_is_zero_precision(self, increment):
        assert precision_from_increment(increment) == 0

    def test_numeric_input_is_accepted(self):
        assert precision_from_increment(0.001) == 3
        assert precision_from_increment(1) == 0


class TestSingleCanonicalTimestampConversion:
    """Regression for seam-07 / SEAM-02: the conversion existed twice.

    ``data.py`` carried its own copy that computed in binary floating point while
    ``common/parsing.py`` computed in ``Decimal``. Both were correct-looking and
    the suite was green, because the tests only ever exercised the canonical one.
    The two disagreed by 64 ns on millisecond timestamps, so the same venue
    instant became two different values depending on whether it arrived on the
    data path or the execution path.
    """

    def test_the_package_defines_it_exactly_once(self):
        """A second definition is the defect itself, so assert against the tree."""
        package = Path(nautilus_gateio.__file__).resolve().parent
        definitions = [
            f"{path.relative_to(package)}:{index}"
            for path in sorted(package.rglob("*.py"))
            for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if line.startswith("def timestamp_to_nanos")
        ]
        assert definitions == ["common/parsing.py:56"], definitions

    def test_every_module_uses_the_canonical_one(self):
        """Importing it is fine; redefining it is not."""
        from nautilus_gateio import data, execution
        from nautilus_gateio.common import parsing

        assert data.timestamp_to_nanos is parsing.timestamp_to_nanos
        assert execution.timestamp_to_nanos is parsing.timestamp_to_nanos

    @pytest.mark.parametrize(
        "millis",
        [1700000000123, 1790000000123, 1790000000999, 1785555555555],
    )
    def test_millisecond_timestamps_are_exact_on_every_path(self, millis):
        """The measured 64 ns divergence: the float implementation failed this."""
        from nautilus_gateio import data

        assert data.timestamp_to_nanos(millis) == millis * 1_000_000

    def test_the_data_path_and_the_execution_path_agree(self):
        """The property that actually matters: one instant, one value."""
        from nautilus_gateio import data, execution

        for value in (1790000000123, "1790000000123", 1790000000.123456, 1790000000):
            assert data.timestamp_to_nanos(value) == execution.timestamp_to_nanos(value)
