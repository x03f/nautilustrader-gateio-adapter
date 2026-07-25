"""Tests for the instrument providers (no network - stubbed HTTP client)."""

from __future__ import annotations

import asyncio
from typing import Any

from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.identifiers import InstrumentId

from nautilus_gateio.providers import (
    GateioInstrumentProvider,
    StaticInstrumentProvider,
    build_currency_pair,
    get_currency,
)


class _StubHttp:
    """Stands in for ``GateioHttpClient``: serves canned pair specs."""

    def __init__(self, specs: list[dict[str, Any]]) -> None:
        self.specs = specs
        self.requested_pairs: list[str] = []

    def currency_pair(self, pair: str) -> dict[str, Any]:
        self.requested_pairs.append(pair)
        for spec in self.specs:
            if spec["pair"] == pair:
                return dict(spec)
        raise KeyError(pair)

    def currency_pairs(self) -> list[dict[str, Any]]:
        return [dict(spec) for spec in self.specs]


class TestGetCurrency:
    def test_usdt_identity(self):
        assert get_currency("USDT") is USDT

    def test_unknown_code_fallback_precision(self):
        currency = get_currency("QQXZTEST")
        assert currency.code == "QQXZTEST"
        assert currency.precision == 8

    def test_cached_instance_reused(self):
        assert get_currency("QQXZTEST") is get_currency("QQXZTEST")


class TestBuildCurrencyPair:
    def test_instrument_id(self, btc_spec):
        instrument = build_currency_pair(btc_spec)
        assert str(instrument.id) == "BTC_USDT.GATEIO"

    def test_precisions(self, btc_spec):
        instrument = build_currency_pair(btc_spec)
        assert instrument.price_precision == 2
        assert instrument.size_precision == 6

    def test_increments(self, btc_spec):
        instrument = build_currency_pair(btc_spec)
        assert float(instrument.price_increment) == 0.01
        assert float(instrument.size_increment) == 1e-6

    def test_min_quantity_and_notional(self, btc_spec):
        instrument = build_currency_pair(btc_spec)
        assert abs(float(instrument.min_quantity) - 0.00001) < 1e-12
        assert instrument.min_notional is not None
        assert instrument.min_notional.as_double() == 3.0
        assert instrument.min_notional.currency == USDT

    def test_currencies(self, btc_spec):
        instrument = build_currency_pair(btc_spec)
        assert instrument.base_currency.code == "BTC"
        assert instrument.quote_currency is USDT

    def test_no_min_notional_when_absent(self, btc_spec):
        spec = {**btc_spec, "min_quote_amount": 0}
        instrument = build_currency_pair(spec)
        assert instrument.min_notional is None


class TestGateioInstrumentProvider:
    def test_load_ids_async_adds_instrument(self, btc_spec):
        stub = _StubHttp([btc_spec])
        provider = GateioInstrumentProvider(http_client=stub)
        instrument_id = InstrumentId.from_str("BTC_USDT.GATEIO")

        asyncio.run(provider.load_ids_async([instrument_id]))

        instrument = provider.find(instrument_id)
        assert instrument is not None
        assert str(instrument.id) == "BTC_USDT.GATEIO"
        assert stub.requested_pairs == ["BTC_USDT"]

    def test_load_async_delegates(self, btc_spec):
        provider = GateioInstrumentProvider(http_client=_StubHttp([btc_spec]))
        instrument_id = InstrumentId.from_str("BTC_USDT.GATEIO")

        asyncio.run(provider.load_async(instrument_id))

        assert provider.find(instrument_id) is not None

    def test_load_all_async_only_tradable(self, btc_spec):
        untradable = {**btc_spec, "pair": "ETH_USDT", "trade_status": "untradable"}
        provider = GateioInstrumentProvider(http_client=_StubHttp([btc_spec, untradable]))

        asyncio.run(provider.load_all_async())

        assert provider.find(InstrumentId.from_str("BTC_USDT.GATEIO")) is not None
        assert provider.find(InstrumentId.from_str("ETH_USDT.GATEIO")) is None


class TestStaticInstrumentProvider:
    def test_serves_preset_list(self, btc_spec):
        instrument = build_currency_pair(btc_spec)
        provider = StaticInstrumentProvider([instrument])

        asyncio.run(provider.load_all_async())

        assert provider.find(InstrumentId.from_str("BTC_USDT.GATEIO")) is not None

    def test_load_ids_serves_same_list(self, btc_spec):
        instrument = build_currency_pair(btc_spec)
        provider = StaticInstrumentProvider([instrument])

        asyncio.run(provider.load_ids_async([instrument.id]))

        assert provider.find(instrument.id) is not None

    def test_none_entries_filtered(self):
        provider = StaticInstrumentProvider([None])
        asyncio.run(provider.load_all_async())
        assert provider.count == 0
