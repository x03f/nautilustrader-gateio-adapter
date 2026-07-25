"""Symbology: mapping between Nautilus instrument ids and Gate.io symbols.

Design principle: **minimum normalization**. Gate.io symbols are used verbatim
wherever they are already unique, and a suffix is added only where the exchange
genuinely reuses one symbol for two different instruments.

Why perpetuals need a suffix and nothing else does
--------------------------------------------------
Surveyed against the live venue: 527 of Gate.io's USDT perpetual contracts share
their exact symbol with a spot pair (``BTC_USDT`` is both a spot market and a
perpetual). Delivery contracts (``BTC_USDT_20260807``) and options
(``BTC_USDT-20260729-70000-C``) carry the expiry in the symbol and collide with
nothing, and no spot pair contains a dash or a second underscore.

The ``-PERP`` suffix is the established NautilusTrader convention for exactly
this situation (the Binance adapter maps its perpetuals to ``BTCUSDT-PERP``, and
the Tardis integration documents ``BTCUSDT-PERP.BINANCE`` as the canonical form).

======================  ============================================  ============================
Product                 Instrument id                                 ``raw_symbol``
======================  ============================================  ============================
Spot                    ``BTC_USDT.GATE_IO``                          ``BTC_USDT``
Perpetual (linear)      ``BTC_USDT-PERP.GATE_IO``                     ``BTC_USDT``
Perpetual (inverse)     ``BTC_USD-PERP.GATE_IO``                      ``BTC_USD``
Delivery future         ``BTC_USDT_20260807.GATE_IO``                 ``BTC_USDT_20260807``
Option                  ``BTC_USDT-20260729-70000-C.GATE_IO``         ``BTC_USDT-20260729-70000-C``
======================  ============================================  ============================

``raw_symbol`` on every instrument is always the exact string Gate.io uses, so
round-tripping to the API never needs to reverse a transformation.
"""

from __future__ import annotations

import re

from nautilus_trader.model.identifiers import InstrumentId, Symbol

from nautilus_gateio.common.constants import GATEIO_VENUE
from nautilus_gateio.common.enums import GateioProductType

#: Suffix marking a perpetual contract (NautilusTrader convention).
PERP_SUFFIX = "-PERP"

#: ``BASE_QUOTE_YYYYMMDD`` — a dated (delivery) futures contract.
DELIVERY_PATTERN = re.compile(r"^[A-Z0-9]+_[A-Z0-9]+_\d{8}$")

#: ``BASE_QUOTE-YYYYMMDD-STRIKE-C|P`` — an option contract.
OPTION_PATTERN = re.compile(r"^([A-Z0-9]+_[A-Z0-9]+)-(\d{8})-([\d.]+)-([CP])$")

#: Quote currencies that identify an inverse (coin-margined) perpetual on Gate.io.
INVERSE_QUOTES = ("USD",)


def product_from_raw_symbol(raw_symbol: str, perpetual: bool = False) -> GateioProductType:
    """Infer the Gate.io product from a venue symbol.

    ``perpetual`` disambiguates the one case the symbol alone cannot: a spot pair
    and a perpetual contract may share a symbol.
    """
    symbol = raw_symbol.upper()
    if OPTION_PATTERN.match(symbol):
        return GateioProductType.OPT
    if DELIVERY_PATTERN.match(symbol):
        return GateioProductType.FUT
    if perpetual:
        quote = symbol.rpartition("_")[2]
        return GateioProductType.INVERSE if quote in INVERSE_QUOTES else GateioProductType.PERP
    return GateioProductType.SPOT


def instrument_id_to_gateio(instrument_id: InstrumentId | str) -> tuple[GateioProductType, str]:
    """Split an instrument id into its Gate.io product and venue symbol.

    The venue symbol returned is exactly what the API expects; the ``-PERP``
    marker (if any) is removed.
    """
    symbol = str(instrument_id).split(".")[0].upper()
    if not symbol:
        raise ValueError("instrument id has an empty symbol")
    if symbol.endswith(PERP_SUFFIX):
        raw = symbol[: -len(PERP_SUFFIX)]
        if not raw:
            raise ValueError(f"instrument symbol {symbol!r} has an empty venue symbol")
        return product_from_raw_symbol(raw, perpetual=True), raw
    return product_from_raw_symbol(symbol), symbol


def gateio_to_instrument_id(
    product: GateioProductType,
    raw_symbol: str,
    venue: str | None = None,
) -> InstrumentId:
    """Build the canonical instrument id for a Gate.io symbol and product."""
    if not raw_symbol:
        raise ValueError("raw_symbol must not be empty")
    symbol = raw_symbol.upper()
    if product.is_perpetual:
        symbol += PERP_SUFFIX
    return InstrumentId(symbol=Symbol(symbol), venue=_venue(venue))


def nautilus_symbol(product: GateioProductType, raw_symbol: str) -> str:
    """Return the Nautilus symbol string (venue symbol plus any required suffix)."""
    symbol = raw_symbol.upper()
    return symbol + PERP_SUFFIX if product.is_perpetual else symbol


def _venue(name: str | None):
    if name is None:
        return GATEIO_VENUE
    from nautilus_trader.model.identifiers import Venue

    return Venue(name)


def product_of(instrument_id: InstrumentId | str) -> GateioProductType:
    """Return just the product of an instrument id."""
    return instrument_id_to_gateio(instrument_id)[0]


def raw_symbol_of(instrument_id: InstrumentId | str) -> str:
    """Return just the venue symbol of an instrument id."""
    return instrument_id_to_gateio(instrument_id)[1]


def parse_option_symbol(raw_symbol: str) -> tuple[str, str, float, bool]:
    """Decompose a Gate.io option symbol.

    ``BTC_USDT-20260729-70000-C`` -> ``("BTC_USDT", "20260729", 70000.0, True)``,
    where the final element is ``True`` for a call and ``False`` for a put.
    """
    match = OPTION_PATTERN.match(raw_symbol.upper())
    if not match:
        raise ValueError(
            f"option symbol {raw_symbol!r} is not in the expected "
            f"'<UNDERLYING>-<YYYYMMDD>-<STRIKE>-<C|P>' form"
        )
    underlying, expiry, strike, kind = match.groups()
    return underlying, expiry, float(strike), kind == "C"


def parse_delivery_symbol(raw_symbol: str) -> tuple[str, str]:
    """Decompose a delivery contract symbol.

    ``BTC_USDT_20260807`` -> ``("BTC_USDT", "20260807")``.
    """
    symbol = raw_symbol.upper()
    if not DELIVERY_PATTERN.match(symbol):
        raise ValueError(
            f"delivery symbol {raw_symbol!r} is not in the expected '<PAIR>_<YYYYMMDD>' form"
        )
    pair, _, expiry = symbol.rpartition("_")
    return pair, expiry
