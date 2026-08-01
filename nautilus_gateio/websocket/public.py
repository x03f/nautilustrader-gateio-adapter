"""Typed helpers for Gate.io's public WebSocket channels.

One instance wraps one :class:`~nautilus_gateio.websocket.client.GateioWebSocketClient`
and therefore one product, because Gate.io serves each product from its own host
and channel namespace:

===================  =========================  =================================
Product              Channel namespace          Endpoint
===================  =========================  =================================
Spot                 ``spot.*``                 ``wss://api.gateio.ws/ws/v4/``
Perpetual (linear)   ``futures.*``              ``wss://fx-ws.gateio.ws/v4/ws/usdt``
Perpetual (inverse)  ``futures.*``              ``wss://fx-ws.gateio.ws/v4/ws/btc``
Delivery futures     ``futures.*``              ``wss://fx-ws.gateio.ws/v4/ws/delivery/usdt``
Options              ``options.*``              ``wss://op-ws.gateio.live/v4/ws``
===================  =========================  =================================

Perpetual and delivery futures share the ``futures.*`` names and are told apart
only by the endpoint, which is why the product is part of this object's
identity.

Payload shapes delivered to the handler
---------------------------------------
Every message is the full envelope ``{time, channel, event, result}``; ``result``
is described below per channel. Timestamps named ``t`` are milliseconds,
``create_time`` is seconds and ``create_time_ms`` milliseconds.

``<product>.trades``
    Spot: an object ``{id, id_market, create_time, create_time_ms, side,
    currency_pair, amount, price, range}``, where ``side`` is the taker's.
    Futures and options: an array of ``{id, contract, size, create_time,
    create_time_ms, price}``, where ``size`` is signed — positive is a taker
    buy, negative a taker sell — and there is no separate side field.

``<product>.book_ticker``
    ``{t, u, s, b, B, a, A}`` — best bid price and size, best ask price and
    size, plus the order book update id ``u`` from the same counter as the depth
    channels. An empty ``b`` or ``a`` means that side of the book is empty.

``<product>.order_book_update``
    The incremental depth stream: ``{t, s, U, u, b, a}`` plus ``l`` (the depth
    level) on spot and futures, and ``full: true`` on the spot and perpetual
    hosts when the message is a complete snapshot of the subscribed depth.
    ``U`` and ``u`` are the first and last update ids covered. Level containers
    differ per product: spot sends ``[["price", "amount"], ...]`` while futures,
    delivery and options send ``[{"p": price, "s": size}, ...]``. Sizes are
    absolute; zero deletes the level.
    :class:`nautilus_gateio.books.GateioOrderBook` consumes either form.

``<product>.order_book``
    A periodic limited-depth snapshot, self-synchronizing and needing no
    sequence handling. Spot pushes ``event: "update"`` with ``{t, lastUpdateId,
    s, l, bids, asks}``; futures and options push ``event: "all"`` with
    ``{t, id, contract, bids, asks}``.

``<product>.candlesticks``
    An object on spot and an array on futures and options, of ``{t, o, h, l, c,
    v, n}``. ``n`` is ``"<interval>_<symbol>"``. Spot and perpetual add ``a``
    (base-currency amount; ``v`` is the quote-currency turnover on spot) and
    ``w``, which is ``true`` on the message that closes the bar. Delivery and
    options publish no ``w`` flag, so a closed bar has to be inferred either
    from the bucket timestamp advancing or from the clock passing the bucket's
    close (the data client does both).

``<product>.tickers``
    Spot: 24-hour statistics for the pair, and nothing a derivatives data type
    is built from. Futures: an array of objects keyed by ``contract`` that also
    carry ``mark_price``, ``index_price`` and ``funding_rate`` — Gate.io has no
    separate mark, index or funding channel. Options:
    ``options.contract_tickers`` (see :func:`tickers_channel`), keyed by
    ``name`` rather than ``contract``, carrying ``mark_price`` and
    ``index_price`` per contract plus mark IV and the greeks; options pay no
    funding, so there is no ``funding_rate`` on it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Final

from nautilus_gateio.common.constants import CHANNEL_PREFIX, ws_url
from nautilus_gateio.common.enums import GateioProductType
from nautilus_gateio.websocket.client import GateioWebSocketClient

#: Candlestick intervals accepted on the WebSocket candlestick channels.
WS_CANDLE_INTERVALS: Final[frozenset[str]] = frozenset(
    {"10s", "1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d", "7d"}
)


def tickers_channel(product: GateioProductType) -> str:
    """Return the name of the ticker channel for ``product``.

    Options break the ``<prefix>.<channel>`` pattern the other products follow:
    there is no ``options.tickers``. The per-contract stream is
    ``options.contract_tickers`` and ``options.ul_tickers`` is the
    underlying-scoped one. The data client routes an inbound message by
    comparing its channel against this function rather than against a literal of
    its own, so what is subscribed and what is routed cannot drift apart.
    """
    if product.is_option:
        return "options.contract_tickers"
    return f"{CHANNEL_PREFIX[product]}.tickers"


# -- authoritative per-product order book tables ------------------------------
#
# These four tables are the single source of truth for the venue's order book
# limits. The data client imports them rather than restating the numbers, so a
# venue change is made in exactly one place. Values are as documented by Gate.io
# and confirmed against the live venue:
#
# * spot ``order_book_update``: 20 ms or 100 ms; the interval implies the depth
#   (20 ms -> 20 levels, 100 ms -> 100). The 1000 ms interval was withdrawn.
# * perpetual ``futures.order_book_update``: 20 ms or 100 ms, levels 20/50/100,
#   and 20 ms serves 20 levels only.
# * delivery ``futures.order_book_update``: 100 ms or 1000 ms, levels 5..100.
# * options ``options.order_book_update``: 100 ms or 1000 ms, levels 5..50.

#: ``order_book_update`` push intervals in milliseconds, per product.
BOOK_INTERVALS_MS: Final[dict[GateioProductType, tuple[int, ...]]] = {
    GateioProductType.SPOT: (20, 100),
    GateioProductType.PERP: (20, 100),
    GateioProductType.INVERSE: (20, 100),
    GateioProductType.FUT: (100, 1000),
    GateioProductType.OPT: (100, 1000),
}

#: ``order_book_update`` depth levels, per product.
BOOK_LEVELS: Final[dict[GateioProductType, tuple[int, ...]]] = {
    GateioProductType.SPOT: (20, 100),  # implied by the interval, not sent
    GateioProductType.PERP: (20, 50, 100),
    GateioProductType.INVERSE: (20, 50, 100),
    GateioProductType.FUT: (5, 10, 20, 50, 100),
    GateioProductType.OPT: (5, 10, 20, 50),
}

#: ``order_book`` snapshot depths, per product. These are also the depths the
#: REST snapshot endpoint accepts, which is why options top out at 50.
BOOK_SNAPSHOT_LIMITS: Final[dict[GateioProductType, tuple[int, ...]]] = {
    GateioProductType.SPOT: (5, 10, 20, 50, 100),
    GateioProductType.PERP: (1, 5, 10, 20, 50, 100),
    GateioProductType.INVERSE: (1, 5, 10, 20, 50, 100),
    GateioProductType.FUT: (1, 5, 10, 20, 50, 100),
    GateioProductType.OPT: (1, 5, 10, 20, 50),
}

#: ``order_book`` snapshot push intervals; only spot is configurable.
BOOK_SNAPSHOT_PUSH_INTERVALS: Final[dict[GateioProductType, tuple[str, ...]]] = {
    GateioProductType.SPOT: ("100ms", "1000ms"),
    GateioProductType.PERP: ("0",),
    GateioProductType.INVERSE: ("0",),
    GateioProductType.FUT: ("0",),
    GateioProductType.OPT: ("0",),
}

#: Every push interval any product accepts, for configuration-level validation.
#: A configured value in this set may still be adjusted per product; see
#: :data:`BOOK_INTERVALS_MS`, which is authoritative.
ALL_BOOK_INTERVALS_MS: Final[tuple[int, ...]] = tuple(
    sorted({ms for values in BOOK_INTERVALS_MS.values() for ms in values})
)


def book_interval_strs(product: GateioProductType) -> tuple[str, ...]:
    """Return the accepted push intervals for ``product`` in wire form."""
    return tuple(f"{ms}ms" for ms in BOOK_INTERVALS_MS[product])


def nearest_snapshot_limit(product: GateioProductType, limit: int) -> int:
    """Return the closest depth ``product`` accepts for an order book snapshot.

    A depth Gate.io does not serve would be rejected by the API, so it is
    rounded **up** to the next supported depth, or capped at the deepest one the
    product offers (options stop at 50, every other product at 100).
    """
    limits = BOOK_SNAPSHOT_LIMITS[product]
    deeper = [value for value in limits if value >= limit]
    return min(deeper) if deeper else max(limits)


#: Derived wire-form view of :data:`BOOK_INTERVALS_MS`, used to build payloads.
_UPDATE_INTERVALS: Final[dict[GateioProductType, tuple[str, ...]]] = {
    product: tuple(f"{ms}ms" for ms in values) for product, values in BOOK_INTERVALS_MS.items()
}


#: The depth spot serves for each ``spot.order_book_update`` interval.
SPOT_INTERVAL_DEPTH: Final[dict[str, int]] = {"20ms": 20, "100ms": 100}

DEFAULT_BOOK_INTERVAL: Final[str] = "100ms"
DEFAULT_BOOK_LEVEL: Final[int] = 100


class GateioPublicWebSocket:
    """Public market data streams for one Gate.io product.

    Parameters
    ----------
    product : GateioProductType
        The product family to stream.
    handler : Callable[[dict], Any]
        Receives every data message; see the module docstring for the payloads.
    url : str, optional
        Override the endpoint (for example to select a settle-specific options
        host). Defaults to the endpoint for ``product``.
    testnet : bool
        Use the testnet endpoint. Gate.io publishes one only for spot and USDT
        perpetuals.
    loop : asyncio.AbstractEventLoop, optional
        Event loop for the connection's background tasks.
    on_reconnect : Callable[[], Any], optional
        Invoked after subscriptions have been replayed on a new connection.
        Local order books must be rebuilt from a REST snapshot at that point.

    Notes
    -----
    Channels without a typed helper are still reachable through
    :attr:`client`, e.g. ``ws.client.subscribe("options.mark_prices", [symbol])``.
    """

    def __init__(
        self,
        product: GateioProductType,
        handler: Callable[[dict[str, Any]], Any],
        url: str | None = None,
        testnet: bool = False,
        loop: asyncio.AbstractEventLoop | None = None,
        **client_kwargs: Any,
    ) -> None:
        self.product = product
        self.client = GateioWebSocketClient(
            url=url or ws_url(product, testnet),
            product=product,
            handler=handler,
            loop=loop,
            **client_kwargs,
        )

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> None:
        await self.client.connect()

    async def disconnect(self) -> None:
        await self.client.disconnect()

    @property
    def is_connected(self) -> bool:
        return self.client.is_connected

    @property
    def prefix(self) -> str:
        """The channel namespace for this product (``spot``, ``futures`` or ``options``)."""
        return CHANNEL_PREFIX[self.product]

    # -- channel names -----------------------------------------------------

    @property
    def trades_channel(self) -> str:
        return f"{self.prefix}.trades"

    @property
    def book_ticker_channel(self) -> str:
        return f"{self.prefix}.book_ticker"

    @property
    def order_book_update_channel(self) -> str:
        return f"{self.prefix}.order_book_update"

    @property
    def order_book_channel(self) -> str:
        return f"{self.prefix}.order_book"

    @property
    def candlesticks_channel(self) -> str:
        # Options name the per-contract candlestick channel differently, and
        # reserve ``options.ul_candlesticks`` for the underlying.
        if self.product.is_option:
            return "options.contract_candlesticks"
        return f"{self.prefix}.candlesticks"

    @property
    def tickers_channel(self) -> str:
        return tickers_channel(self.product)

    # -- trades ------------------------------------------------------------

    async def subscribe_trades(self, symbol: str) -> dict[str, Any]:
        """Subscribe to public trades for ``symbol`` (venue symbol, not instrument id)."""
        return await self.client.subscribe(self.trades_channel, [symbol])

    async def unsubscribe_trades(self, symbol: str) -> dict[str, Any]:
        return await self.client.unsubscribe(self.trades_channel, [symbol])

    # -- best bid/offer ----------------------------------------------------

    async def subscribe_book_ticker(self, symbol: str) -> dict[str, Any]:
        """Subscribe to the real best bid/ask stream for ``symbol``."""
        return await self.client.subscribe(self.book_ticker_channel, [symbol])

    async def unsubscribe_book_ticker(self, symbol: str) -> dict[str, Any]:
        return await self.client.unsubscribe(self.book_ticker_channel, [symbol])

    # -- incremental depth -------------------------------------------------

    async def subscribe_order_book_update(
        self,
        symbol: str,
        interval: str = DEFAULT_BOOK_INTERVAL,
        level: int | None = None,
    ) -> dict[str, Any]:
        """Subscribe to the incremental depth stream for ``symbol``.

        Parameters
        ----------
        symbol : str
            Venue symbol.
        interval : str
            Push interval. Spot and perpetuals accept ``20ms`` and ``100ms``;
            delivery and options accept ``100ms`` and ``1000ms``.
        level : int, optional
            Depth to maintain. Not a spot parameter — spot derives the depth
            from the interval (``20ms`` gives 20 levels, ``100ms`` gives 100).
            Defaults to the deepest level the product allows for the interval.

        The REST snapshot used to seed the local book must be requested with the
        same depth (see :meth:`effective_depth`); a mismatch leaves the book
        misaligned because updates then cover a different price range.
        """
        payload = self._book_update_payload(symbol, interval, level)
        return await self.client.subscribe(self.order_book_update_channel, payload)

    async def unsubscribe_order_book_update(
        self,
        symbol: str,
        interval: str = DEFAULT_BOOK_INTERVAL,
        level: int | None = None,
    ) -> dict[str, Any]:
        payload = self._book_update_payload(symbol, interval, level)
        return await self.client.unsubscribe(self.order_book_update_channel, payload)

    def _book_update_payload(self, symbol: str, interval: str, level: int | None) -> list[str]:
        intervals = _UPDATE_INTERVALS[self.product]
        if interval not in intervals:
            raise ValueError(
                f"{self.order_book_update_channel} interval {interval!r} is not supported for "
                f"{self.product.value} (supported: {', '.join(intervals)})"
            )
        if self.product.is_spot:
            # ``spot.order_book_update`` takes no level; the interval implies it.
            if level is not None and level != SPOT_INTERVAL_DEPTH[interval]:
                raise ValueError(
                    f"spot.order_book_update at {interval} always serves "
                    f"{SPOT_INTERVAL_DEPTH[interval]} levels; level={level} cannot be requested"
                )
            return [symbol, interval]

        depth = level if level is not None else self.effective_depth(interval)
        levels = BOOK_LEVELS[self.product]
        if depth not in levels:
            raise ValueError(
                f"{self.order_book_update_channel} level {depth} is not supported for "
                f"{self.product.value} (supported: {', '.join(str(item) for item in levels)})"
            )
        if self.product.is_perpetual and interval == "20ms" and depth != 20:
            raise ValueError(
                "futures.order_book_update at 20ms only serves 20 levels; "
                "use 100ms for deeper books"
            )
        return [symbol, interval, str(depth)]

    def effective_depth(
        self,
        interval: str = DEFAULT_BOOK_INTERVAL,
        level: int | None = None,
    ) -> int:
        """Return the depth the incremental stream will actually serve.

        Use it to size the REST snapshot request so that the snapshot and the
        stream cover the same price range.
        """
        if self.product.is_spot:
            return SPOT_INTERVAL_DEPTH[interval] if interval in SPOT_INTERVAL_DEPTH else 100
        if level is not None:
            return level
        if self.product.is_perpetual and interval == "20ms":
            return 20
        return min(DEFAULT_BOOK_LEVEL, max(BOOK_LEVELS[self.product]))

    # -- periodic snapshots ------------------------------------------------

    async def subscribe_order_book_snapshot(
        self,
        symbol: str,
        limit: int = 20,
        interval: str | None = None,
    ) -> dict[str, Any]:
        """Subscribe to the periodic limited-depth snapshot channel.

        This channel needs no sequence handling: every message replaces the
        previous state. ``interval`` applies to spot only (``100ms`` or
        ``1000ms``); futures and options accept the single documented value
        ``"0"``, which this method supplies.
        """
        payload = self._book_snapshot_payload(symbol, limit, interval)
        return await self.client.subscribe(self.order_book_channel, payload)

    async def unsubscribe_order_book_snapshot(
        self,
        symbol: str,
        limit: int = 20,
        interval: str | None = None,
    ) -> dict[str, Any]:
        payload = self._book_snapshot_payload(symbol, limit, interval)
        return await self.client.unsubscribe(self.order_book_channel, payload)

    def _book_snapshot_payload(
        self,
        symbol: str,
        limit: int,
        interval: str | None,
    ) -> list[str]:
        limits = BOOK_SNAPSHOT_LIMITS[self.product]
        if limit not in limits:
            raise ValueError(
                f"{self.order_book_channel} limit {limit} is not supported for "
                f"{self.product.value} (supported: {', '.join(str(item) for item in limits)})"
            )
        intervals = BOOK_SNAPSHOT_PUSH_INTERVALS[self.product]
        resolved = interval if interval is not None else intervals[0]
        if resolved not in intervals:
            raise ValueError(
                f"{self.order_book_channel} interval {resolved!r} is not supported for "
                f"{self.product.value} (supported: {', '.join(intervals)})"
            )
        return [symbol, str(limit), resolved]

    # -- candlesticks ------------------------------------------------------

    async def subscribe_candlesticks(self, symbol: str, interval: str = "1m") -> dict[str, Any]:
        """Subscribe to candlesticks for ``symbol``.

        On the futures hosts the symbol may be prefixed with ``mark_`` or
        ``index_`` to receive mark or index price candles instead.
        """
        return await self.client.subscribe(
            self.candlesticks_channel, self._candles_payload(symbol, interval)
        )

    async def unsubscribe_candlesticks(self, symbol: str, interval: str = "1m") -> dict[str, Any]:
        return await self.client.unsubscribe(
            self.candlesticks_channel, self._candles_payload(symbol, interval)
        )

    def _candles_payload(self, symbol: str, interval: str) -> list[str]:
        if interval not in WS_CANDLE_INTERVALS:
            raise ValueError(
                f"candlestick interval {interval!r} is not supported by Gate.io "
                f"(supported: {', '.join(sorted(WS_CANDLE_INTERVALS))})"
            )
        # Note the argument order: the interval comes first on every product.
        return [interval, symbol]

    # -- tickers -----------------------------------------------------------

    async def subscribe_tickers(self, symbol: str) -> dict[str, Any]:
        """Subscribe to the ticker stream for ``symbol``.

        On futures this is also the source of mark price, index price and
        funding rate; Gate.io publishes no dedicated channel for those.
        """
        return await self.client.subscribe(self.tickers_channel, [symbol])

    async def unsubscribe_tickers(self, symbol: str) -> dict[str, Any]:
        return await self.client.unsubscribe(self.tickers_channel, [symbol])
