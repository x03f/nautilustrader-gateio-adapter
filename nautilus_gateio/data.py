"""Market data client for Gate.io.

One client multiplexes every configured product: spot, USDT perpetual futures,
BTC-settled (inverse) perpetual futures, USDT delivery futures and options. Each
product has its own WebSocket endpoint, so the client opens one public socket
per configured product and routes messages by the endpoint they arrived on and
the channel they name.

Everything published by this client is data the venue actually sent. There are
no synthesised quotes: quotes come from the real ``book_ticker`` best bid/ask
stream, trades from the public trade stream, bars from closed candlesticks, and
order book deltas from the incremental depth stream aligned against a REST
snapshot.

======================================  =======================================
Nautilus subscription                   Gate.io source
======================================  =======================================
``_subscribe_trade_ticks``              ``{spot,futures,options}.trades``
``_subscribe_quote_ticks``              ``{spot,futures,options}.book_ticker``
``_subscribe_order_book_deltas``        REST snapshot + ``*.order_book_update``
``_subscribe_bars``                     ``*.candlesticks`` (closed bars only)
``_subscribe_mark_prices``              ``futures.tickers.mark_price``
``_subscribe_index_prices``             ``futures.tickers.index_price``
``_subscribe_funding_rates``            ``futures.tickers.funding_rate``
======================================  =======================================

Gate.io publishes no instrument definition channel, so instruments are refreshed
by a polling task (``update_instruments_interval_mins``), following the same
approach as other NautilusTrader adapters for venues without such a channel.

Sizes
-----
Gate.io quotes contract sizes with more precision than a contract instrument can
represent (``size_precision`` is ``0`` on every derivative: a Nautilus quantity
is a whole number of contracts). Every size published by this client therefore
goes through :func:`venue_quantity`, which truncates toward zero exactly as the
venue does, and a value that truncates to zero is reported as an absent level or
a skipped quote rather than as a fabricated zero.

The per-product order book limits live in :mod:`nautilus_gateio.websocket.public`
and are imported here; they are not restated.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from decimal import ROUND_DOWN, Decimal
from functools import partial
from typing import Any, Final, NamedTuple

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.enums import LogColor
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.data.messages import (
    RequestBars,
    RequestInstrument,
    RequestInstruments,
    RequestOrderBookSnapshot,
    RequestTradeTicks,
    SubscribeBars,
    SubscribeFundingRates,
    SubscribeIndexPrices,
    SubscribeInstrument,
    SubscribeInstruments,
    SubscribeMarkPrices,
    SubscribeOrderBook,
    SubscribeQuoteTicks,
    SubscribeTradeTicks,
    UnsubscribeBars,
    UnsubscribeFundingRates,
    UnsubscribeIndexPrices,
    UnsubscribeInstrument,
    UnsubscribeInstruments,
    UnsubscribeMarkPrices,
    UnsubscribeOrderBook,
    UnsubscribeQuoteTicks,
    UnsubscribeTradeTicks,
)
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.data import (
    Bar,
    BarType,
    BookOrder,
    FundingRateUpdate,
    IndexPriceUpdate,
    MarkPriceUpdate,
    OrderBookDelta,
    OrderBookDeltas,
    QuoteTick,
    TradeTick,
)
from nautilus_trader.model.enums import (
    AggregationSource,
    AggressorSide,
    BookAction,
    BookType,
    OrderSide,
    PriceType,
    RecordFlag,
    bar_aggregation_to_str,
    book_type_to_str,
)
from nautilus_trader.model.identifiers import ClientId, InstrumentId, TradeId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Quantity

from nautilus_gateio.books import (
    BID,
    GateioOrderBook,
    OrderBookSequenceError,
    SnapshotStaleError,
)
from nautilus_gateio.common.constants import (
    GATEIO_INTERVAL_MS,
    GATEIO_VENUE,
    NAUTILUS_TO_GATEIO_INTERVAL,
)
from nautilus_gateio.common.enums import GateioProductType
from nautilus_gateio.common.errors import GateioError
from nautilus_gateio.common.parsing import to_decimal, to_float, to_int
from nautilus_gateio.common.symbols import (
    gateio_to_instrument_id,
    instrument_id_to_gateio,
)
from nautilus_gateio.config import (
    GateioDataClientConfig,
    validate_book_interval_ms,
    validate_products,
    validate_snapshot_limit,
)
from nautilus_gateio.http.client import GateioHttpClient
from nautilus_gateio.http.futures import GateioFuturesHttpAPI
from nautilus_gateio.http.options import GateioOptionsHttpAPI
from nautilus_gateio.http.spot import GateioSpotHttpAPI
from nautilus_gateio.websocket.client import is_transient_ws_error
from nautilus_gateio.websocket.public import (
    BOOK_INTERVALS_MS,
    BOOK_LEVELS,
    GateioPublicWebSocket,
    nearest_snapshot_limit,
)

#: Maximum rows Gate.io returns from one candlestick or trade request.
MAX_REST_LIMIT: Final[int] = 1000

#: Attempts to align a REST snapshot with the buffered incremental stream before
#: backing off. Gate.io's snapshot lags the stream by around a second, so the
#: first attempt occasionally lands behind the first buffered notification.
SNAPSHOT_ATTEMPTS: Final[int] = 4

#: Delay before a failed book synchronisation is attempted again.
SNAPSHOT_RETRY_DELAY_SECS: Final[float] = 5.0

#: How long a candlestick bucket may stay pending after it closed before it is
#: published without a window-close flag. Delivery and option candlesticks carry
#: no ``w`` flag at all, and Gate.io documents that it "may be missing" on the
#: other products, so a bar that is demonstrably in the past is released on the
#: clock rather than waiting for a candle from the next bucket, which on an
#: illiquid contract may never arrive.
BAR_CLOSE_GRACE_SECS: Final[float] = 5.0

#: How often the pending-bar flush runs.
BAR_FLUSH_INTERVAL_SECS: Final[float] = 1.0

#: Subscription kinds sharing the ``futures.tickers`` channel.
_MARK: Final[str] = "mark"
_INDEX: Final[str] = "index"
_FUNDING: Final[str] = "funding"

_MS_THRESHOLD: Final[float] = 1e12

_NANOS_PER_SEC: Final[int] = 1_000_000_000


class _PendingBar(NamedTuple):
    """A candlestick bucket awaiting confirmation that it has closed."""

    open_secs: int
    bar: Bar
    flush_at_ns: int


def _restamp_bar(bar: Bar, ts_init: int) -> Bar:
    """Return ``bar`` with a fresh ``ts_init``; ``Bar`` is immutable."""
    return Bar(
        bar_type=bar.bar_type,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        ts_event=bar.ts_event,
        ts_init=ts_init,
    )


def venue_quantity(value: Any, precision: int) -> Quantity:
    """Convert a Gate.io size field to a ``Quantity``, truncating toward zero.

    Gate.io sizes are absolute amounts (base currency on spot, contract counts
    on every derivative). A value carrying more decimals than the instrument can
    represent must not be rounded **up**: that would publish size the venue never
    showed. Truncation toward zero is also exactly what Gate.io itself does to
    futures sizes when the ``X-Gate-Size-Decimal`` opt-in is not requested (its
    changelog spells out that "the size of 1.1, 1.5, and 1.7 will be 1"), so the
    adapter behaves identically whether or not the venue sends fractions.

    A value that truncates to zero is therefore a level or quote the adapter
    cannot represent; callers must treat it as absent rather than publishing a
    zero-sized book entry, which NautilusTrader rejects outright.
    """
    parsed = to_decimal(value)
    try:
        if precision <= 0:
            truncated = parsed.to_integral_value(rounding=ROUND_DOWN)
        else:
            truncated = parsed.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)
    except ArithmeticError:  # pragma: no cover - implausibly large venue size
        truncated = parsed
    return Quantity(truncated, precision)


def timestamp_to_nanos(value: Any) -> int:
    """Convert a Gate.io timestamp field to UNIX nanoseconds.

    Gate.io is inconsistent about the unit: WebSocket messages use milliseconds,
    several REST endpoints report seconds in a field named ``create_time_ms``,
    and some carry fractional seconds. The magnitude disambiguates them, since
    a seconds timestamp is roughly a thousand times smaller than the same
    instant in milliseconds.
    """
    number = to_float(value)
    if number <= 0.0:
        return 0
    if number >= _MS_THRESHOLD:
        return int(number * 1_000_000)
    return int(number * 1_000_000_000)


def bar_type_to_interval(bar_type: BarType) -> str:
    """Return the Gate.io candlestick interval for ``bar_type``.

    Raises
    ------
    ValueError
        If the bar specification has no Gate.io equivalent.

    """
    spec = bar_type.spec
    if spec.price_type != PriceType.LAST:
        raise ValueError(
            f"Gate.io only publishes last-price candlesticks, was {bar_type}",
        )
    token = f"{spec.step}-{bar_aggregation_to_str(spec.aggregation)}"
    interval = NAUTILUS_TO_GATEIO_INTERVAL.get(token)
    if interval is None:
        raise ValueError(
            f"Gate.io has no candlestick interval for {token}; supported: "
            f"{', '.join(sorted(NAUTILUS_TO_GATEIO_INTERVAL))}",
        )
    return interval


class GateioDataClient(LiveMarketDataClient):
    """Provides a market data client for Gate.io.

    Parameters
    ----------
    loop : asyncio.AbstractEventLoop
        The event loop for the client.
    client_id : ClientId
        The client id.
    msgbus : MessageBus
        The message bus for the client.
    cache : Cache
        The cache for the client.
    clock : LiveClock
        The clock for the client.
    instrument_provider : InstrumentProvider
        The instrument provider (already configured for the same products).
    http_client : GateioHttpClient
        The shared REST transport.
    config : GateioDataClientConfig
        The client configuration.

    Raises
    ------
    ValueError
        If the configuration is inconsistent (empty or environment-incompatible
        product set, unsupported book interval or snapshot depth). The
        configuration struct is frozen, so this validation happens here rather
        than on the struct.

    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        client_id: ClientId,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: Any,
        http_client: GateioHttpClient,
        config: GateioDataClientConfig,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=client_id,
            venue=GATEIO_VENUE,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
            config=config,
        )
        # Cross-field validation of the frozen configuration struct
        self._products: tuple[GateioProductType, ...] = validate_products(
            config.products,
            config.environment,
        )
        self._book_interval_ms: int = validate_book_interval_ms(
            config.order_book_update_interval_ms,
        )
        self._snapshot_limit: int = validate_snapshot_limit(config.order_book_snapshot_limit)

        self._config = config
        self._http_client = http_client
        self._spot_http = GateioSpotHttpAPI(http_client)
        self._options_http = GateioOptionsHttpAPI(http_client)
        self._futures_http: dict[GateioProductType, GateioFuturesHttpAPI] = {
            GateioProductType.PERP: GateioFuturesHttpAPI(http_client, settle="usdt"),
            GateioProductType.INVERSE: GateioFuturesHttpAPI(http_client, settle="btc"),
            GateioProductType.FUT: GateioFuturesHttpAPI(
                http_client,
                settle="usdt",
                delivery=True,
            ),
        }

        self._ws_clients: dict[GateioProductType, GateioPublicWebSocket] = {}

        # Subscription state
        self._books: dict[InstrumentId, GateioOrderBook] = {}
        self._book_streams: dict[InstrumentId, tuple[str, int | None]] = {}
        self._book_levels: dict[InstrumentId, int] = {}
        self._resyncing: set[InstrumentId] = set()
        self._bar_types: dict[tuple[GateioProductType, str], BarType] = {}
        self._bar_pending: dict[BarType, _PendingBar] = {}
        self._bar_published: dict[BarType, int] = {}
        self._ticker_subs: dict[InstrumentId, set[str]] = defaultdict(set)
        self._update_instruments_task: asyncio.Task | None = None
        self._bar_flush_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()

        # Metrics
        self._reconnects: dict[str, int] = defaultdict(int)
        self._messages: dict[str, int] = defaultdict(int)
        self._gaps: dict[str, int] = defaultdict(int)
        self._resyncs: dict[str, int] = defaultdict(int)
        self._snapshot_retries: dict[str, int] = defaultdict(int)
        self._snapshot_errors: dict[str, int] = defaultdict(int)
        self._published: dict[str, int] = defaultdict(int)

        self._log.info(f"Products: {', '.join(p.value for p in self._products)}", LogColor.BLUE)
        self._log.info(f"Environment: {config.environment}", LogColor.BLUE)
        self._log.info(f"REST base URL: {config.resolve_http_url()}", LogColor.BLUE)

    # -- properties --------------------------------------------------------

    @property
    def products(self) -> tuple[GateioProductType, ...]:
        """Return the products this client serves."""
        return self._products

    def metrics(self) -> dict[str, Any]:
        """Return a snapshot of the client's stream health counters.

        The counters are cumulative since construction and are keyed by product
        value where they are per-product.

        ``gaps`` counts sequence breaks in a live depth stream, each of which
        forces a ``resync``. ``snapshot_retries`` counts the separate case where
        a REST snapshot arrived older than the buffered notifications and had to
        be re-fetched, which is routine at subscription time and not a data
        loss. ``snapshot_errors`` counts REST failures during a seed or resync;
        those are retried, never fatal to the subscription. ``book_gaps``
        reports every discontinuity each local book has seen, including those
        retries, and ``books_synced`` shows how many books are currently aligned
        with the venue.
        """
        book_gaps = {
            str(instrument_id): book.gaps_detected
            for instrument_id, book in self._books.items()
            if book.gaps_detected
        }
        reconnects = dict(self._reconnects)
        connections: dict[str, dict[str, Any]] = {}
        for product, ws in self._ws_clients.items():
            stats = ws.client.stats()
            connections[product.value] = stats
            reconnects[product.value] = int(stats.get("reconnects", 0))
        return {
            "reconnects": reconnects,
            "gaps": dict(self._gaps),
            "resyncs": dict(self._resyncs),
            "snapshot_retries": dict(self._snapshot_retries),
            "snapshot_errors": dict(self._snapshot_errors),
            "messages": dict(self._messages),
            "published": dict(self._published),
            "book_gaps": book_gaps,
            "books": len(self._books),
            "books_synced": sum(1 for book in self._books.values() if book.is_synced),
            "connections": connections,
        }

    # -- lifecycle ---------------------------------------------------------

    async def _connect(self) -> None:
        await self._instrument_provider.initialize()
        self._send_all_instruments_to_data_engine()

        for product in self._products:
            url = self._config.resolve_ws_url(product)
            client = GateioPublicWebSocket(
                product=product,
                handler=partial(self._handle_ws_message, product),
                url=url,
                testnet=self._config.is_testnet,
                loop=self._loop,
                on_reconnect=partial(self._handle_ws_reconnect, product),
            )
            self._ws_clients[product] = client
            await client.connect()
            self._log.info(f"Connected public WebSocket for {product.value}: {url}")

        interval = self._config.update_instruments_interval_mins
        if interval:
            self._update_instruments_task = self.create_task(
                self._update_instruments(interval),
            )
        self._bar_flush_task = self.create_task(self._flush_bars_loop())

    async def _disconnect(self) -> None:
        if self._update_instruments_task is not None:
            self._update_instruments_task.cancel()
            self._update_instruments_task = None
        if self._bar_flush_task is not None:
            self._bar_flush_task.cancel()
            self._bar_flush_task = None

        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

        for product, client in self._ws_clients.items():
            try:
                await client.disconnect()
            except Exception as e:  # noqa: BLE001 - shutdown must not raise
                self._log.warning(f"Error disconnecting {product.value} WebSocket: {e}")
        self._ws_clients.clear()

    def _track(self, coro: Any, log_msg: str) -> None:
        """Run ``coro`` as a tracked background task."""
        task = self.create_task(coro, log_msg=log_msg)
        if task is not None:
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    # -- instruments -------------------------------------------------------

    def _send_all_instruments_to_data_engine(self) -> None:
        for currency in self._instrument_provider.currencies().values():
            self._cache.add_currency(currency)
        for instrument in self._instrument_provider.get_all().values():
            self._handle_data(instrument)

    async def _update_instruments(self, interval_mins: int) -> None:
        while True:
            try:
                await asyncio.sleep(interval_mins * 60)
                await self._instrument_provider.initialize(reload=True)
                self._send_all_instruments_to_data_engine()
                self._log.debug("Reloaded instruments")
            except asyncio.CancelledError:
                self._log.debug("Canceled task 'update_instruments'")
                return
            except Exception as e:  # noqa: BLE001 - the task must survive venue errors
                self._log.error(f"Error updating instruments: {e}")

    def _instrument(self, instrument_id: InstrumentId) -> Instrument | None:
        instrument = self._cache.instrument(instrument_id)
        if instrument is None:
            instrument = self._instrument_provider.find(instrument_id)
        return instrument

    # -- routing helpers ---------------------------------------------------

    def _resolve(self, instrument_id: InstrumentId) -> tuple[GateioProductType, str] | None:
        """Return ``(product, venue symbol)`` if the product is configured."""
        try:
            product, raw_symbol = instrument_id_to_gateio(instrument_id)
        except ValueError as e:
            self._log.error(f"Cannot route {instrument_id}: {e}")
            return None
        if product not in self._products:
            self._log.error(
                f"Cannot handle {instrument_id}: product {product.value} is not configured "
                f"(configured: {', '.join(p.value for p in self._products)})",
            )
            return None
        return product, raw_symbol

    def _ws(self, product: GateioProductType) -> GateioPublicWebSocket | None:
        client = self._ws_clients.get(product)
        if client is None:
            self._log.error(f"No WebSocket connection for {product.value}")
        return client

    def _resolve_book_stream(
        self,
        product: GateioProductType,
        depth: int,
    ) -> tuple[str, int | None]:
        """Return the ``(interval, level)`` pair to subscribe for ``product``.

        ``level`` is ``None`` for spot, which derives the depth from the push
        interval rather than accepting one. Values the product does not serve
        are adjusted to the nearest supported value with a warning, rather than
        failing the subscription.
        """
        interval_ms = self._book_interval_ms
        allowed_intervals = BOOK_INTERVALS_MS[product]
        if interval_ms not in allowed_intervals:
            # Prefer 100ms, the interval every product serves at full depth, over
            # the numerically smallest one, which on spot and the perpetuals
            # would silently cut the stream down to 20 levels.
            fallback = 100 if 100 in allowed_intervals else min(allowed_intervals)
            self._log.warning(
                f"{product.value} does not accept a {interval_ms}ms book interval; "
                f"using {fallback}ms (accepted: {allowed_intervals})",
            )
            interval_ms = fallback

        if product.is_spot:
            # Spot takes no level: 20ms streams 20 levels, otherwise 100.
            implied = 20 if interval_ms == 20 else 100
            if depth and depth != implied:
                self._log.warning(
                    f"Spot book depth {depth} is not selectable; the {interval_ms}ms "
                    f"stream publishes {implied} levels",
                )
            return f"{interval_ms}ms", None

        allowed_levels = BOOK_LEVELS[product]
        level = depth or self._snapshot_limit
        if level not in allowed_levels:
            candidates = [x for x in allowed_levels if x >= level]
            adjusted = min(candidates) if candidates else max(allowed_levels)
            self._log.warning(
                f"{product.value} does not stream {level} book levels; using {adjusted} "
                f"(accepted: {allowed_levels})",
            )
            level = adjusted
        if interval_ms == 20 and level != 20:
            self._log.warning(
                f"{product.value} only streams 20 levels at 20ms; using a 100ms interval "
                f"for {level} levels",
            )
            interval_ms = 100
        return f"{interval_ms}ms", level

    # -- subscriptions -----------------------------------------------------

    async def _subscribe_instruments(self, command: SubscribeInstruments) -> None:
        self._log.info(
            "Gate.io publishes no instrument definition channel; instrument updates come "
            f"from the reload task every {self._config.update_instruments_interval_mins} minutes",
        )
        self._send_all_instruments_to_data_engine()

    async def _subscribe_instrument(self, command: SubscribeInstrument) -> None:
        instrument = self._instrument(command.instrument_id)
        if instrument is None:
            self._log.error(f"Cannot subscribe: no instrument for {command.instrument_id}")
            return
        self._handle_data(instrument)

    async def _subscribe_trade_ticks(self, command: SubscribeTradeTicks) -> None:
        resolved = self._resolve(command.instrument_id)
        if resolved is None:
            return
        product, raw_symbol = resolved
        client = self._ws(product)
        if client is None:
            return
        try:
            await client.subscribe_trades(raw_symbol)
        except (GateioError, ValueError) as e:
            self._log.error(f"Cannot subscribe to trades for {command.instrument_id}: {e}")

    async def _unsubscribe_trade_ticks(self, command: UnsubscribeTradeTicks) -> None:
        resolved = self._resolve(command.instrument_id)
        if resolved is None:
            return
        product, raw_symbol = resolved
        client = self._ws(product)
        if client is None:
            return
        try:
            await client.unsubscribe_trades(raw_symbol)
        except (GateioError, ValueError) as e:
            self._log.error(f"Cannot unsubscribe from trades for {command.instrument_id}: {e}")

    async def _subscribe_quote_ticks(self, command: SubscribeQuoteTicks) -> None:
        resolved = self._resolve(command.instrument_id)
        if resolved is None:
            return
        product, raw_symbol = resolved
        client = self._ws(product)
        if client is None:
            return
        try:
            await client.subscribe_book_ticker(raw_symbol)
        except (GateioError, ValueError) as e:
            self._log.error(f"Cannot subscribe to quotes for {command.instrument_id}: {e}")

    async def _unsubscribe_quote_ticks(self, command: UnsubscribeQuoteTicks) -> None:
        resolved = self._resolve(command.instrument_id)
        if resolved is None:
            return
        product, raw_symbol = resolved
        client = self._ws(product)
        if client is None:
            return
        try:
            await client.unsubscribe_book_ticker(raw_symbol)
        except (GateioError, ValueError) as e:
            self._log.error(f"Cannot unsubscribe from quotes for {command.instrument_id}: {e}")

    async def _subscribe_order_book_deltas(self, command: SubscribeOrderBook) -> None:
        if command.book_type != BookType.L2_MBP:
            self._log.error(
                f"Cannot subscribe to {book_type_to_str(command.book_type)} order book: "
                f"Gate.io publishes L2_MBP depth only",
            )
            return
        resolved = self._resolve(command.instrument_id)
        if resolved is None:
            return
        product, raw_symbol = resolved
        client = self._ws(product)
        if client is None:
            return
        instrument_id = command.instrument_id
        if instrument_id in self._books:
            self._log.warning(f"Already subscribed to order book deltas for {instrument_id}")
            return

        interval, level = self._resolve_book_stream(product, command.depth)

        # The book is registered before subscribing so that no notification
        # arriving with (or immediately after) the acknowledgement is dropped;
        # notifications received before the snapshot are buffered by the book.
        # Gate.io requires the REST snapshot depth to match the streamed depth.
        self._books[instrument_id] = GateioOrderBook(raw_symbol)
        self._book_levels[instrument_id] = client.effective_depth(interval, level)
        self._book_streams[instrument_id] = (interval, level)
        try:
            await client.subscribe_order_book_update(raw_symbol, interval=interval, level=level)
        except (GateioError, ValueError) as e:
            if is_transient_ws_error(e):
                # The socket is reconnecting or the acknowledgement was late.
                # The WebSocket client keeps the subscription and replays it, so
                # the local book is kept too and resnapshotted on reconnect.
                self._log.warning(
                    f"Order book subscription for {instrument_id} did not complete ({e}); "
                    f"it will be replayed on the next connection",
                )
                return
            self._books.pop(instrument_id, None)
            self._book_levels.pop(instrument_id, None)
            self._book_streams.pop(instrument_id, None)
            self._log.error(f"Cannot subscribe to the order book for {instrument_id}: {e}")
            return

        self._track(
            self._book_snapshot_then_deltas(instrument_id),
            log_msg=f"book snapshot {instrument_id}",
        )

    async def _unsubscribe_order_book_deltas(self, command: UnsubscribeOrderBook) -> None:
        instrument_id = command.instrument_id
        stream = self._book_streams.pop(instrument_id, None)
        self._books.pop(instrument_id, None)
        self._book_levels.pop(instrument_id, None)
        if stream is None:
            return
        resolved = self._resolve(instrument_id)
        if resolved is None:
            return
        product, raw_symbol = resolved
        client = self._ws(product)
        if client is None:
            return
        interval, level = stream
        try:
            await client.unsubscribe_order_book_update(raw_symbol, interval=interval, level=level)
        except (GateioError, ValueError) as e:
            self._log.error(f"Cannot unsubscribe from the order book for {instrument_id}: {e}")

    async def _subscribe_bars(self, command: SubscribeBars) -> None:
        bar_type = command.bar_type
        if bar_type.aggregation_source != AggregationSource.EXTERNAL:
            self._log.error(
                f"Cannot subscribe to {bar_type}: only EXTERNAL aggregation is served by "
                f"Gate.io candlesticks",
            )
            return
        resolved = self._resolve(bar_type.instrument_id)
        if resolved is None:
            return
        product, raw_symbol = resolved
        try:
            interval = bar_type_to_interval(bar_type)
        except ValueError as e:
            self._log.error(f"Cannot subscribe to {bar_type}: {e}")
            return
        client = self._ws(product)
        if client is None:
            return

        self._bar_types[(product, f"{interval}_{raw_symbol}")] = bar_type
        try:
            await client.subscribe_candlesticks(raw_symbol, interval=interval)
        except (GateioError, ValueError) as e:
            if is_transient_ws_error(e):
                self._log.warning(
                    f"Candlestick subscription for {bar_type} did not complete ({e}); "
                    f"it will be replayed on the next connection",
                )
                return
            self._bar_types.pop((product, f"{interval}_{raw_symbol}"), None)
            self._log.error(f"Cannot subscribe to {bar_type}: {e}")

    async def _unsubscribe_bars(self, command: UnsubscribeBars) -> None:
        bar_type = command.bar_type
        resolved = self._resolve(bar_type.instrument_id)
        if resolved is None:
            return
        product, raw_symbol = resolved
        try:
            interval = bar_type_to_interval(bar_type)
        except ValueError as e:
            self._log.error(f"Cannot unsubscribe from {bar_type}: {e}")
            return
        self._bar_types.pop((product, f"{interval}_{raw_symbol}"), None)
        self._bar_pending.pop(bar_type, None)
        self._bar_published.pop(bar_type, None)
        client = self._ws(product)
        if client is None:
            return
        try:
            await client.unsubscribe_candlesticks(raw_symbol, interval=interval)
        except (GateioError, ValueError) as e:
            self._log.error(f"Cannot unsubscribe from {bar_type}: {e}")

    # -- futures ticker derived streams ------------------------------------

    async def _subscribe_ticker_stream(self, instrument_id: InstrumentId, kind: str) -> None:
        """Subscribe ``futures.tickers``, which carries mark, index and funding."""
        resolved = self._resolve(instrument_id)
        if resolved is None:
            return
        product, raw_symbol = resolved
        if not product.is_futures:
            self._log.error(
                f"Cannot subscribe to {kind} prices for {instrument_id}: Gate.io publishes "
                f"them for futures products only",
            )
            return
        if kind == _FUNDING and not product.is_perpetual:
            # A delivery contract converges on its settlement price instead of
            # paying funding, and its ticker reports a basis rather than a
            # funding rate, so the subscription would never produce data.
            self._log.error(
                f"Cannot subscribe to funding rates for {instrument_id}: only perpetual "
                f"contracts pay funding on Gate.io",
            )
            return
        client = self._ws(product)
        if client is None:
            return
        first = not self._ticker_subs[instrument_id]
        self._ticker_subs[instrument_id].add(kind)
        if first:
            try:
                await client.subscribe_tickers(raw_symbol)
            except (GateioError, ValueError) as e:
                self._ticker_subs.pop(instrument_id, None)
                self._log.error(f"Cannot subscribe to {kind} data for {instrument_id}: {e}")

    async def _unsubscribe_ticker_stream(self, instrument_id: InstrumentId, kind: str) -> None:
        kinds = self._ticker_subs.get(instrument_id)
        if not kinds:
            return
        kinds.discard(kind)
        if kinds:
            return  # another data type still needs the channel
        self._ticker_subs.pop(instrument_id, None)
        resolved = self._resolve(instrument_id)
        if resolved is None:
            return
        product, raw_symbol = resolved
        client = self._ws(product)
        if client is None:
            return
        try:
            await client.unsubscribe_tickers(raw_symbol)
        except (GateioError, ValueError) as e:
            self._log.error(f"Cannot unsubscribe from {kind} data for {instrument_id}: {e}")

    async def _subscribe_mark_prices(self, command: SubscribeMarkPrices) -> None:
        await self._subscribe_ticker_stream(command.instrument_id, _MARK)

    async def _unsubscribe_mark_prices(self, command: UnsubscribeMarkPrices) -> None:
        await self._unsubscribe_ticker_stream(command.instrument_id, _MARK)

    async def _subscribe_index_prices(self, command: SubscribeIndexPrices) -> None:
        await self._subscribe_ticker_stream(command.instrument_id, _INDEX)

    async def _unsubscribe_index_prices(self, command: UnsubscribeIndexPrices) -> None:
        await self._unsubscribe_ticker_stream(command.instrument_id, _INDEX)

    async def _subscribe_funding_rates(self, command: SubscribeFundingRates) -> None:
        await self._subscribe_ticker_stream(command.instrument_id, _FUNDING)

    async def _unsubscribe_funding_rates(self, command: UnsubscribeFundingRates) -> None:
        await self._unsubscribe_ticker_stream(command.instrument_id, _FUNDING)

    async def _unsubscribe_instruments(self, command: UnsubscribeInstruments) -> None:
        pass  # No venue channel to unsubscribe from

    async def _unsubscribe_instrument(self, command: UnsubscribeInstrument) -> None:
        pass  # No venue channel to unsubscribe from

    # -- WebSocket handling ------------------------------------------------

    def _handle_ws_message(self, product: GateioProductType, msg: dict[str, Any]) -> None:
        self._messages[product.value] += 1
        channel = msg.get("channel")
        if not isinstance(channel, str):
            return
        event = msg.get("event")

        if event in ("subscribe", "unsubscribe"):
            error = msg.get("error")
            if error:
                self._log.error(f"{channel} {event} failed: {error}")
            else:
                self._log.debug(f"{channel} {event} acknowledged")
            return
        if event not in ("update", "all"):
            return

        result = msg.get("result")
        if result is None:
            return

        try:
            if channel.endswith(".trades"):
                self._handle_trades(product, result)
            elif channel.endswith(".book_ticker"):
                self._handle_book_ticker(product, result)
            elif channel.endswith(".order_book_update"):
                self._handle_book_update(product, result)
            elif channel.endswith("candlesticks"):
                self._handle_candlesticks(product, result)
            elif channel == "futures.tickers":
                self._handle_tickers(product, result)
        except Exception as e:  # noqa: BLE001 - one bad message must not kill the stream
            self._log.exception(f"Error handling {channel} message", e)

    async def _handle_ws_reconnect(self, product: GateioProductType) -> None:
        """Rebuild every local book after a reconnect.

        The WebSocket client replays the subscription set itself (Gate.io
        subscriptions are additive and there is no server-side session resume),
        but any local book is stale by definition after a disconnect, so each
        one is rebuilt from a fresh REST snapshot.
        """
        self._reconnects[product.value] += 1
        book_ids = self._subscribed_book_ids(product)
        self._log.warning(
            f"Reconnected {product.value} WebSocket; resynchronising {len(book_ids)} order books",
        )
        for instrument_id in book_ids:
            book = self._books.get(instrument_id)
            if book is not None:
                book.reset()
            self._resyncs[product.value] += 1
            self._track(
                self._book_snapshot_then_deltas(instrument_id),
                log_msg=f"book resnapshot {instrument_id}",
            )

    def _subscribed_book_ids(self, product: GateioProductType) -> list[InstrumentId]:
        return [
            instrument_id
            for instrument_id in self._books
            if instrument_id_to_gateio(instrument_id)[0] is product
        ]

    # -- order book --------------------------------------------------------

    async def _fetch_book_snapshot(
        self,
        product: GateioProductType,
        raw_symbol: str,
        limit: int,
    ) -> dict[str, Any]:
        if product.is_spot:
            return await self._spot_http.order_book(raw_symbol, limit=limit, with_id=True)
        if product.is_option:
            return await self._options_http.order_book(raw_symbol, limit=limit, with_id=True)
        return await self._futures_http[product].order_book(
            raw_symbol,
            limit=limit,
            with_id=True,
        )

    async def _book_snapshot_then_deltas(self, instrument_id: InstrumentId) -> None:
        """Seed a local book from REST and publish it as a snapshot batch."""
        if instrument_id in self._resyncing:
            return
        self._resyncing.add(instrument_id)
        try:
            resolved = self._resolve(instrument_id)
            book = self._books.get(instrument_id)
            if resolved is None or book is None:
                return
            product, raw_symbol = resolved
            limit = nearest_snapshot_limit(
                product,
                self._book_levels.get(instrument_id, self._snapshot_limit),
            )

            for attempt in range(SNAPSHOT_ATTEMPTS):
                last_attempt = attempt + 1 >= SNAPSHOT_ATTEMPTS
                try:
                    # The fetch is inside the retry scope on purpose: a REST
                    # failure here (network error, 5xx, exhausted 429 retries)
                    # must take the same retry path as a sequence mismatch. If
                    # it escaped, the subscription would be left with an
                    # unsynchronised book that buffers every further
                    # notification and never publishes again.
                    payload = await self._fetch_book_snapshot(product, raw_symbol, limit)
                    book.apply_snapshot(payload)
                    break
                except SnapshotStaleError as e:
                    # The stream resynced the book while this request was in
                    # flight; the local state is newer than the snapshot and
                    # already correct.
                    self._log.debug(f"Discarding a stale snapshot for {instrument_id}: {e}")
                    return
                except OrderBookSequenceError as e:
                    # Gate.io serves the REST snapshot with a latency of roughly
                    # a second, so it occasionally reflects a state older than
                    # the first buffered notification. The documented remedy is
                    # simply to fetch a newer snapshot.
                    self._snapshot_retries[product.value] += 1
                    self._log.debug(f"Retrying the snapshot for {instrument_id}: {e}")
                    if last_attempt:
                        self._log.warning(
                            f"Could not synchronise the order book for {instrument_id} in "
                            f"{SNAPSHOT_ATTEMPTS} attempts; retrying in "
                            f"{SNAPSHOT_RETRY_DELAY_SECS:.0f}s",
                        )
                        self._schedule_book_retry(instrument_id)
                        return
                    await asyncio.sleep(0.5)
                except (GateioError, ValueError) as e:
                    self._snapshot_errors[product.value] += 1
                    self._log.warning(f"Order book snapshot for {instrument_id} failed: {e}")
                    if last_attempt:
                        self._log.warning(
                            f"Could not fetch an order book snapshot for {instrument_id} in "
                            f"{SNAPSHOT_ATTEMPTS} attempts; retrying in "
                            f"{SNAPSHOT_RETRY_DELAY_SECS:.0f}s",
                        )
                        self._schedule_book_retry(instrument_id)
                        return
                    await asyncio.sleep(0.5)

            deltas = self._snapshot_deltas(instrument_id, book)
            if deltas is not None:
                self._handle_data(deltas)
                self._published["order_book_snapshots"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - reported, never propagated to the loop
            # An unexpected failure must not silence the subscription either.
            self._log.error(f"Error building the order book for {instrument_id}: {e}")
            self._schedule_book_retry(instrument_id)
        finally:
            self._resyncing.discard(instrument_id)

    def _schedule_book_retry(self, instrument_id: InstrumentId) -> None:
        """Re-attempt a failed book synchronisation after a delay.

        A book that cannot be aligned is never abandoned: without a retry the
        subscription would stay silent for the life of the process, since every
        further notification is buffered while the book is unsynchronised.
        """

        async def _retry() -> None:
            await asyncio.sleep(SNAPSHOT_RETRY_DELAY_SECS)
            if instrument_id not in self._books:
                return  # unsubscribed while waiting
            await self._book_snapshot_then_deltas(instrument_id)

        self._track(_retry(), log_msg=f"book snapshot retry {instrument_id}")

    def _snapshot_deltas(
        self,
        instrument_id: InstrumentId,
        book: GateioOrderBook,
    ) -> OrderBookDeltas | None:
        """Build a CLEAR-then-ADD delta batch describing the whole local book."""
        instrument = self._instrument(instrument_id)
        if instrument is None:
            self._log.error(f"Cannot publish an order book snapshot: no {instrument_id}")
            return None

        ts_event = book.last_update_ms * 1_000_000
        ts_init = self._clock.timestamp_ns()
        if ts_event == 0:
            ts_event = ts_init
        sequence = book.last_update_id
        flags = RecordFlag.F_SNAPSHOT

        deltas: list[OrderBookDelta] = [
            OrderBookDelta(
                instrument_id=instrument_id,
                action=BookAction.CLEAR,
                order=BookOrder(
                    side=OrderSide.NO_ORDER_SIDE,
                    price=instrument.make_price(0),
                    size=Quantity(0, instrument.size_precision),
                    order_id=0,
                ),
                flags=flags,
                sequence=sequence,
                ts_event=ts_event,
                ts_init=ts_init,
            ),
        ]
        for side, levels in ((OrderSide.BUY, book.bids()), (OrderSide.SELL, book.asks())):
            for price, size in levels:
                quantity = venue_quantity(size, instrument.size_precision)
                if quantity == 0:
                    # A level the instrument's size precision cannot represent
                    # (see `venue_quantity`). It is reported as absent rather
                    # than added with a fabricated size; NautilusTrader rejects
                    # a zero-sized ADD outright.
                    self._published["book_levels_not_representable"] += 1
                    continue
                deltas.append(
                    OrderBookDelta(
                        instrument_id=instrument_id,
                        action=BookAction.ADD,
                        order=BookOrder(
                            side=side,
                            price=instrument.make_price(price),
                            size=quantity,
                            order_id=0,
                        ),
                        flags=flags,
                        sequence=sequence,
                        ts_event=ts_event,
                        ts_init=ts_init,
                    ),
                )

        deltas[-1] = self._with_last_flag(deltas[-1])
        return OrderBookDeltas(instrument_id=instrument_id, deltas=deltas)

    @staticmethod
    def _with_last_flag(delta: OrderBookDelta) -> OrderBookDelta:
        return OrderBookDelta(
            instrument_id=delta.instrument_id,
            action=delta.action,
            order=delta.order,
            flags=delta.flags | RecordFlag.F_LAST,
            sequence=delta.sequence,
            ts_event=delta.ts_event,
            ts_init=delta.ts_init,
        )

    def _handle_book_update(self, product: GateioProductType, result: Any) -> None:
        if not isinstance(result, dict):
            return
        raw_symbol = result.get("s") or result.get("contract")
        if not raw_symbol:
            return
        instrument_id = gateio_to_instrument_id(product, str(raw_symbol))
        book = self._books.get(instrument_id)
        if book is None:
            return

        try:
            changes = book.apply_update(result)
        except OrderBookSequenceError as e:
            self._gaps[product.value] += 1
            self._resyncs[product.value] += 1
            self._log.warning(f"{e}; rebuilding from a REST snapshot")
            book.reset()
            self._track(
                self._book_snapshot_then_deltas(instrument_id),
                log_msg=f"book resync {instrument_id}",
            )
            return

        if book.last_apply_was_snapshot:
            # The venue pushed a full-depth message; republish the whole book.
            deltas = self._snapshot_deltas(instrument_id, book)
            if deltas is not None:
                self._handle_data(deltas)
                self._published["order_book_snapshots"] += 1
            return

        if not changes:
            return

        instrument = self._instrument(instrument_id)
        if instrument is None:
            return

        ts_event = book.last_update_ms * 1_000_000
        ts_init = self._clock.timestamp_ns()
        if ts_event == 0:
            ts_event = ts_init
        sequence = book.last_update_id

        deltas_list: list[OrderBookDelta] = []
        for side_name, price, size in changes:
            side = OrderSide.BUY if side_name == BID else OrderSide.SELL
            # The action must follow the size that is actually published, not
            # the raw venue Decimal. A fractional size below the instrument's
            # representable increment truncates to zero, and an UPDATE carrying
            # a zero size is rejected by NautilusTrader, which would abort the
            # whole batch and leave the local book diverged from the venue for
            # the life of the subscription.
            quantity = venue_quantity(size, instrument.size_precision)
            if quantity == 0 and size != 0:
                self._published["book_levels_not_representable"] += 1
            action = BookAction.DELETE if quantity == 0 else BookAction.UPDATE
            deltas_list.append(
                OrderBookDelta(
                    instrument_id=instrument_id,
                    action=action,
                    order=BookOrder(
                        side=side,
                        price=instrument.make_price(price),
                        size=quantity,
                        order_id=0,
                    ),
                    flags=0,
                    sequence=sequence,
                    ts_event=ts_event,
                    ts_init=ts_init,
                ),
            )
        deltas_list[-1] = self._with_last_flag(deltas_list[-1])
        self._handle_data(OrderBookDeltas(instrument_id=instrument_id, deltas=deltas_list))
        self._published["order_book_deltas"] += 1

    # -- trades ------------------------------------------------------------

    def _handle_trades(self, product: GateioProductType, result: Any) -> None:
        items = result if isinstance(result, list) else [result]
        for item in items:
            if not isinstance(item, dict):
                continue
            trade = self._parse_trade(product, item)
            if trade is not None:
                self._handle_data(trade)
                self._published["trade_ticks"] += 1

    def _parse_trade(self, product: GateioProductType, item: dict[str, Any]) -> TradeTick | None:
        raw_symbol = item.get("currency_pair") or item.get("contract") or item.get("s")
        if not raw_symbol:
            return None
        instrument_id = gateio_to_instrument_id(product, str(raw_symbol))
        instrument = self._instrument(instrument_id)
        if instrument is None:
            return None

        # TradeId is always the venue's own trade id and is never synthesised.
        # A row without one cannot be identified, deduplicated or reconciled, so
        # it is rejected rather than published as the literal id "None".
        raw_id = item.get("id")
        if raw_id is None or str(raw_id) == "":
            self._published["trade_ticks_skipped"] += 1
            self._log.warning(
                f"Discarding a {product.value} trade for {instrument_id} with no 'id'",
            )
            return None

        if product.is_spot:
            raw_size = to_decimal(item.get("amount"))
            aggressor = (
                AggressorSide.BUYER
                if str(item.get("side", "")).lower() == "buy"
                else AggressorSide.SELLER
            )
        else:
            # Futures, delivery and options report a signed contract count whose
            # sign is the aggressor's side.
            signed = to_decimal(item.get("size"))
            raw_size = abs(signed)
            aggressor = AggressorSide.BUYER if signed > 0 else AggressorSide.SELLER
        size = venue_quantity(raw_size, instrument.size_precision)
        if size == 0:
            self._published["trade_ticks_skipped"] += 1
            return None

        ts_event = timestamp_to_nanos(item.get("create_time_ms") or item.get("create_time"))
        ts_init = self._clock.timestamp_ns()
        return TradeTick(
            instrument_id=instrument_id,
            price=instrument.make_price(item.get("price")),
            size=size,
            aggressor_side=aggressor,
            trade_id=TradeId(str(raw_id)),
            ts_event=ts_event or ts_init,
            ts_init=ts_init,
        )

    # -- quotes ------------------------------------------------------------

    def _handle_book_ticker(self, product: GateioProductType, result: Any) -> None:
        if not isinstance(result, dict):
            return
        raw_symbol = result.get("s")
        if not raw_symbol:
            return
        bid = result.get("b")
        ask = result.get("a")
        raw_bid_size = result.get("B")
        raw_ask_size = result.get("A")
        if bid in (None, "") or ask in (None, ""):
            # One side is empty; a quote cannot be published without both.
            self._published["quote_ticks_skipped"] += 1
            return
        if raw_bid_size in (None, "") or raw_ask_size in (None, ""):
            # A quote with an unknown size would publish a fabricated zero.
            self._published["quote_ticks_skipped"] += 1
            return
        instrument_id = gateio_to_instrument_id(product, str(raw_symbol))
        instrument = self._instrument(instrument_id)
        if instrument is None:
            return

        bid_size = venue_quantity(raw_bid_size, instrument.size_precision)
        ask_size = venue_quantity(raw_ask_size, instrument.size_precision)
        if bid_size == 0 or ask_size == 0:
            # Either the venue reported an empty side, or the size is below the
            # instrument's representable increment (see `venue_quantity`).
            # Publishing it would assert a zero-sized top of book.
            self._published["quote_ticks_skipped"] += 1
            return

        ts_init = self._clock.timestamp_ns()
        ts_event = timestamp_to_nanos(result.get("t")) or ts_init
        quote = QuoteTick(
            instrument_id=instrument_id,
            bid_price=instrument.make_price(bid),
            ask_price=instrument.make_price(ask),
            bid_size=bid_size,
            ask_size=ask_size,
            ts_event=ts_event,
            ts_init=ts_init,
        )
        self._handle_data(quote)
        self._published["quote_ticks"] += 1

    # -- bars --------------------------------------------------------------

    def _handle_candlesticks(self, product: GateioProductType, result: Any) -> None:
        items = result if isinstance(result, list) else [result]
        for item in items:
            if isinstance(item, dict):
                self._handle_candlestick(product, item)

    def _handle_candlestick(self, product: GateioProductType, item: dict[str, Any]) -> None:
        name = item.get("n")
        if not name:
            return
        bar_type = self._bar_types.get((product, str(name)))
        if bar_type is None:
            return
        instrument = self._instrument(bar_type.instrument_id)
        if instrument is None:
            return

        open_secs = to_int(item.get("t"))
        bar = self._build_bar(bar_type, instrument, item, product, open_secs)
        if bar is None:
            return

        # Delivery and option candlesticks carry no window-close flag, and the
        # flag "may be missing" on the others, so a bucket is also considered
        # closed once a newer bucket starts.
        pending = self._bar_pending.get(bar_type)
        if pending is not None and pending.open_secs < open_secs:
            self._publish_bar(bar_type, pending.open_secs, pending.bar)

        if item.get("w") is True:
            self._publish_bar(bar_type, open_secs, bar)
            return

        step_ms = self._bar_step_ms(bar_type)
        flush_at_ns = 0
        if step_ms > 0:
            close_ns = (open_secs * _NANOS_PER_SEC) + (step_ms * 1_000_000)
            flush_at_ns = close_ns + int(BAR_CLOSE_GRACE_SECS * _NANOS_PER_SEC)
        self._bar_pending[bar_type] = _PendingBar(open_secs, bar, flush_at_ns)
        self._flush_pending_bars()

    async def _flush_bars_loop(self) -> None:
        """Release pending bars whose bucket has demonstrably closed.

        Without this, a bar on a product that sends no window-close flag is only
        released when the *next* bucket produces a candle. On an illiquid
        delivery or option contract that may be many intervals later, or never.
        """
        while True:
            try:
                await asyncio.sleep(BAR_FLUSH_INTERVAL_SECS)
                self._flush_pending_bars()
            except asyncio.CancelledError:
                self._log.debug("Canceled task 'flush_bars'")
                return
            except Exception as e:  # noqa: BLE001 - the task must survive bad data
                self._log.error(f"Error flushing pending bars: {e}")

    def _flush_pending_bars(self) -> None:
        """Publish every pending bar whose grace period after close has elapsed."""
        now_ns = self._clock.timestamp_ns()
        for bar_type, pending in list(self._bar_pending.items()):
            if not pending.flush_at_ns or now_ns < pending.flush_at_ns:
                continue
            # Re-stamp the bar so ts_init reports when it was actually
            # published rather than when the last candle update arrived.
            self._publish_bar(bar_type, pending.open_secs, _restamp_bar(pending.bar, now_ns))

    def _bar_step_ms(self, bar_type: BarType) -> int:
        """Return the bar interval in milliseconds, or ``0`` if Gate.io has none."""
        interval = NAUTILUS_TO_GATEIO_INTERVAL.get(
            f"{bar_type.spec.step}-{bar_aggregation_to_str(bar_type.spec.aggregation)}",
        )
        return GATEIO_INTERVAL_MS.get(interval or "", 0)

    def _publish_bar(self, bar_type: BarType, open_secs: int, bar: Bar) -> None:
        if self._bar_published.get(bar_type, -1) >= open_secs:
            return  # Gate.io repeats the closing update; publish each bar once
        self._bar_published[bar_type] = open_secs
        self._bar_pending.pop(bar_type, None)
        self._handle_data(bar)
        self._published["bars"] += 1

    def _build_bar(
        self,
        bar_type: BarType,
        instrument: Instrument,
        item: dict[str, Any],
        product: GateioProductType,
        open_secs: int,
    ) -> Bar | None:
        try:
            open_price = instrument.make_price(item["o"])
            high_price = instrument.make_price(item["h"])
            low_price = instrument.make_price(item["l"])
            close_price = instrument.make_price(item["c"])
        except (KeyError, TypeError, ValueError):
            return None

        # Spot reports the traded base amount in ``a`` and quote turnover in
        # ``v``; contract products report the contract count in ``v``.
        raw_volume = item.get("a") if product.is_spot else item.get("v")
        if raw_volume is None:
            raw_volume = item.get("v")
        volume = Quantity(max(to_float(raw_volume), 0.0), instrument.size_precision)

        return Bar(
            bar_type=bar_type,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume=volume,
            ts_event=self._bar_ts_event(bar_type, open_secs),
            ts_init=self._clock.timestamp_ns(),
        )

    def _bar_ts_event(self, bar_type: BarType, open_secs: int) -> int:
        ts_ns = open_secs * _NANOS_PER_SEC
        if not self._config.bars_timestamp_on_close:
            return ts_ns
        return ts_ns + self._bar_step_ms(bar_type) * 1_000_000

    # -- futures tickers ---------------------------------------------------

    def _handle_tickers(self, product: GateioProductType, result: Any) -> None:
        items = result if isinstance(result, list) else [result]
        ts_init = self._clock.timestamp_ns()
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_symbol = item.get("contract")
            if not raw_symbol:
                continue
            instrument_id = gateio_to_instrument_id(product, str(raw_symbol))
            kinds = self._ticker_subs.get(instrument_id)
            if not kinds:
                continue
            instrument = self._instrument(instrument_id)
            if instrument is None:
                continue
            ts_event = timestamp_to_nanos(item.get("t")) or ts_init

            if _MARK in kinds and item.get("mark_price") not in (None, ""):
                self._handle_data(
                    MarkPriceUpdate(
                        instrument_id=instrument_id,
                        value=instrument.make_price(item["mark_price"]),
                        ts_event=ts_event,
                        ts_init=ts_init,
                    ),
                )
                self._published["mark_prices"] += 1

            if _INDEX in kinds and item.get("index_price") not in (None, ""):
                self._handle_data(
                    IndexPriceUpdate(
                        instrument_id=instrument_id,
                        value=instrument.make_price(item["index_price"]),
                        ts_event=ts_event,
                        ts_init=ts_init,
                    ),
                )
                self._published["index_prices"] += 1

            if _FUNDING in kinds and item.get("funding_rate") not in (None, ""):
                self._handle_data(
                    FundingRateUpdate(
                        instrument_id=instrument_id,
                        rate=Decimal(str(item["funding_rate"])),
                        ts_event=ts_event,
                        ts_init=ts_init,
                        interval=self._funding_interval_mins(instrument),
                        next_funding_ns=self._next_funding_ns(instrument),
                    ),
                )
                self._published["funding_rates"] += 1

    @staticmethod
    def _funding_interval_mins(instrument: Instrument) -> int | None:
        """Return the funding interval in minutes from the contract definition."""
        seconds = to_int(instrument.info.get("funding_interval")) if instrument.info else 0
        return seconds // 60 if seconds > 0 else None

    @staticmethod
    def _next_funding_ns(instrument: Instrument) -> int | None:
        """Return the next funding time in nanoseconds, if the venue published one."""
        secs = to_int(instrument.info.get("funding_next_apply")) if instrument.info else 0
        return secs * 1_000_000_000 if secs > 0 else None

    # -- requests ----------------------------------------------------------

    async def _request_instrument(self, request: RequestInstrument) -> None:
        instrument = self._instrument(request.instrument_id)
        if instrument is None:
            await self._instrument_provider.load_async(request.instrument_id)
            instrument = self._instrument(request.instrument_id)
        if instrument is None:
            self._log.error(f"Cannot find instrument for {request.instrument_id}")
            return
        self._handle_instrument(
            instrument,
            request.id,
            request.start,
            request.end,
            request.params,
        )

    async def _request_instruments(self, request: RequestInstruments) -> None:
        instruments = list(self._instrument_provider.get_all().values())
        self._handle_instruments(
            request.venue or GATEIO_VENUE,
            instruments,
            request.id,
            request.start,
            request.end,
            request.params,
        )

    async def _request_order_book_snapshot(self, request: RequestOrderBookSnapshot) -> None:
        resolved = self._resolve(request.instrument_id)
        if resolved is None:
            return
        product, raw_symbol = resolved
        instrument = self._instrument(request.instrument_id)
        if instrument is None:
            self._log.error(f"Cannot find instrument for {request.instrument_id}")
            return

        # The accepted depths differ per product (options stop at 50), so the
        # clamp must consult the table for this product rather than a global one.
        limit = nearest_snapshot_limit(product, request.limit or self._snapshot_limit)
        try:
            payload = await self._fetch_book_snapshot(product, raw_symbol, limit)
            book = GateioOrderBook(raw_symbol)
            book.apply_snapshot(payload)
        except (GateioError, ValueError) as e:
            self._log.error(
                f"Cannot request an order book snapshot for {request.instrument_id}: {e}",
            )
            return
        deltas = self._snapshot_deltas(request.instrument_id, book)
        if deltas is None:
            return
        self._handle_order_book_deltas(
            request.instrument_id,
            [deltas],
            request.id,
            request.start,
            request.end,
            request.params,
        )

    async def _request_trade_ticks(self, request: RequestTradeTicks) -> None:
        resolved = self._resolve(request.instrument_id)
        if resolved is None:
            return
        product, raw_symbol = resolved
        instrument = self._instrument(request.instrument_id)
        if instrument is None:
            self._log.error(f"Cannot find instrument for {request.instrument_id}")
            return

        limit = min(request.limit or MAX_REST_LIMIT, MAX_REST_LIMIT)
        if product.is_spot:
            rows = await self._spot_http.trades(raw_symbol, limit=limit)
        elif product.is_option:
            rows = await self._options_http.trades(raw_symbol, limit=limit)
        else:
            rows = await self._futures_http[product].trades(raw_symbol, limit=limit)

        start_ns = dt_to_unix_nanos(request.start) if request.start is not None else 0
        end_ns = dt_to_unix_nanos(request.end) if request.end is not None else 0
        ticks: list[TradeTick] = []
        for row in rows or []:
            tick = self._parse_trade(product, row)
            if tick is None:
                continue
            if start_ns and tick.ts_event < start_ns:
                continue
            if end_ns and tick.ts_event > end_ns:
                continue
            ticks.append(tick)
        ticks.sort(key=lambda t: t.ts_event)

        self._handle_trade_ticks(
            request.instrument_id,
            ticks,
            request.id,
            request.start,
            request.end,
            request.params,
        )

    async def _request_bars(self, request: RequestBars) -> None:
        bar_type = request.bar_type
        resolved = self._resolve(bar_type.instrument_id)
        if resolved is None:
            return
        product, raw_symbol = resolved
        instrument = self._instrument(bar_type.instrument_id)
        if instrument is None:
            self._log.error(f"Cannot find instrument for {bar_type.instrument_id}")
            return
        try:
            interval = bar_type_to_interval(bar_type)
        except ValueError as e:
            self._log.error(f"Cannot request {bar_type}: {e}")
            return

        step_secs = GATEIO_INTERVAL_MS[interval] // 1000
        now_secs = self._clock.timestamp_ns() // 1_000_000_000
        end_secs = int(request.end.timestamp()) if request.end is not None else now_secs
        start_secs = int(request.start.timestamp()) if request.start is not None else 0
        limit = request.limit or 0

        rows = await self._fetch_candles(
            product,
            raw_symbol,
            interval,
            step_secs,
            start_secs,
            end_secs,
            limit,
        )

        bars: list[Bar] = []
        for open_secs, row in rows:
            if open_secs + step_secs > now_secs:
                continue  # the current bucket is still open
            bar = self._build_bar(bar_type, instrument, row, product, open_secs)
            if bar is not None:
                bars.append(bar)
        bars.sort(key=lambda b: b.ts_event)
        if limit and len(bars) > limit:
            bars = bars[-limit:]

        self._handle_bars(
            bar_type,
            bars,
            request.id,
            request.start,
            request.end,
            request.params,
        )

    async def _fetch_candles(
        self,
        product: GateioProductType,
        raw_symbol: str,
        interval: str,
        step_secs: int,
        start_secs: int,
        end_secs: int,
        limit: int,
    ) -> list[tuple[int, dict[str, Any]]]:
        """Page through the candlestick endpoint and normalise the rows.

        Gate.io returns at most 1000 candles per call, so a request spanning a
        longer window is split into consecutive pages. Rows are returned as
        ``(open_time_seconds, row)`` with the spot endpoint's positional arrays
        converted into the same object form the other products use.
        """
        collected: dict[int, dict[str, Any]] = {}

        if start_secs <= 0:
            page_limit = min(limit or MAX_REST_LIMIT, MAX_REST_LIMIT)
            rows = await self._candles_page(
                product,
                raw_symbol,
                interval,
                limit=page_limit,
                frm=None,
                to=end_secs or None,
            )
            for open_secs, row in rows:
                collected[open_secs] = row
        else:
            cursor = start_secs
            while cursor <= end_secs:
                window_end = min(cursor + (MAX_REST_LIMIT - 1) * step_secs, end_secs)
                rows = await self._candles_page(
                    product,
                    raw_symbol,
                    interval,
                    limit=None,
                    frm=cursor,
                    to=window_end,
                )
                if not rows:
                    break
                for open_secs, row in rows:
                    collected[open_secs] = row
                newest = max(open_secs for open_secs, _ in rows)
                if newest + step_secs <= cursor:
                    break  # no forward progress; stop rather than loop
                cursor = newest + step_secs

        return sorted(collected.items())

    async def _candles_page(
        self,
        product: GateioProductType,
        raw_symbol: str,
        interval: str,
        limit: int | None,
        frm: int | None,
        to: int | None,
    ) -> list[tuple[int, dict[str, Any]]]:
        if product.is_spot:
            raw = await self._spot_http.candlesticks(
                raw_symbol,
                interval=interval,
                limit=limit,
                frm=frm,
                to=to,
            )
            return [self._spot_candle(row) for row in raw or [] if len(row) >= 7]
        if product.is_option:
            raw = await self._options_http.candlesticks(
                raw_symbol,
                interval=interval,
                limit=limit,
                frm=frm,
                to=to,
            )
        else:
            raw = await self._futures_http[product].candlesticks(
                raw_symbol,
                interval=interval,
                limit=limit,
                frm=frm,
                to=to,
            )
        return [(to_int(row.get("t")), row) for row in raw or [] if isinstance(row, dict)]

    @staticmethod
    def _spot_candle(row: list[str]) -> tuple[int, dict[str, Any]]:
        """Convert a positional spot candle into the object form used elsewhere.

        The spot endpoint returns
        ``[time, quote_volume, close, high, low, open, base_volume, closed]``.
        """
        return (
            to_int(row[0]),
            {
                "t": row[0],
                "v": row[1],
                "c": row[2],
                "h": row[3],
                "l": row[4],
                "o": row[5],
                "a": row[6],
                "w": (str(row[7]).lower() == "true") if len(row) > 7 else None,
            },
        )


__all__ = [
    "BAR_CLOSE_GRACE_SECS",
    "BOOK_INTERVALS_MS",
    "BOOK_LEVELS",
    "GateioDataClient",
    "bar_type_to_interval",
    "timestamp_to_nanos",
    "venue_quantity",
]
