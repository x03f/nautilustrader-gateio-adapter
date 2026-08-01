"""Derive ``MarketStatusAction`` from a Gate.io instrument listing payload.

Gate.io publishes no instrument-status channel on any product: the recorded
public channel inventory (:mod:`gateio_nt.websocket.public`) is trades,
book ticker, depth, snapshots, candlesticks and tickers, and nothing
status-shaped. The only evidence of a halt, a listing or a delisting is in the
instrument listing endpoints, so the status feed is polled — which the
NautilusTrader developer guide names as a supported shape rather than a
compromise, and which is how the three in-tree crypto adapters that implement
the hook (Bybit, Kraken, dYdX) do it.

The guide asks for the diff to live in its own module with a
``diff_and_emit_statuses`` entry point rather than inline in the data client.
The platform's own implementation of that helper is a crate-internal Rust
function; it is absent from ``core/nautilus_pyo3.pyi``, so there is nothing to
import and this is a translation rather than a reimplementation.

Both functions here are pure: they take payload dicts and hand results back
through a callback, so the mapping table can be tested on dicts alone and the
diff on plain dictionaries, without a client, a socket or a clock.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nautilus_trader.model.enums import MarketStatusAction
from nautilus_trader.model.identifiers import InstrumentId

from gateio_nt.common.enums import GateioProductType
from gateio_nt.common.parsing import to_int

#: Venue ``status`` values that mean the contract is on its way off the board.
_DELISTING_STATUSES: frozenset[str] = frozenset({"delisting", "delisted"})


def market_status_action(
    product: GateioProductType,
    payload: dict[str, Any],
    now_secs: int,
) -> tuple[MarketStatusAction, str]:
    """Map one Gate.io listing payload onto a ``MarketStatusAction``.

    ``payload`` is the venue row exactly as ``GET /spot/currency_pairs``,
    ``GET {futures_base}/contracts`` or ``GET /options/contracts`` returns it.
    ``now_secs`` is the observation time in UNIX seconds, used only to decide
    whether a stated start or expiry is still ahead.

    The second element of the result is a human-readable reason naming the venue
    field that decided the action, verbatim. It is carried onto
    ``InstrumentStatus.reason`` so that a strategy reading a halt can see which
    field the venue changed rather than only the adapter's interpretation.

    Only three actions are ever returned. Gate.io's listing payloads distinguish
    tradable, one-sided-with-a-stated-start, and not tradable; every other
    ``MarketStatusAction`` value (``HALT``, ``PAUSE``, ``CROSS``, ``ROTATION``,
    ``PRE_CLOSE``, ...) would be an invention of venue intent.
    """
    if product.is_spot:
        return _spot_status(payload, now_secs)
    if product.is_option:
        return _option_status(payload, now_secs)
    return _contract_status(payload, now_secs)


def _spot_status(payload: dict[str, Any], now_secs: int) -> tuple[MarketStatusAction, str]:
    trade_status = str(payload.get("trade_status") or "").lower()
    if trade_status == "tradable":
        return MarketStatusAction.TRADING, "trade_status=tradable"

    # A one-sided pair with a stated start is the one case where the venue names
    # the transition time instead of leaving it to be inferred: the payload
    # literally says "this side opens at T". `PRE_CLOSE` is never the answer,
    # because Gate.io does not say whether a one-sided window is a listing or a
    # delisting, and choosing between them would be inventing venue intent.
    for field in ("buy_start", "sell_start"):
        starts_at = to_int(payload.get(field))
        if starts_at > now_secs:
            return MarketStatusAction.PRE_OPEN, f"{field}={starts_at}"

    # A pair that trades on one side only is genuinely trading, on that side.
    # It is also genuinely absent from the instrument set this adapter
    # publishes, because `CurrencyPair` cannot express "buys only" and the
    # provider refuses to build one. Reporting `TRADING` here would tell a
    # strategy an instrument is live at the same moment the provider stops
    # offering it. `SHORT_SELL_RESTRICTION_CHANGE` is not the answer either:
    # selling inventory already held is not short selling, and the platform's
    # comment for that value is specifically about short-selling restrictions.
    return (
        MarketStatusAction.NOT_AVAILABLE_FOR_TRADING,
        f"trade_status={trade_status or 'unknown'}",
    )


def _contract_status(payload: dict[str, Any], now_secs: int) -> tuple[MarketStatusAction, str]:
    if payload.get("in_delisting"):
        return MarketStatusAction.NOT_AVAILABLE_FOR_TRADING, "in_delisting=true"
    status = str(payload.get("status") or "").lower()
    if status in _DELISTING_STATUSES:
        return MarketStatusAction.NOT_AVAILABLE_FOR_TRADING, f"status={status}"

    # Delivery contracts carry an expiry; perpetuals and inverse perpetuals do
    # not, and `to_int` reports an absent field as zero rather than as "expired
    # at the epoch".
    expire_secs = to_int(payload.get("expire_time"))
    if 0 < expire_secs <= now_secs:
        return MarketStatusAction.NOT_AVAILABLE_FOR_TRADING, f"expire_time={expire_secs} elapsed"

    # The reason names a field this payload actually carried. Returning
    # "in_delisting=false" whatever arrived quoted a value for a field a partial
    # payload may never have sent, which is the one thing `reason` exists to
    # rule out. The action is unchanged: nothing the venue said states a halt.
    if "in_delisting" in payload:
        return MarketStatusAction.TRADING, "in_delisting=false"
    if status:
        return MarketStatusAction.TRADING, f"status={status}"
    if expire_secs > 0:
        return MarketStatusAction.TRADING, f"expire_time={expire_secs} ahead"
    return MarketStatusAction.TRADING, "no venue field states a halt"


def _option_status(payload: dict[str, Any], now_secs: int) -> tuple[MarketStatusAction, str]:
    # Options have no `in_delisting` field; tradability is `is_active` alone.
    if not payload.get("is_active"):
        return MarketStatusAction.NOT_AVAILABLE_FOR_TRADING, "is_active=false"
    expire_secs = to_int(payload.get("expiration_time"))
    if 0 < expire_secs <= now_secs:
        return (
            MarketStatusAction.NOT_AVAILABLE_FOR_TRADING,
            f"expiration_time={expire_secs} elapsed",
        )
    return MarketStatusAction.TRADING, "is_active=true"


def diff_and_emit_statuses(
    new_statuses: dict[InstrumentId, tuple[MarketStatusAction, str]],
    cached_statuses: dict[InstrumentId, MarketStatusAction],
    subscriptions: set[InstrumentId] | None,
    emit: Callable[[InstrumentId, MarketStatusAction, str], None],
    removable: Callable[[InstrumentId], bool],
) -> None:
    """Emit a status for every instrument whose action changed since the last poll.

    ``cached_statuses`` is updated in place and always reflects the full venue
    state, whether or not anyone is subscribed — otherwise a subscription taken
    out later would have nothing to replay. ``subscriptions`` restricts what is
    emitted, not what is tracked; ``None`` emits everything.

    An instrument the cache holds but the new snapshot does not is treated as
    removed and reported ``NOT_AVAILABLE_FOR_TRADING``, which is the guide's
    stated rule. ``removable`` is the guard on that rule: it must answer
    ``False`` for any instrument whose listing was not actually read this round.
    Gate.io is polled per product family — and per underlying for options — so a
    single failed request would otherwise look exactly like a mass delisting of
    everything that request would have covered. Kraken carries the same guard
    in-tree, and its comment records that the bug was found there first.
    """
    for instrument_id, (action, reason) in new_statuses.items():
        previous = cached_statuses.get(instrument_id)
        cached_statuses[instrument_id] = action
        if previous == action:
            continue
        if subscriptions is None or instrument_id in subscriptions:
            emit(instrument_id, action, reason)

    absent = MarketStatusAction.NOT_AVAILABLE_FOR_TRADING
    for instrument_id in list(cached_statuses):
        if instrument_id in new_statuses:
            continue
        if cached_statuses[instrument_id] == absent:
            continue
        if not removable(instrument_id):
            continue
        cached_statuses[instrument_id] = absent
        if subscriptions is None or instrument_id in subscriptions:
            emit(instrument_id, absent, "absent from the venue listing")


__all__ = [
    "diff_and_emit_statuses",
    "market_status_action",
]
