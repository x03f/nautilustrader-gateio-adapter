"""Venue-native data types Gate.io publishes that NautilusTrader has no type for.

The platform models mark prices, index prices, funding rates and option greeks
as first-class data (``MarkPriceUpdate``, ``IndexPriceUpdate``,
``FundingRateUpdate``, ``OptionGreeks``) and this client publishes all four from
the venue's ticker channel. What the ticker carries *besides* those — 24-hour
statistics, the delivery basis, the indicative next funding rate — has no
platform type, and the in-tree answer for that is a ``Data`` subclass routed
through the generic ``_subscribe`` hook, exactly as ``BinanceTicker``
(``adapters/binance/common/types.py``) and ``BetfairTicker``
(``adapters/betfair/data_types.py``) do for their venues.

This module deliberately does **not** use ``from __future__ import annotations``,
unlike every other module in the package. ``@customdataclass`` reads
``cls.__annotations__`` and maps each annotation object onto an Arrow type
(``model/custom.py:239-266``); with PEP 563 postponed evaluation every
annotation is a plain string, ``_annotation_name`` finds no ``__name__`` on it
and the class definition raises ``TypeError`` at import. The same code path is
why every field below is annotated with a bare type and never with a union: an
optional annotation resolves to ``UnionType``, which is not in the decorator's
type map and raises the same error.
"""

from nautilus_trader.core.data import Data
from nautilus_trader.model.custom import customdataclass
from nautilus_trader.model.identifiers import InstrumentId

#: Ticker fields carried verbatim from the venue payload, in declaration order.
#: Keeping the venue's own names means a reader can match a published value
#: against Gate.io's documentation without a translation table, and it keeps the
#: spot and contract dialects distinguishable: spot names its turnover
#: ``base_volume``/``quote_volume`` while the contract products name theirs
#: ``volume_24h_base``/``volume_24h_quote`` and add a contract count.
TICKER_FIELDS: tuple[str, ...] = (
    # Every product
    "last",
    "change_percentage",
    "high_24h",
    "low_24h",
    # Spot only (`spot.tickers`)
    "highest_bid",
    "lowest_ask",
    "base_volume",
    "quote_volume",
    # Futures, delivery and options (`futures.tickers`, `options.contract_tickers`)
    "volume_24h",
    "volume_24h_base",
    "volume_24h_quote",
    "volume_24h_settle",
    "total_size",
    "quanto_base_rate",
    # Perpetual only: the rate Gate.io projects for the *next* settlement, which
    # is not the applied rate the client publishes as `FundingRateUpdate`.
    "funding_rate_indicative",
    # Delivery only
    "basis_rate",
    "basis_value",
    "settle_price",
)


@customdataclass
class GateioTicker(Data):
    """One Gate.io ticker row, published as the venue sent it.

    Mark price, index price, funding rate and the option greeks are deliberately
    **absent**. The platform has ``MarkPriceUpdate``, ``IndexPriceUpdate``,
    ``FundingRateUpdate`` and ``OptionGreeks`` for them and this client publishes
    all four from the same venue message, so carrying them here as well would
    give a strategy two sources for one number and no rule for which to believe.

    "The option greeks" means everything ``OptionGreeks`` declares: the five
    standard greeks, the three implied volatilities, and the open interest
    Gate.io names ``position_size``. Those are the platform's canonical schema
    for an option quote, so they are subscribed to as ``OptionGreeks`` and not
    read off this row. What is left here for options is what the canonical
    schema has no field for — the row's 24-hour statistics.

    Every field is the venue's own string rather than a ``Price`` or a
    ``Quantity``. One ticker row mixes scales — an order-tick price, a contract
    count, a base-currency turnover and a dimensionless implied volatility — and
    quantizing them all onto the instrument's order precision would silently
    change values. This is the same reason mark and index prices keep the venue's
    scale rather than going through ``Instrument.make_price``.

    A field the venue did not send for this product is the empty string, not
    ``None``: the platform's Arrow schema builder for custom data accepts no
    optional annotation (see the module docstring), so "absent" has to be a
    value of the field's own type.

    Parameters
    ----------
    instrument_id : InstrumentId
        The instrument the row describes.
    last : str
        Last traded price.
    change_percentage : str
        24-hour price change, in percent.
    high_24h : str
        24-hour high price.
    low_24h : str
        24-hour low price.
    highest_bid : str
        Best bid price (spot only).
    lowest_ask : str
        Best ask price (spot only).
    base_volume : str
        24-hour base-currency volume (spot only).
    quote_volume : str
        24-hour quote-currency turnover (spot only).
    volume_24h : str
        24-hour volume in contracts (contract products only).
    volume_24h_base : str
        24-hour volume in base currency (contract products only).
    volume_24h_quote : str
        24-hour turnover in quote currency (contract products only).
    volume_24h_settle : str
        24-hour turnover in the settlement currency (contract products only).
    total_size : str
        Open interest in contracts (contract products only).
    quanto_base_rate : str
        Base-currency exchange rate of a quanto contract (contract products only).
    funding_rate_indicative : str
        The rate Gate.io projects for the next funding settlement (perpetuals only).
    basis_rate : str
        Basis rate against the index (delivery futures only).
    basis_value : str
        Basis value against the index (delivery futures only).
    settle_price : str
        Settlement price; ``"0"`` until the contract settles (delivery futures only).
    ts_event : int
        UNIX timestamp (nanoseconds) when the venue published the row.
    ts_init : int
        UNIX timestamp (nanoseconds) when the object was initialized.

    """

    instrument_id: InstrumentId = InstrumentId.from_str("NULL.GATE_IO")
    last: str = ""
    change_percentage: str = ""
    high_24h: str = ""
    low_24h: str = ""
    highest_bid: str = ""
    lowest_ask: str = ""
    base_volume: str = ""
    quote_volume: str = ""
    volume_24h: str = ""
    volume_24h_base: str = ""
    volume_24h_quote: str = ""
    volume_24h_settle: str = ""
    total_size: str = ""
    quanto_base_rate: str = ""
    funding_rate_indicative: str = ""
    basis_rate: str = ""
    basis_value: str = ""
    settle_price: str = ""

    @classmethod
    def from_payload(
        cls,
        instrument_id: InstrumentId,
        payload: dict,
        ts_event: int,
        ts_init: int,
    ) -> "GateioTicker":
        """Build a ticker from one venue payload row.

        Fields the row does not carry stay at their empty default rather than
        being filled from a previous row: a ticker is a complete statement of
        what the venue published at ``ts_event``, and carrying a stale value
        forward would make an absent field indistinguishable from an unchanged
        one.
        """
        values = {
            field: "" if payload.get(field) is None else str(payload[field])
            for field in TICKER_FIELDS
        }
        return cls(
            ts_event=ts_event,
            ts_init=ts_init,
            instrument_id=instrument_id,
            **values,
        )


__all__ = [
    "TICKER_FIELDS",
    "GateioTicker",
]
