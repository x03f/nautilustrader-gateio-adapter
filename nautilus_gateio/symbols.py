"""Symbol conversion between Nautilus instrument ids and Gate.io currency pairs.

Canonical form
--------------
The canonical Nautilus symbol used by this adapter is the raw Gate.io currency
pair, underscore included: ``BTC_USDT.GATEIO``. This is lossless for any quote
currency and round-trips trivially.

Compatibility form
------------------
Symbols without an underscore (``BTCUSDT.GATEIO``) are also accepted for
convenience. In that case the quote currency is inferred by longest-match
against a list of known quote suffixes (``USDT``, ``USDC``, ``BTC``, ``ETH``).
This heuristic cannot represent every listing, so the underscore form is
recommended and is what :class:`~nautilus_gateio.providers.GateioInstrumentProvider`
produces.
"""

from __future__ import annotations

from nautilus_trader.model.identifiers import InstrumentId, Symbol

from nautilus_gateio.constants import GATEIO_VENUE

# Ordered longest-first so USDT wins over ... none needed today, kept explicit.
_KNOWN_QUOTES: tuple[str, ...] = ("USDT", "USDC", "BTC", "ETH")


def instrument_id_to_gate_pair(instrument_id: InstrumentId | str) -> str:
    """Convert a Nautilus instrument id (or its string form) to a Gate.io pair.

    ``BTC_USDT.GATEIO`` -> ``BTC_USDT`` (canonical, lossless)
    ``BTCUSDT.GATEIO``  -> ``BTC_USDT`` (heuristic on known quote suffixes)

    Raises ``ValueError`` if the symbol cannot be mapped.
    """
    symbol = str(instrument_id).split(".")[0]
    if not symbol or not symbol.isupper():
        raise ValueError(f"invalid Gate.io symbol: {symbol!r} (must be upper-case)")
    if "_" in symbol:
        base, _, quote = symbol.partition("_")
        if not base or not quote:
            raise ValueError(f"invalid Gate.io pair symbol: {symbol!r}")
        return symbol
    for quote in _KNOWN_QUOTES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[: -len(quote)]}_{quote}"
    raise ValueError(
        f"cannot infer quote currency from symbol {symbol!r}; "
        f"use the underscore form (e.g. 'BTC_USDT.GATEIO')"
    )


def gate_pair_to_instrument_id(pair: str) -> InstrumentId:
    """Convert a Gate.io currency pair (``BTC_USDT``) to the canonical instrument id."""
    pair = pair.upper()
    if "_" not in pair:
        raise ValueError(f"invalid Gate.io currency pair: {pair!r} (expected BASE_QUOTE)")
    return InstrumentId(symbol=Symbol(pair), venue=GATEIO_VENUE)
