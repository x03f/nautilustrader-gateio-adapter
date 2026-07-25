"""Unit tests for symbol conversion between Nautilus ids and Gate.io pairs."""

from __future__ import annotations

import pytest
from nautilus_trader.model.identifiers import InstrumentId

from nautilus_gateio.constants import GATEIO_VENUE
from nautilus_gateio.symbols import (
    gate_pair_to_instrument_id,
    instrument_id_to_gate_pair,
)


class TestInstrumentIdToGatePair:
    def test_canonical_underscore_form_passes_through(self):
        assert instrument_id_to_gate_pair("BTC_USDT.GATEIO") == "BTC_USDT"

    def test_accepts_instrument_id_object(self):
        instrument_id = InstrumentId.from_str("BTC_USDT.GATEIO")
        assert instrument_id_to_gate_pair(instrument_id) == "BTC_USDT"

    def test_accepts_plain_symbol_without_venue(self):
        assert instrument_id_to_gate_pair("ETH_USDT") == "ETH_USDT"

    @pytest.mark.parametrize(
        ("compact", "expected"),
        [
            ("BTCUSDT", "BTC_USDT"),
            ("SOLUSDT", "SOL_USDT"),
            ("SANDUSDT", "SAND_USDT"),  # SAND ends with 'AND', not a quote
            ("MATICUSDT", "MATIC_USDT"),
            ("BTCUSDC", "BTC_USDC"),
            ("ETHBTC", "ETH_BTC"),
        ],
    )
    def test_heuristic_infers_known_quote(self, compact, expected):
        assert instrument_id_to_gate_pair(f"{compact}.GATEIO") == expected

    def test_lowercase_rejected(self):
        with pytest.raises(ValueError):
            instrument_id_to_gate_pair("btcusdt.GATEIO")

    def test_lowercase_underscore_form_rejected(self):
        with pytest.raises(ValueError):
            instrument_id_to_gate_pair("btc_usdt")

    def test_no_quote_match_rejected(self):
        with pytest.raises(ValueError):
            instrument_id_to_gate_pair("FOOBAR.GATEIO")

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            instrument_id_to_gate_pair("")

    def test_bare_quote_symbol_rejected(self):
        # 'USDT' alone matches a quote suffix but has no base part.
        with pytest.raises(ValueError):
            instrument_id_to_gate_pair("USDT.GATEIO")

    def test_dangling_underscore_rejected(self):
        with pytest.raises(ValueError):
            instrument_id_to_gate_pair("BTC_.GATEIO")


class TestGatePairToInstrumentId:
    def test_basic_conversion(self):
        instrument_id = gate_pair_to_instrument_id("BTC_USDT")
        assert isinstance(instrument_id, InstrumentId)
        assert str(instrument_id) == "BTC_USDT.GATEIO"
        assert instrument_id.venue == GATEIO_VENUE

    def test_lowercase_input_normalized_upper(self):
        instrument_id = gate_pair_to_instrument_id("btc_usdt")
        assert str(instrument_id) == "BTC_USDT.GATEIO"

    def test_invalid_pair_without_underscore_rejected(self):
        with pytest.raises(ValueError):
            gate_pair_to_instrument_id("BTCUSDT")

    def test_round_trip(self):
        for pair in ("BTC_USDT", "SAND_USDT", "ETH_BTC"):
            instrument_id = gate_pair_to_instrument_id(pair)
            assert instrument_id_to_gate_pair(instrument_id) == pair

    def test_round_trip_from_lowercase_input(self):
        instrument_id = gate_pair_to_instrument_id("btc_usdt")
        assert instrument_id_to_gate_pair(instrument_id) == "BTC_USDT"
