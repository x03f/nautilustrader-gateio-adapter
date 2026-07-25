"""Instrument provider building Nautilus ``CurrencyPair`` instruments from Gate.io specs."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import CurrencyType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
from nautilus_trader.model.instruments import CurrencyPair
from nautilus_trader.model.objects import Currency, Money, Price, Quantity

from nautilus_gateio.constants import GATEIO
from nautilus_gateio.http import GateioHttpClient
from nautilus_gateio.symbols import instrument_id_to_gate_pair

_currency_cache: dict[str, Currency] = {}


def get_currency(code: str) -> Currency:
    """Return a Nautilus ``Currency`` for ``code``, creating a crypto fallback
    (precision 8) for codes unknown to Nautilus."""
    if code == "USDT":
        return USDT
    if code not in _currency_cache:
        try:
            _currency_cache[code] = Currency.from_str(code)
        except Exception:
            _currency_cache[code] = Currency(
                code=code,
                precision=8,
                iso4217=0,
                name=code,
                currency_type=CurrencyType.CRYPTO,
            )
    return _currency_cache[code]


def build_currency_pair(
    spec: dict[str, Any],
    venue: str = GATEIO,
    maker_fee: Decimal = Decimal("0.002"),
    taker_fee: Decimal = Decimal("0.002"),
) -> CurrencyPair:
    """Build a Nautilus ``CurrencyPair`` from a parsed Gate.io pair spec.

    ``spec`` is the output of ``GateioHttpClient.currency_pair()`` /
    ``parse_currency_pair()``. The instrument id uses the canonical underscore
    symbol (``BTC_USDT.GATEIO``). Default fees are Gate.io's standard tier;
    pass your account's actual tier for accurate simulated fills.
    """
    pair = spec["pair"]
    base_code, _, quote_code = pair.partition("_")
    price_precision = int(spec.get("price_precision", 8))
    size_precision = int(spec.get("amount_precision", 8))
    symbol = Symbol(pair)
    min_base = float(spec.get("min_base_amount", 0) or 0)
    min_quote = float(spec.get("min_quote_amount", 0) or 0)
    quote_currency = get_currency(quote_code)
    return CurrencyPair(
        instrument_id=InstrumentId(symbol=symbol, venue=Venue(venue)),
        raw_symbol=symbol,
        base_currency=get_currency(base_code),
        quote_currency=quote_currency,
        price_precision=price_precision,
        size_precision=size_precision,
        price_increment=Price(10**-price_precision, price_precision),
        size_increment=Quantity(10**-size_precision, size_precision),
        lot_size=None,
        max_quantity=None,
        min_quantity=Quantity(min_base or 10**-size_precision, size_precision),
        max_notional=None,
        min_notional=Money(min_quote or 0, quote_currency) if min_quote else None,
        max_price=None,
        min_price=None,
        margin_init=Decimal(0),
        margin_maint=Decimal(0),
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        ts_event=0,
        ts_init=0,
    )


class GateioInstrumentProvider(InstrumentProvider):
    """Loads Gate.io spot instruments on demand.

    ``load_ids_async`` fetches the individual pair specs via REST and builds
    ``CurrencyPair`` instruments. ``load_all_async`` loads every tradable spot
    pair (thousands of instruments — prefer ``load_ids_async`` with an explicit
    list for live nodes).
    """

    def __init__(
        self,
        http_client: GateioHttpClient | None = None,
        config: InstrumentProviderConfig | None = None,
        venue: str = GATEIO,
    ) -> None:
        super().__init__(config=config or InstrumentProviderConfig())
        self._http = http_client or GateioHttpClient()
        self._venue = venue

    async def load_all_async(self, filters: dict | None = None) -> None:
        for spec in self._http.currency_pairs():
            if spec.get("trade_status") == "tradable":
                self.add(build_currency_pair(spec, venue=self._venue))

    async def load_ids_async(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict | None = None,
    ) -> None:
        for instrument_id in instrument_ids:
            pair = instrument_id_to_gate_pair(instrument_id)
            spec = self._http.currency_pair(pair)
            self.add(build_currency_pair(spec, venue=self._venue))

    async def load_async(
        self,
        instrument_id: InstrumentId,
        filters: dict | None = None,
    ) -> None:
        await self.load_ids_async([instrument_id], filters)


class StaticInstrumentProvider(InstrumentProvider):
    """Serves a fixed, pre-built list of instruments (no network access).

    Useful for tests and for nodes that construct instruments up front.
    """

    def __init__(self, instruments: list[CurrencyPair] | None = None) -> None:
        super().__init__(config=InstrumentProviderConfig())
        # NOTE: must not be named ``_instruments`` — that would shadow the
        # internal registry of the InstrumentProvider base class.
        self._preset_instruments = [i for i in (instruments or []) if i is not None]

    async def load_all_async(self, filters: dict | None = None) -> None:
        for instrument in self._preset_instruments:
            self.add(instrument)

    async def load_ids_async(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict | None = None,
    ) -> None:
        await self.load_all_async(filters)
