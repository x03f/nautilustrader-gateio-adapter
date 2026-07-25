"""Unit tests for the pure payload parsers in ``nautilus_gateio.schemas``.

All payloads are synthetic, shaped like real Gate.io API v4 responses.
"""

from __future__ import annotations

import pytest

from nautilus_gateio.errors import OrderValidationError
from nautilus_gateio.schemas import (
    parse_balances,
    parse_candle,
    parse_currency_pair,
    parse_fill,
    parse_futures_contract,
    parse_order,
    parse_order_book,
    parse_public_trade,
    validate_order,
)


class TestParseCurrencyPair:
    def test_full_mapping(self):
        raw = {
            "id": "BTC_USDT",
            "base": "BTC",
            "quote": "USDT",
            "amount_precision": "6",
            "precision": "2",
            "min_base_amount": "0.00001",
            "min_quote_amount": "3",
            "fee": "0.2",
            "trade_status": "tradable",
        }
        parsed = parse_currency_pair(raw)
        assert parsed == {
            "pair": "BTC_USDT",
            "base": "BTC",
            "quote": "USDT",
            "amount_precision": 6,
            "price_precision": 2,
            "min_base_amount": 0.00001,
            "min_quote_amount": 3.0,
            "fee": 0.2,
            "trade_status": "tradable",
        }

    def test_defaults_when_fields_missing(self):
        parsed = parse_currency_pair({"id": "ETH_USDT"})
        assert parsed["amount_precision"] == 8
        assert parsed["price_precision"] == 8
        assert parsed["min_base_amount"] == 0.0
        assert parsed["min_quote_amount"] == 0.0
        assert parsed["fee"] == 0.0
        assert parsed["trade_status"] is None


class TestParseCandle:
    def test_gate_row_order_and_seconds_to_ms(self):
        # Gate.io row order: [ts, quote_volume, close, high, low, open, base_volume]
        raw = ["1700000000", "123456.7", "100.5", "101.0", "99.0", "100.0", "12.34"]
        parsed = parse_candle(raw)
        assert parsed == {
            "ts": 1_700_000_000_000,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 12.34,
        }

    def test_numeric_inputs_accepted(self):
        raw = [1700000060, 1.0, 2.0, 3.0, 1.5, 1.8, 4.0]
        parsed = parse_candle(raw)
        assert parsed["ts"] == 1_700_000_060_000
        assert parsed["close"] == 2.0


class TestParsePublicTrade:
    def test_full_mapping(self):
        raw = {
            "id": "5736713",
            "create_time_ms": "1700000000123.456",
            "price": "100.5",
            "amount": "0.25",
            "side": "buy",
        }
        parsed = parse_public_trade(raw)
        assert parsed == {
            "id": "5736713",
            "ts": 1_700_000_000_123,
            "price": 100.5,
            "amount": 0.25,
            "side": "buy",
        }

    def test_missing_fields_default_safely(self):
        parsed = parse_public_trade({})
        assert parsed["ts"] == 0
        assert parsed["price"] == 0.0
        assert parsed["amount"] == 0.0
        assert parsed["side"] is None


class TestParseOrderBook:
    def test_string_levels_become_floats(self):
        raw = {
            "current": 1700000000123,
            "bids": [["100.1", "0.5"], ["100.0", "1.5"]],
            "asks": [["100.2", "0.7"], ["100.3", "2.0"]],
        }
        parsed = parse_order_book(raw)
        assert parsed["bids"] == [[100.1, 0.5], [100.0, 1.5]]
        assert parsed["asks"] == [[100.2, 0.7], [100.3, 2.0]]
        assert parsed["ts"] == 1700000000123
        assert all(isinstance(v, float) for level in parsed["bids"] + parsed["asks"] for v in level)

    def test_empty_book(self):
        parsed = parse_order_book({})
        assert parsed["bids"] == []
        assert parsed["asks"] == []
        assert parsed["ts"] is None


def _order_payload(**overrides):
    raw = {
        "id": "1234567890",
        "text": "t-ng-0001",
        "currency_pair": "BTC_USDT",
        "side": "buy",
        "type": "limit",
        "price": "100.0",
        "avg_deal_price": "99.9",
        "amount": "1.0",
        "filled_total": "0",
        "left": "1.0",
        "status": "open",
        "fee": "0",
        "fee_currency": "USDT",
        "create_time_ms": "1700000000123",
        "finish_as": "open",
        "time_in_force": "gtc",
    }
    raw.update(overrides)
    return raw


class TestParseOrder:
    def test_open_partial_fill_sets_partial_flag(self):
        parsed = parse_order(_order_payload(filled_total="0.4", left="0.6"))
        assert parsed["status"] == "open"
        assert parsed["filled"] == 0.4
        assert parsed["amount"] == 1.0
        assert parsed["partial"] is True

    def test_open_zero_fill_not_partial(self):
        parsed = parse_order(_order_payload(filled_total="0"))
        assert parsed["partial"] is False

    def test_closed_filled_not_partial(self):
        parsed = parse_order(
            _order_payload(status="closed", filled_total="1.0", left="0", finish_as="filled")
        )
        assert parsed["status"] == "closed"
        assert parsed["finish_as"] == "filled"
        assert parsed["partial"] is False
        assert parsed["filled"] == 1.0

    def test_cancelled_order(self):
        parsed = parse_order(_order_payload(status="cancelled", finish_as="cancelled", left="1.0"))
        assert parsed["status"] == "cancelled"
        assert parsed["finish_as"] == "cancelled"
        assert parsed["partial"] is False

    def test_avg_price_parsed(self):
        parsed = parse_order(_order_payload(avg_deal_price="99.95"))
        assert parsed["avg_price"] == 99.95

    def test_time_in_force_passthrough(self):
        parsed = parse_order(_order_payload(time_in_force="ioc"))
        assert parsed["time_in_force"] == "ioc"

    def test_identity_fields(self):
        parsed = parse_order(_order_payload())
        assert parsed["id"] == "1234567890"
        assert parsed["client_id"] == "t-ng-0001"
        assert parsed["pair"] == "BTC_USDT"
        assert parsed["side"] == "buy"
        assert parsed["type"] == "limit"
        assert parsed["create_time_ms"] == 1700000000123

    def test_missing_fields_default_safely(self):
        parsed = parse_order({})
        assert parsed["price"] == 0.0
        assert parsed["avg_price"] == 0.0
        assert parsed["amount"] == 0.0
        assert parsed["filled"] == 0.0
        assert parsed["left"] == 0.0
        assert parsed["fee"] == 0.0
        assert parsed["create_time_ms"] == 0
        assert parsed["partial"] is False
        assert parsed["status"] == ""
        assert parsed["time_in_force"] is None


class TestParseFill:
    @pytest.mark.parametrize("role", ["maker", "taker"])
    def test_maker_taker_mapping(self, role):
        raw = {
            "id": "77",
            "order_id": "1234567890",
            "currency_pair": "BTC_USDT",
            "side": "sell",
            "price": "100.5",
            "amount": "0.4",
            "fee": "0.0008",
            "fee_currency": "USDT",
            "role": role,
            "create_time_ms": "1700000000456",
        }
        parsed = parse_fill(raw)
        assert parsed == {
            "id": "77",
            "order_id": "1234567890",
            "pair": "BTC_USDT",
            "side": "sell",
            "price": 100.5,
            "amount": 0.4,
            "fee": 0.0008,
            "fee_currency": "USDT",
            "role": role,
            "create_time_ms": 1700000000456,
        }


class TestParseBalances:
    def test_available_and_locked_floats(self):
        raw = [
            {"currency": "USDT", "available": "1000.5", "locked": "10.25"},
            {"currency": "BTC", "available": "0.5", "locked": "0"},
        ]
        parsed = parse_balances(raw)
        assert parsed == {
            "USDT": {"available": 1000.5, "locked": 10.25},
            "BTC": {"available": 0.5, "locked": 0.0},
        }
        assert isinstance(parsed["USDT"]["available"], float)
        assert isinstance(parsed["BTC"]["locked"], float)

    def test_empty_list(self):
        assert parse_balances([]) == {}


class TestParseFuturesContract:
    def test_full_mapping(self):
        raw = {
            "name": "BTC_USDT",
            "type": "direct",
            "quanto_multiplier": "0.0001",
            "order_price_round": "0.1",
            "order_size_min": 1,
            "leverage_min": "1",
            "leverage_max": "100",
            "maker_fee_rate": "-0.00025",
            "taker_fee_rate": "0.00075",
            "mark_price": "100000.5",
            "index_price": "100000.4",
            "funding_rate": "0.0001",
            "funding_interval": 28800,
            "maintenance_rate": "0.005",
            "in_delisting": False,
        }
        parsed = parse_futures_contract(raw)
        assert parsed == {
            "name": "BTC_USDT",
            "type": "direct",
            "quanto_multiplier": 0.0001,
            "tick_size": "0.1",
            "order_size_min": 1,
            "leverage_min": "1",
            "leverage_max": "100",
            "maker_fee": -0.00025,
            "taker_fee": 0.00075,
            "mark_price": 100000.5,
            "index_price": 100000.4,
            "funding_rate": 0.0001,
            "funding_interval": 28800,
            "maintenance_rate": "0.005",
            "in_delisting": False,
        }


class TestValidateOrder:
    def test_valid_order_returns_normalized_tuple(self, btc_spec):
        amt, px = validate_order(btc_spec, amount=0.5, price=100.0)
        assert (amt, px) == (0.5, 100.0)

    def test_rejects_tiny_amount(self, btc_spec):
        with pytest.raises(OrderValidationError, match="min_base_amount"):
            validate_order(btc_spec, amount=0.000001, price=100000.0)

    def test_rejects_below_min_notional(self, btc_spec):
        # 0.0001 * 100 = 0.01 quote, below min_quote_amount of 3.0
        with pytest.raises(OrderValidationError, match="min_notional"):
            validate_order(btc_spec, amount=0.0001, price=100.0)

    def test_rejects_untradable_pair(self, btc_spec):
        spec = dict(btc_spec, trade_status="untradable")
        with pytest.raises(OrderValidationError, match="not tradable"):
            validate_order(spec, amount=1.0, price=100.0)

    def test_rounds_amount_to_precision(self, btc_spec):
        spec = dict(btc_spec, amount_precision=4)
        amt, _ = validate_order(spec, amount=0.123456789, price=100000.0)
        assert amt == 0.1235

    def test_rounds_price_to_precision(self, btc_spec):
        _, px = validate_order(btc_spec, amount=1.0, price=100.123456)
        assert px == 100.12  # price_precision=2

    def test_price_none_skips_notional_check(self, btc_spec):
        # Amount above min_base but with a notional that would fail at low prices;
        # with price=None the notional check must be skipped.
        amt, px = validate_order(btc_spec, amount=0.0001, price=None)
        assert amt == 0.0001
        assert px is None

    def test_price_none_still_checks_min_base(self, btc_spec):
        with pytest.raises(OrderValidationError, match="min_base_amount"):
            validate_order(btc_spec, amount=0.000001, price=None)
