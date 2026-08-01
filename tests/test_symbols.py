"""Tests for the minimum-normalization symbology contract.

The contract under test (``gateio_nt.common.symbols``):

======================  ============================================  ============================
Product                 Instrument id                                 ``raw_symbol``
======================  ============================================  ============================
Spot                    ``BTC_USDT.GATE_IO``                          ``BTC_USDT``
Perpetual (linear)      ``BTC_USDT-PERP.GATE_IO``                     ``BTC_USDT``
Perpetual (inverse)     ``BTC_USD-PERP.GATE_IO``                      ``BTC_USD``
Delivery future         ``BTC_USDT_20260807.GATE_IO``                 ``BTC_USDT_20260807``
Option                  ``BTC_USDT-20260729-70000-C.GATE_IO``         ``BTC_USDT-20260729-70000-C``
======================  ============================================  ============================

``-PERP`` is the only suffix the adapter may add, and only to perpetuals.
Every other symbol must round trip unchanged.
"""

from __future__ import annotations

import pytest
from nautilus_trader.model.identifiers import InstrumentId

from gateio_nt.common.constants import GATEIO, GATEIO_VENUE
from gateio_nt.common.enums import GateioProductType
from gateio_nt.common.symbols import (
    PERP_SUFFIX,
    gateio_to_instrument_id,
    instrument_id_to_gateio,
    nautilus_symbol,
    parse_delivery_symbol,
    parse_option_symbol,
    product_from_raw_symbol,
    product_of,
    raw_symbol_of,
)

#: (product, venue symbol, instrument id string) for at least one per product.
ROUND_TRIP_CASES = [
    (GateioProductType.SPOT, "BTC_USDT", "BTC_USDT.GATE_IO"),
    (GateioProductType.SPOT, "ETH_BTC", "ETH_BTC.GATE_IO"),
    (GateioProductType.PERP, "BTC_USDT", "BTC_USDT-PERP.GATE_IO"),
    (GateioProductType.PERP, "SOL_USDT", "SOL_USDT-PERP.GATE_IO"),
    (GateioProductType.INVERSE, "BTC_USD", "BTC_USD-PERP.GATE_IO"),
    (GateioProductType.FUT, "BTC_USDT_20260807", "BTC_USDT_20260807.GATE_IO"),
    (GateioProductType.OPT, "BTC_USDT-20260729-70000-C", "BTC_USDT-20260729-70000-C.GATE_IO"),
    (GateioProductType.OPT, "ETH_USDT-20260731-3500-P", "ETH_USDT-20260731-3500-P.GATE_IO"),
]


class TestVenue:
    def test_venue_string_is_gate_io(self):
        assert GATEIO == "GATE_IO"
        assert str(GATEIO_VENUE) == "GATE_IO"


class TestRoundTrip:
    @pytest.mark.parametrize(("product", "raw_symbol", "expected_id"), ROUND_TRIP_CASES)
    def test_gateio_to_instrument_id(self, product, raw_symbol, expected_id):
        instrument_id = gateio_to_instrument_id(product, raw_symbol)
        assert isinstance(instrument_id, InstrumentId)
        assert str(instrument_id) == expected_id
        assert instrument_id.venue == GATEIO_VENUE

    @pytest.mark.parametrize(("product", "raw_symbol", "expected_id"), ROUND_TRIP_CASES)
    def test_instrument_id_back_to_product_and_symbol(self, product, raw_symbol, expected_id):
        assert instrument_id_to_gateio(expected_id) == (product, raw_symbol)

    @pytest.mark.parametrize(("product", "raw_symbol", "expected_id"), ROUND_TRIP_CASES)
    def test_round_trip_is_stable(self, product, raw_symbol, expected_id):
        instrument_id = gateio_to_instrument_id(product, raw_symbol)
        again_product, again_symbol = instrument_id_to_gateio(instrument_id)
        assert (again_product, again_symbol) == (product, raw_symbol)
        assert str(gateio_to_instrument_id(again_product, again_symbol)) == expected_id

    @pytest.mark.parametrize(("product", "raw_symbol", "expected_id"), ROUND_TRIP_CASES)
    def test_objects_and_strings_are_equivalent(self, product, raw_symbol, expected_id):
        as_object = instrument_id_to_gateio(InstrumentId.from_str(expected_id))
        as_string = instrument_id_to_gateio(expected_id)
        assert as_object == as_string == (product, raw_symbol)

    @pytest.mark.parametrize(("product", "raw_symbol", "expected_id"), ROUND_TRIP_CASES)
    def test_helpers_agree_with_the_full_split(self, product, raw_symbol, expected_id):
        assert product_of(expected_id) is product
        assert raw_symbol_of(expected_id) == raw_symbol


class TestRawSymbolPreservation:
    """The venue symbol must survive the trip unchanged."""

    @pytest.mark.parametrize(("product", "raw_symbol", "expected_id"), ROUND_TRIP_CASES)
    def test_venue_symbol_is_never_rewritten(self, product, raw_symbol, expected_id):
        assert raw_symbol_of(gateio_to_instrument_id(product, raw_symbol)) == raw_symbol

    @pytest.mark.parametrize(
        ("product", "raw_symbol"),
        [
            (GateioProductType.SPOT, "BTC_USDT"),
            (GateioProductType.FUT, "BTC_USDT_20260807"),
            (GateioProductType.OPT, "BTC_USDT-20260729-70000-C"),
        ],
    )
    def test_non_perpetual_ids_carry_no_suffix(self, product, raw_symbol):
        instrument_id = gateio_to_instrument_id(product, raw_symbol)
        assert instrument_id.symbol.value == raw_symbol
        assert not instrument_id.symbol.value.endswith(PERP_SUFFIX)

    @pytest.mark.parametrize(
        ("product", "raw_symbol"),
        [(GateioProductType.PERP, "BTC_USDT"), (GateioProductType.INVERSE, "BTC_USD")],
    )
    def test_only_perpetuals_gain_the_suffix(self, product, raw_symbol):
        assert gateio_to_instrument_id(product, raw_symbol).symbol.value == raw_symbol + PERP_SUFFIX

    def test_no_invented_suffixes(self):
        """Regression: only ``-PERP`` may ever be appended (no -SPOT/-FUT/-OPT)."""
        for product, raw_symbol, _ in ROUND_TRIP_CASES:
            symbol = nautilus_symbol(product, raw_symbol)
            extra = symbol[len(raw_symbol) :]
            assert extra in ("", PERP_SUFFIX), f"{product} appended {extra!r}"

    def test_spot_and_perpetual_ids_differ_for_the_same_venue_symbol(self):
        """The symbols shared by spot and perpetual must stay distinguishable."""
        spot = gateio_to_instrument_id(GateioProductType.SPOT, "BTC_USDT")
        perp = gateio_to_instrument_id(GateioProductType.PERP, "BTC_USDT")
        assert spot != perp
        assert raw_symbol_of(spot) == raw_symbol_of(perp) == "BTC_USDT"
        assert product_of(spot) is GateioProductType.SPOT
        assert product_of(perp) is GateioProductType.PERP


class TestProductInference:
    @pytest.mark.parametrize(
        ("raw_symbol", "expected"),
        [
            ("BTC_USDT", GateioProductType.SPOT),
            ("ETH_BTC", GateioProductType.SPOT),
            ("1INCH_USDT", GateioProductType.SPOT),
            ("BTC_USDT_20260807", GateioProductType.FUT),
            ("ETH_USDT_20261225", GateioProductType.FUT),
            ("BTC_USDT-20260729-70000-C", GateioProductType.OPT),
            ("ETH_USDT-20260731-3500.5-P", GateioProductType.OPT),
        ],
    )
    def test_inferred_without_the_perpetual_hint(self, raw_symbol, expected):
        assert product_from_raw_symbol(raw_symbol) is expected

    @pytest.mark.parametrize(
        ("raw_symbol", "expected"),
        [
            ("BTC_USDT", GateioProductType.PERP),
            ("SOL_USDT", GateioProductType.PERP),
            ("BTC_USD", GateioProductType.INVERSE),
            ("ETH_USD", GateioProductType.INVERSE),
        ],
    )
    def test_perpetual_hint_selects_linear_or_inverse_by_quote(self, raw_symbol, expected):
        assert product_from_raw_symbol(raw_symbol, perpetual=True) is expected

    def test_settle_follows_the_inferred_product(self):
        assert product_from_raw_symbol("BTC_USDT", perpetual=True).settle == "usdt"
        assert product_from_raw_symbol("BTC_USD", perpetual=True).settle == "btc"

    def test_dated_symbol_stays_delivery_even_with_the_perpetual_hint(self):
        """A dated symbol is unambiguous; the hint must not override it."""
        assert product_from_raw_symbol("BTC_USDT_20260807", perpetual=True) is GateioProductType.FUT

    def test_option_symbol_stays_an_option_even_with_the_perpetual_hint(self):
        assert (
            product_from_raw_symbol("BTC_USDT-20260729-70000-C", perpetual=True)
            is GateioProductType.OPT
        )


class TestRejectionOfAmbiguousInput:
    @pytest.mark.parametrize("value", ["", ".GATE_IO"])
    def test_empty_symbol_is_rejected(self, value):
        with pytest.raises(ValueError, match="instrument_id symbol"):
            instrument_id_to_gateio(value)

    def test_bare_perp_suffix_is_rejected(self):
        with pytest.raises(ValueError, match="venue symbol under the -PERP suffix"):
            instrument_id_to_gateio("-PERP.GATE_IO")

    def test_empty_raw_symbol_is_rejected(self):
        with pytest.raises(ValueError, match="raw_symbol"):
            gateio_to_instrument_id(GateioProductType.SPOT, "")

    @pytest.mark.parametrize("blank", [" ", "   ", "\t"])
    def test_blank_raw_symbol_is_rejected(self, blank):
        """The hole the hand-written emptiness test left open.

        ``" "`` is not empty, so the old ``if not raw_symbol`` admitted it. For
        a perpetual the ``-PERP`` suffix is appended before ``Symbol`` sees the
        string, so ``Symbol``'s own blank check never fired either and the
        function returned the accepted identifier ``"  -PERP.GATE_IO"``. The
        client would then have subscribed under a whitespace symbol.
        """
        with pytest.raises(ValueError, match="raw_symbol"):
            gateio_to_instrument_id(GateioProductType.PERP, blank)

    @pytest.mark.parametrize(
        "value",
        [
            "BTC_USDT-2026072-70000-C",  # 7-digit expiry
            "BTC_USDT-20260729-70000-X",  # neither a call nor a put
            "BTC_USDT-20260729-C",  # no strike
            "BTC_USDT",  # not an option at all
            "",
        ],
    )
    def test_option_symbol_parser_rejects_malformed_input(self, value):
        with pytest.raises(ValueError, match="option symbol"):
            parse_option_symbol(value)

    @pytest.mark.parametrize(
        "value",
        [
            "BTC_USDT_2026080",  # 7-digit expiry
            "BTC_USDT",  # a spot pair
            "BTC_USDT-20260807",  # dash instead of underscore
            "",
        ],
    )
    def test_delivery_symbol_parser_rejects_malformed_input(self, value):
        with pytest.raises(ValueError, match="delivery symbol"):
            parse_delivery_symbol(value)


class TestSymbolDecomposition:
    @pytest.mark.parametrize(
        ("raw_symbol", "expected"),
        [
            ("BTC_USDT-20260729-70000-C", ("BTC_USDT", "20260729", 70000.0, True)),
            ("ETH_USDT-20260731-3500-P", ("ETH_USDT", "20260731", 3500.0, False)),
            ("ETH_USDT-20260731-3500.5-C", ("ETH_USDT", "20260731", 3500.5, True)),
        ],
    )
    def test_parse_option_symbol(self, raw_symbol, expected):
        assert parse_option_symbol(raw_symbol) == expected

    @pytest.mark.parametrize(
        ("raw_symbol", "expected"),
        [
            ("BTC_USDT_20260807", ("BTC_USDT", "20260807")),
            ("ETH_USDT_20261225", ("ETH_USDT", "20261225")),
        ],
    )
    def test_parse_delivery_symbol(self, raw_symbol, expected):
        assert parse_delivery_symbol(raw_symbol) == expected

    def test_option_underlying_is_a_valid_spot_symbol(self):
        underlying, _, _, _ = parse_option_symbol("BTC_USDT-20260729-70000-C")
        assert product_from_raw_symbol(underlying) is GateioProductType.SPOT

    def test_delivery_underlying_is_a_valid_spot_symbol(self):
        pair, _ = parse_delivery_symbol("BTC_USDT_20260807")
        assert product_from_raw_symbol(pair) is GateioProductType.SPOT


class TestCaseHandling:
    def test_lowercase_input_is_upper_cased_consistently(self):
        assert instrument_id_to_gateio("btc_usdt.GATE_IO") == (GateioProductType.SPOT, "BTC_USDT")
        assert (
            gateio_to_instrument_id(GateioProductType.PERP, "btc_usdt").symbol.value
            == "BTC_USDT-PERP"
        )

    def test_lowercase_perp_suffix_is_recognised(self):
        assert instrument_id_to_gateio("btc_usdt-perp.GATE_IO") == (
            GateioProductType.PERP,
            "BTC_USDT",
        )
