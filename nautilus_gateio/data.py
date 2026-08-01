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
``_subscribe_order_book_depth``         ``*.order_book`` (periodic snapshot)
``_subscribe_bars``                     ``*.candlesticks`` (closed bars only)
``_subscribe_mark_prices``              ``<ticker channel>.mark_price``
``_subscribe_index_prices``             ``<ticker channel>.index_price``
``_subscribe_funding_rates``            ``futures.tickers.funding_rate``
``_subscribe`` (``GateioTicker``)       ``<ticker channel>`` (the whole row)
``_subscribe_instrument_status``        polled instrument listings (no channel)
``_subscribe_instrument_close``         polled settlement (dated products only)
``_request_funding_rates``              ``GET /futures/{settle}/funding_rate``
======================================  =======================================

``_request_quote_ticks`` is implemented only to refuse: Gate.io publishes a
live best bid/offer stream and no quote history on any product.

Books
-----
The two book subscriptions read different venue channels and neither derives
from the other. Deltas come from the incremental ``*.order_book_update`` stream
aligned against a REST snapshot; ``OrderBookDepth10`` comes from the periodic
``*.order_book`` snapshot channel, which is self-synchronising and needs no
sequence algorithm, no REST seed and no rebuild after a reconnect. Holding both
for one instrument is allowed by the venue but gives NautilusTrader's single
cached ``OrderBook`` two writers, so the client warns when it sees that.

Reference prices
----------------
The ticker channel above is ``futures.tickers`` on the three futures products
and ``options.contract_tickers`` on options (:func:`.public.tickers_channel`);
Gate.io has no dedicated mark, index or funding channel, so one venue
subscription serves whichever of the three a client asked for.

Mark and index prices are not order prices, so they are published on the scale
Gate.io published them with rather than rounded onto the instrument's order
tick; see :func:`venue_price`. Funding is perpetual-only, and its next
application time is derived from the venue's funding grid rather than served
from the cached contract definition, which the instrument reload task refreshes
far more slowly than funding settles; see
:meth:`GateioDataClient._next_funding_ns`.

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
    RequestData,
    RequestFundingRates,
    RequestInstrument,
    RequestInstruments,
    RequestOrderBookSnapshot,
    RequestQuoteTicks,
    RequestTradeTicks,
    SubscribeBars,
    SubscribeData,
    SubscribeFundingRates,
    SubscribeIndexPrices,
    SubscribeInstrument,
    SubscribeInstrumentClose,
    SubscribeInstruments,
    SubscribeInstrumentStatus,
    SubscribeMarkPrices,
    SubscribeOrderBook,
    SubscribeQuoteTicks,
    SubscribeTradeTicks,
    UnsubscribeBars,
    UnsubscribeData,
    UnsubscribeFundingRates,
    UnsubscribeIndexPrices,
    UnsubscribeInstrument,
    UnsubscribeInstrumentClose,
    UnsubscribeInstruments,
    UnsubscribeInstrumentStatus,
    UnsubscribeMarkPrices,
    UnsubscribeOrderBook,
    UnsubscribeQuoteTicks,
    UnsubscribeTradeTicks,
)
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.data import (
    NULL_ORDER,
    Bar,
    BarType,
    BookOrder,
    CustomData,
    DataType,
    FundingRateUpdate,
    IndexPriceUpdate,
    InstrumentClose,
    InstrumentStatus,
    MarkPriceUpdate,
    OrderBookDelta,
    OrderBookDeltas,
    OrderBookDepth10,
    QuoteTick,
    TradeTick,
)
from nautilus_trader.model.enums import (
    AggregationSource,
    AggressorSide,
    BookAction,
    BookType,
    InstrumentCloseType,
    MarketStatusAction,
    OptionKind,
    OrderSide,
    PriceType,
    RecordFlag,
    bar_aggregation_to_str,
    book_type_to_str,
    market_status_action_to_str,
)
from nautilus_trader.model.identifiers import ClientId, InstrumentId, TradeId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import FIXED_PRECISION, Price, Quantity

from nautilus_gateio.books import (
    BID,
    GateioOrderBook,
    OrderBookSequenceError,
    SnapshotStaleError,
    parse_levels,
)
from nautilus_gateio.common.constants import (
    GATEIO_INTERVAL_MS,
    GATEIO_VENUE,
    NAUTILUS_TO_GATEIO_INTERVAL,
)
from nautilus_gateio.common.enums import GateioProductType
from nautilus_gateio.common.errors import GateioError
from nautilus_gateio.common.parsing import (
    precision_from_increment,
    timestamp_to_nanos,
    to_decimal,
    to_float,
    to_int,
)
from nautilus_gateio.common.status import diff_and_emit_statuses, market_status_action
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
from nautilus_gateio.types import GateioTicker
from nautilus_gateio.websocket.client import is_transient_ws_error
from nautilus_gateio.websocket.public import (
    BOOK_INTERVALS_MS,
    BOOK_LEVELS,
    BOOK_SNAPSHOT_PUSH_INTERVALS,
    GateioPublicWebSocket,
    nearest_snapshot_limit,
    tickers_channel,
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

#: Levels per side an ``OrderBookDepth10`` carries. The type is fixed at ten
#: (``core/includes/model.h`` ``DEPTH10_LEN``), and Gate.io's snapshot channel
#: offers exactly ten on all five products, so nothing is truncated.
DEPTH10_LEVELS: Final[int] = 10

#: How often a settled contract is re-read while waiting for the venue to
#: publish its settlement price, and how long that wait may last. Delivery
#: settlement is published within minutes of expiry, but an option's settlement
#: row appears on Gate.io's own schedule; the wait is bounded so that a
#: subscription for a contract the venue never settles does not poll forever.
INSTRUMENT_CLOSE_POLL_SECS: Final[float] = 30.0
INSTRUMENT_CLOSE_TIMEOUT_SECS: Final[float] = 3600.0

#: Half-width of the window searched for an option settlement row, in seconds.
#: ``GET /options/settlements`` is queried by time range, and the row's own
#: ``time`` is the venue's settlement instant, which need not equal the
#: contract's stated ``expiration_time`` to the second.
OPTION_SETTLEMENT_WINDOW_SECS: Final[int] = 86_400

#: Subscription kinds sharing the ``futures.tickers`` channel.
_MARK: Final[str] = "mark"
_INDEX: Final[str] = "index"
_FUNDING: Final[str] = "funding"
#: The whole ticker row, published as :class:`GateioTicker`. It shares the same
#: venue channel and the same reference count as the three above, so a strategy
#: holding both a mark-price and a ticker subscription costs one subscription.
_TICKER: Final[str] = "ticker"


_NANOS_PER_SEC: Final[int] = 1_000_000_000


class _SettlementConflict(Exception):
    """Two venue sources disagreed about a settlement price.

    Raised to stop a close watcher rather than to be reported to a caller: a
    disagreement between two independent public fields is not transient, so
    re-polling would produce the same contradiction every time, and
    ``InstrumentClose.close_price`` has no null form in which to say "closed,
    price unknown".
    """


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


def venue_price(value: Any, floor_precision: int = 0) -> Price | None:
    """Convert a Gate.io reference price to a ``Price`` at the venue's own scale.

    Mark and index prices are not order prices. They do not sit on the
    instrument's order tick, and Gate.io says so explicitly by publishing a
    separate ``mark_price_round`` ("minimum unit of mark price") alongside
    ``order_price_round``: the BTC_USDT perpetual quotes orders in 0.1 and marks
    in 0.01, and the BTC_USDT options quote orders in 1 and marks in 0.1. Passing
    such a value through ``Instrument.make_price`` rounds a number the venue
    published onto a grid it does not live on — an option marked 5797.7 would be
    published as 5798.

    So the precision comes from the venue's own value, raised to
    ``floor_precision`` where the contract states a finer grid, so the scale
    stays constant between updates instead of shrinking on a value that happens
    to end in a zero. This is also what the reference Python adapter does:
    ``adapters/binance/futures/schemas/market.py`` builds mark, index and
    estimated-settlement prices with ``Price.from_str`` on the venue string
    rather than through the instrument.

    Returns ``None`` when the field is absent, unparseable or outside what a
    ``Price`` can hold; callers must treat that as "not published" rather than
    publish a zero, which would be a reference price no participant ever saw.
    """
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    if not number.is_finite():
        return None
    # A positive exponent (``1E+3``) means no fractional digits at all.
    precision = max(-int(number.as_tuple().exponent), floor_precision, 0)
    # Only reachable on a standard-precision wheel, whose FIXED_PRECISION is 9
    # against the 16 of the high-precision build. Rounding is then the only
    # option left and it is done once here rather than at each call site.
    precision = min(precision, FIXED_PRECISION)
    try:
        return Price(number, precision)
    except (ValueError, OverflowError):  # pragma: no cover - implausible magnitude
        return None


def _mark_price_precision(instrument: Instrument) -> int:
    """Return the decimal scale Gate.io publishes ``instrument``'s mark price on.

    ``mark_price_round`` is the contract's documented "minimum unit of mark
    price" and is independent of ``order_price_round``. Contracts that do not
    publish it yield ``0``, which leaves :func:`venue_price` on the scale of the
    value itself.
    """
    info = instrument.info or {}
    return precision_from_increment(info.get("mark_price_round"))


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
        # Depth keeps its own registries rather than sharing the delta path's.
        # Sharing `self._books` would make `_unsubscribe_order_book_deltas` tear
        # down a live depth stream and would make `_handle_ws_reconnect` schedule
        # a REST resnapshot for a channel that resynchronises itself.
        self._depth_streams: dict[InstrumentId, tuple[int, str]] = {}
        self._depth_sequences: dict[InstrumentId, int] = {}
        self._instrument_status_subs: set[InstrumentId] = set()
        self._status_cache: dict[InstrumentId, MarketStatusAction] = {}
        self._instrument_close_tasks: dict[InstrumentId, asyncio.Task] = {}
        self._instrument_close_emitted: set[InstrumentId] = set()
        self._bar_types: dict[tuple[GateioProductType, str], BarType] = {}
        self._bar_pending: dict[BarType, _PendingBar] = {}
        self._bar_published: dict[BarType, int] = {}
        self._dropped_candles: dict[BarType, int] = defaultdict(int)
        self._ticker_subs: dict[InstrumentId, set[str]] = defaultdict(set)
        self._update_instruments_task: asyncio.Task | None = None
        self._bar_flush_task: asyncio.Task | None = None
        # No task registry of our own: `LiveMarketDataClient.__init__` already
        # created `self._tasks` as a WeakSet that `create_task` populates and
        # `cancel_pending_tasks` drains. Replacing it with a plain set was what
        # made every completed subscribe and request task live for the lifetime
        # of the node.

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
            "candles_dropped": {
                str(bar_type): count for bar_type, count in self._dropped_candles.items() if count
            },
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
        # First, and the only load-bearing statement here: from this line no
        # request reaches the wire, whoever births it and whenever it wakes.
        # `cancel_pending_tasks` below takes a *snapshot* of the registry
        # (live/cancellation.py), so it can neither see a task born from a
        # WebSocket frame that arrives afterwards nor stop one that is merely
        # asleep in it; the gate covers both, and covers the instrument reload,
        # which reaches the venue through the shared provider rather than
        # through this client's namespaces.
        self._http_client.stop_accepting()
        try:
            # The platform's bounded teardown: it snapshots strong references,
            # cancels, and gathers with a timeout. The base `disconnect()` calls
            # it again once this coroutine returns, which is harmless — by then
            # the WeakSet holds nothing pending.
            await self.cancel_pending_tasks()
            self._update_instruments_task = None
            self._bar_flush_task = None
            # The close watchers were cancelled with everything else above;
            # dropping the handles keeps `_subscribe_instrument_close` from
            # reporting an already-subscribed instrument after a reconnect cycle.
            self._instrument_close_tasks.clear()

            # `popitem`, not `.items()`: a `_connect` still in flight inserts
            # into this dict, and iterating it live raises `RuntimeError:
            # dictionary changed size during iteration` past the inner `except`
            # below, killing `_disconnect_with_cleanup` (live/data_client.py)
            # before `cancel_pending_tasks()` and `_set_connected(False)`.
            while self._ws_clients:
                product, client = self._ws_clients.popitem()
                try:
                    await client.disconnect()
                except Exception as e:  # noqa: BLE001 - shutdown must not raise
                    self._log.warning(f"Error disconnecting {product.value} WebSocket: {e}")
        except Exception as e:  # noqa: BLE001 - the platform's wrapper has no try
            self._log.exception("Error during Gate.io data client teardown", e)
        finally:
            # Released last, and always. The transport is shared with the
            # execution client and reference counted, so this call is what
            # closes the pool when this client is the last holder. It waits
            # briefly for whatever is already on the wire: closing the pool
            # underneath an unanswered request makes `httpx` raise a bare
            # `RuntimeError` that no handler here classifies (measured on httpx
            # 0.28.1 — the older comment claiming such a request would see
            # `CLIENT_CLOSED` was simply wrong).
            await self._http_client.close()

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
                # Status rides the instrument reload rather than a timer of its
                # own, as Kraken's in-tree client does: the evidence is the same
                # listing payloads, so a second cadence would double the traffic
                # on a rate-limited public API and let the two views disagree.
                await self._poll_instrument_statuses()
                self._log.debug("Reloaded instruments")
            except asyncio.CancelledError:
                self._log.debug("Canceled task 'update_instruments'")
                return
            except GateioError as e:
                if e.label == "CLIENT_CLOSED":
                    # The gate is shut, so the node is stopping. This loop
                    # reaches the venue through the *shared* instrument
                    # provider, which is why the refusal arrives as an error
                    # rather than as a cancellation; leave quietly instead of
                    # turning shutdown into a wall of ERROR lines.
                    self._log.debug("Stopping 'update_instruments': the transport is closing")
                    return
                self._log.error(f"Error updating instruments: {e}")
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

    def _resolve_depth_stream(
        self,
        product: GateioProductType,
        depth: int,
        params: dict[str, Any] | None,
    ) -> tuple[int, str]:
        """Return the ``(limit, push interval)`` pair for the depth snapshot channel.

        ``config.order_book_snapshot_limit`` is deliberately not consulted. That
        setting exists to keep the REST seed aligned with the incremental
        stream's level; applying it here would subscribe to a hundred levels and
        discard ninety, and would make one configuration field mean two things.

        The push interval comes off ``command.params`` rather than a new
        configuration field, which is the in-tree precedent (Deribit reads its
        venue-specific interval the same way). It matters on spot, where the
        channel pushes the full book every 100 ms whether or not anything
        changed; every other product accepts only ``"0"`` (push on change).
        """
        requested = depth or DEPTH10_LEVELS
        if requested > DEPTH10_LEVELS:
            self._log.warning(
                f"OrderBookDepth10 carries {DEPTH10_LEVELS} levels per side; subscribing "
                f"{DEPTH10_LEVELS} rather than the requested {requested} for {product.value}",
            )
            requested = DEPTH10_LEVELS
        # Rounds *up* to a depth the product serves, so a request for three
        # levels on spot becomes five rather than being rejected by the venue.
        limit = nearest_snapshot_limit(product, requested)

        intervals = BOOK_SNAPSHOT_PUSH_INTERVALS[product]
        requested_interval = (params or {}).get("interval")
        if requested_interval is None:
            return limit, intervals[0]
        interval = str(requested_interval)
        if interval not in intervals:
            self._log.warning(
                f"{product.value} does not accept a {interval!r} snapshot push interval; "
                f"using {intervals[0]!r} (accepted: {', '.join(intervals)})",
            )
            interval = intervals[0]
        return limit, interval

    # -- subscriptions -----------------------------------------------------

    async def _subscribe(self, command: SubscribeData) -> None:
        """Subscribe a venue-native data type the platform has no first-class type for.

        Today that is :class:`GateioTicker` alone. Everything else is refused
        with a log line and a return rather than an exception:
        ``LiveMarketDataClient.subscribe`` records the data type *before* it
        creates the task running this coroutine, and ``DataEngine`` then skips
        any data type already recorded, so raising here would leave a
        subscription the client reports as held forever and never retries. The
        engine's own ``except NotImplementedError`` around ``client.subscribe``
        cannot help: on a live client the call returns before the task runs.

        Rows are published under
        ``DataType(GateioTicker, metadata={"instrument_id": instrument_id})``,
        which is the data type to subscribe with: the platform addresses a custom
        data type carrying metadata by that metadata
        (``common/data_topics.pyx:189-210``), so a subscription taken out with a
        bare ``DataType(GateioTicker)`` and a separate ``instrument_id`` listens
        on a different topic from the one the rows arrive on. This is the in-tree
        convention (``adapters/binance/data.py:1015-1020``).
        """
        data_type = command.data_type
        if data_type.type is not GateioTicker:
            self._log.error(f"Cannot subscribe to {data_type.type} (not implemented)")
            return
        instrument_id = self._custom_data_instrument_id(command)
        if instrument_id is None:
            self._log.error(
                f"Cannot subscribe to {data_type}: no instrument ID on the command or in the "
                f"`data_type` metadata",
            )
            return
        await self._hold_ticker_channel(instrument_id, _TICKER)

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

        self.create_task(
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

    async def _subscribe_order_book_depth(self, command: SubscribeOrderBook) -> None:
        """Subscribe the periodic ``*.order_book`` snapshot channel at ten levels.

        This channel is self-synchronising: every message is a complete book of
        the subscribed depth, so there is no sequence algorithm, no REST seed and
        nothing to rebuild after a reconnect. It is a different venue channel
        from the incremental stream the delta path uses, so holding both costs
        two subscriptions at the venue and neither disturbs the other.
        """
        if command.book_type != BookType.L2_MBP:
            self._log.error(
                f"Cannot subscribe to {book_type_to_str(command.book_type)} order book depth: "
                f"Gate.io publishes L2_MBP depth only",
            )
            return
        instrument_id = command.instrument_id
        if instrument_id not in self.subscribed_order_book_depth():
            # The base class records the subscription before creating this task,
            # so an absence here means it was withdrawn in between; subscribing
            # now would leave a venue stream nobody is listening to.
            return
        resolved = self._resolve(instrument_id)
        if resolved is None:
            return
        product, raw_symbol = resolved
        client = self._ws(product)
        if client is None:
            return
        if instrument_id in self._depth_streams:
            self._log.warning(f"Already subscribed to order book depth for {instrument_id}")
            return
        if instrument_id in self._books:
            # NautilusTrader keeps one `OrderBook` per instrument in the cache,
            # and a managed subscription of either kind attaches its own writer
            # to it. `apply_depth` *replaces* the book, so a depth message
            # discards every delta level below the tenth and the book flaps
            # between the two feeds. The adapter cannot fix this — the engine
            # owns the cached book — so it says so rather than pretending
            # otherwise or silently refusing one of the two.
            self._log.warning(
                f"Order book deltas and depth are both subscribed for {instrument_id}; "
                f"NautilusTrader maintains a single cached order book per instrument, so the "
                f"two feeds will overwrite each other",
            )

        limit, interval = self._resolve_depth_stream(product, command.depth, command.params)
        self._depth_streams[instrument_id] = (limit, interval)
        try:
            await client.subscribe_order_book_snapshot(raw_symbol, limit=limit, interval=interval)
        except (GateioError, ValueError) as e:
            if is_transient_ws_error(e):
                # The transport keeps the subscription and replays it, and this
                # channel needs no local state to survive that, so the registry
                # entry stays.
                self._log.warning(
                    f"Order book depth subscription for {instrument_id} did not complete ({e}); "
                    f"it will be replayed on the next connection",
                )
                return
            self._depth_streams.pop(instrument_id, None)
            self._log.error(f"Cannot subscribe to order book depth for {instrument_id}: {e}")

    async def _unsubscribe_order_book_depth(self, command: UnsubscribeOrderBook) -> None:
        instrument_id = command.instrument_id
        stream = self._depth_streams.pop(instrument_id, None)
        self._depth_sequences.pop(instrument_id, None)
        if stream is None:
            return
        resolved = self._resolve(instrument_id)
        if resolved is None:
            return
        product, raw_symbol = resolved
        client = self._ws(product)
        if client is None:
            return
        limit, interval = stream
        # Gate.io identifies a subscription by its full argument list, so the
        # unsubscribe must repeat the triple that was subscribed. Rebuilding it
        # from defaults would be acknowledged and leave the stream running.
        try:
            await client.unsubscribe_order_book_snapshot(
                raw_symbol,
                limit=limit,
                interval=interval,
            )
        except (GateioError, ValueError) as e:
            self._log.error(f"Cannot unsubscribe from order book depth for {instrument_id}: {e}")

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

    # -- ticker-derived derivative streams ---------------------------------

    async def _hold_ticker_channel(self, instrument_id: InstrumentId, kind: str) -> None:
        """Take a reference on the ticker channel for ``instrument_id``.

        Not a platform hook, and deliberately not named like one: every
        ``_subscribe_*`` / ``_unsubscribe_*`` method on a ``LiveMarketDataClient``
        is a hook taking a single command object, and a private helper with a
        different signature sitting in that namespace misleads a reader and any
        sweep that enumerates hooks.

        On futures the channel is ``futures.tickers``; on options it is
        ``options.contract_tickers``, which publishes ``mark_price`` and
        ``index_price`` per contract; on spot it is ``spot.tickers``, which
        carries 24-hour statistics and a best bid/offer and none of the three
        reference prices. Gate.io has no dedicated mark, index or funding
        channel, so one venue subscription serves every combination and the
        subscribers are reference counted per instrument.
        """
        resolved = self._resolve(instrument_id)
        if resolved is None:
            return
        product, raw_symbol = resolved
        if kind != _TICKER and product.is_spot:
            # A spot pair has no mark price, no index and no funding: the spot
            # ticker is 24-hour trade statistics and nothing else. The row
            # itself is real, though, which is why `GateioTicker` is allowed
            # here and the three reference prices are not.
            self._log.error(
                f"Cannot subscribe to {kind} prices for {instrument_id}: Gate.io publishes "
                f"them for derivative products only",
            )
            return
        if kind == _FUNDING and not product.is_perpetual:
            # A delivery contract converges on its settlement price instead of
            # paying funding, and its ticker reports a basis rather than a
            # funding rate; an option is a premium with no funding leg at all.
            # Either subscription would never produce data.
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
                if is_transient_ws_error(e):
                    # The transport holds the subscription and replays it on the
                    # next connection, and the replayed rows are routed by this
                    # registry alone: dropping the entry here would leave
                    # `_handle_tickers` discarding every row of a stream the
                    # venue is sending. Every other subscribe path in this client
                    # keeps its registry entry for the same reason.
                    self._log.warning(
                        f"Ticker subscription for {instrument_id} did not complete ({e}); "
                        f"it will be replayed on the next connection",
                    )
                    return
                self._ticker_subs.pop(instrument_id, None)
                self._log.error(f"Cannot subscribe to {kind} data for {instrument_id}: {e}")

    async def _release_ticker_channel(self, instrument_id: InstrumentId, kind: str) -> None:
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
        await self._hold_ticker_channel(command.instrument_id, _MARK)

    async def _unsubscribe_mark_prices(self, command: UnsubscribeMarkPrices) -> None:
        await self._release_ticker_channel(command.instrument_id, _MARK)

    async def _subscribe_index_prices(self, command: SubscribeIndexPrices) -> None:
        await self._hold_ticker_channel(command.instrument_id, _INDEX)

    async def _unsubscribe_index_prices(self, command: UnsubscribeIndexPrices) -> None:
        await self._release_ticker_channel(command.instrument_id, _INDEX)

    async def _subscribe_funding_rates(self, command: SubscribeFundingRates) -> None:
        await self._hold_ticker_channel(command.instrument_id, _FUNDING)

    async def _unsubscribe_funding_rates(self, command: UnsubscribeFundingRates) -> None:
        await self._release_ticker_channel(command.instrument_id, _FUNDING)

    # -- instrument lifecycle ----------------------------------------------

    async def _subscribe_instrument_status(self, command: SubscribeInstrumentStatus) -> None:
        """Register a status subscription and report the instrument's state now.

        Gate.io publishes no status channel on any product, so the source is the
        instrument listing endpoints, polled on the instrument reload cadence.
        Reporting immediately on subscribe is what makes a subscription useful in
        a quiet market: the periodic diff only fires on a *change*, so without
        this a strategy that subscribes to a healthy instrument would learn
        nothing until it stopped being healthy. The in-tree polled adapters
        (Kraken, dYdX) do the same.
        """
        resolved = self._resolve(command.instrument_id)
        if resolved is None:
            return
        instrument_id = command.instrument_id
        self._instrument_status_subs.add(instrument_id)
        if not self._config.update_instruments_interval_mins:
            self._log.warning(
                f"Subscribed to the instrument status for {instrument_id}, but "
                f"`update_instruments_interval_mins` is not set: Gate.io has no status channel, "
                f"so the status will be read once now and never refreshed",
            )

        action_reason = self._cached_status(instrument_id)
        if action_reason is None:
            action_reason = await self._fetch_instrument_status(*resolved)
        if action_reason is None:
            self._log.warning(
                f"Could not read the current instrument status for {instrument_id}; it will be "
                f"reported at the next instrument reload",
            )
            return
        action, reason = action_reason
        self._status_cache[instrument_id] = action
        self._emit_instrument_status(instrument_id, action, reason)

    async def _unsubscribe_instrument_status(self, command: UnsubscribeInstrumentStatus) -> None:
        # No venue channel to unsubscribe from; the status feed is derived from
        # the instrument listings this client already reloads. The cache entry
        # stays so that a later subscription can report the state at once.
        self._instrument_status_subs.discard(command.instrument_id)

    async def _subscribe_instrument_close(self, command: SubscribeInstrumentClose) -> None:
        """Watch for the settlement of a dated contract, and refuse for the rest.

        ``InstrumentClose.close_price`` is not optional and has no null form, so
        an adapter that cannot obtain a real settlement price must publish
        nothing at all. On Gate.io only delivery futures and options ever settle;
        spot, perpetual and inverse perpetual markets trade continuously, so
        there is no close price for them to publish and the subscription is
        refused with the reason rather than accepted and left silent.
        ``InstrumentCloseType.END_OF_SESSION`` is never emitted: Gate.io has no
        sessions, so naming one would be a fabrication.
        """
        resolved = self._resolve(command.instrument_id)
        if resolved is None:
            return
        product, _ = resolved
        instrument_id = command.instrument_id
        if not (product.is_delivery or product.is_option):
            self._log.error(
                f"Cannot subscribe to the instrument close for {instrument_id}: Gate.io "
                f"{product.value} markets trade continuously and never settle, so there is no "
                f"close price to publish; only delivery futures and options have one",
            )
            return
        if instrument_id in self._instrument_close_tasks:
            self._log.warning(f"Already subscribed to the instrument close for {instrument_id}")
            return
        self._instrument_close_tasks[instrument_id] = self.create_task(
            self._watch_instrument_close(instrument_id),
            log_msg=f"instrument close {instrument_id}",
        )

    async def _unsubscribe_instrument_close(self, command: UnsubscribeInstrumentClose) -> None:
        task = self._instrument_close_tasks.pop(command.instrument_id, None)
        if task is not None:
            task.cancel()

    async def _unsubscribe(self, command: UnsubscribeData) -> None:
        data_type = command.data_type
        if data_type.type is not GateioTicker:
            self._log.error(f"Cannot unsubscribe from {data_type.type} (not implemented)")
            return
        instrument_id = self._custom_data_instrument_id(command)
        if instrument_id is None:
            self._log.error(
                f"Cannot unsubscribe from {data_type}: no instrument ID on the command or in "
                f"the `data_type` metadata",
            )
            return
        await self._release_ticker_channel(instrument_id, _TICKER)

    async def _unsubscribe_instruments(self, command: UnsubscribeInstruments) -> None:
        pass  # No venue channel to unsubscribe from

    async def _unsubscribe_instrument(self, command: UnsubscribeInstrument) -> None:
        pass  # No venue channel to unsubscribe from

    @staticmethod
    def _custom_data_instrument_id(command: SubscribeData | UnsubscribeData) -> InstrumentId | None:
        """Read the instrument a custom-data command names.

        ``SubscribeData`` copies the instrument id into the ``DataType``
        metadata as well as keeping it as a field, so the direct field is
        preferred and the metadata is the fallback for a caller that built the
        ``DataType`` by hand — which may leave a plain string there.
        """
        instrument_id = command.instrument_id
        if instrument_id is not None:
            return instrument_id
        candidate = command.data_type.metadata.get("instrument_id")
        if isinstance(candidate, InstrumentId):
            return candidate
        if isinstance(candidate, str):
            try:
                return InstrumentId.from_str(candidate)
            except ValueError:
                return None
        return None

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
            elif channel.endswith(".order_book"):
                # After the incremental branch on purpose. The two suffixes are
                # disjoint (`"spot.order_book_update".endswith(".order_book")`
                # is False), so the order is not load-bearing, but keeping the
                # more specific channel first states which is which.
                self._handle_book_depth(product, result)
            elif channel.endswith("candlesticks"):
                self._handle_candlesticks(product, result)
            elif channel == tickers_channel(product):
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
        # The depth channel needs no resnapshot, but it does need its watermark
        # dropped. Gate.io restarts the `id` sequence of the snapshot channel on a
        # new connection, and the monotonic guard in `_handle_book_depth` would
        # then read every message of the new stream as a reordered one and drop
        # it silently while the subscription still looked healthy. The channel is
        # self-synchronising, so forgetting the last sequence costs nothing.
        for instrument_id in self._depth_streams:
            if instrument_id_to_gateio(instrument_id)[0] is product:
                self._depth_sequences.pop(instrument_id, None)
        book_ids = self._subscribed_book_ids(product)
        self._log.warning(
            f"Reconnected {product.value} WebSocket; resynchronising {len(book_ids)} order books",
        )
        for instrument_id in book_ids:
            book = self._books.get(instrument_id)
            if book is not None:
                book.reset()
            self._resyncs[product.value] += 1
            self.create_task(
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

        Except while the node is stopping. The gate refuses the snapshot, that
        refusal arrives here as a failure, and re-arming would answer it with a
        *new* task — created after `cancel_pending_tasks` has already taken its
        snapshot, so nothing will ever cancel it. The guard sits in this one
        funnel rather than at the three failure branches that call it, so a
        retry that fails again cannot re-arm itself either.
        """
        if not self._http_client.is_accepting:
            self._log.debug(
                f"Not re-arming the order book retry for {instrument_id}: transport is closing",
            )
            return

        async def _retry() -> None:
            await asyncio.sleep(SNAPSHOT_RETRY_DELAY_SECS)
            if instrument_id not in self._books:
                return  # unsubscribed while waiting
            await self._book_snapshot_then_deltas(instrument_id)

        self.create_task(_retry(), log_msg=f"book snapshot retry {instrument_id}")

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
            self.create_task(
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

    def _handle_book_depth(self, product: GateioProductType, result: Any) -> None:
        """Publish one ``*.order_book`` push as an ``OrderBookDepth10``.

        A pure parse with no book state: the channel replaces the whole book on
        every message, so there is nothing to accumulate and nothing to
        reconcile. Routing the payload through :class:`GateioOrderBook` instead
        would be the obvious reuse and would be wrong twice over — that class
        demands an ``id`` or ``u`` sequence field while the spot push names it
        ``lastUpdateId``, and it carries buffering, gap and staleness machinery
        this channel has no use for.
        """
        if not isinstance(result, dict) or not (result.get("s") or result.get("contract")):
            # Said out loud rather than dropped. Every message on this channel is
            # a whole book, so a body this parser cannot read is a stream that
            # has gone quiet while the subscription still reports healthy, and a
            # managed book frozen at its last snapshot. The delta path survives
            # the same silence because its sequence check notices the gap.
            self._log.warning(
                f"Discarding an unreadable {product.value} order book snapshot message: {result}",
            )
            return
        raw_symbol = result["s"] if result.get("s") else result["contract"]
        instrument_id = gateio_to_instrument_id(product, str(raw_symbol))
        if instrument_id not in self._depth_streams:
            return
        instrument = self._instrument(instrument_id)
        if instrument is None:
            return

        # Spot names the sequence `lastUpdateId`; the contract products name it
        # `id`. The platform performs no gap or staleness check of its own on
        # depth — unlike deltas there is no buffering and no validation path —
        # so a reordered push would silently roll a managed book backwards.
        sequence = to_int(result.get("id") or result.get("lastUpdateId"))
        if sequence:
            last_sequence = self._depth_sequences.get(instrument_id, 0)
            if last_sequence and sequence <= last_sequence:
                self._published["order_book_depths_out_of_order"] += 1
                return
            self._depth_sequences[instrument_id] = sequence

        ts_init = self._clock.timestamp_ns()
        ts_event = timestamp_to_nanos(result.get("t")) or ts_init

        bids = self._depth_side(instrument, result.get("bids"), OrderSide.BUY)
        asks = self._depth_side(instrument, result.get("asks"), OrderSide.SELL)
        depth = OrderBookDepth10(
            instrument_id=instrument_id,
            bids=bids,
            asks=asks,
            # Gate.io publishes aggregated price levels with no per-level order
            # count on any product; the type documents zeros as the value for
            # "data not available".
            bid_counts=[0] * DEPTH10_LEVELS,
            ask_counts=[0] * DEPTH10_LEVELS,
            # One `*.order_book` message is one complete book event, which is
            # exactly what F_LAST states. F_SNAPSHOT would be a misstatement: it
            # means the message came from a replay or snapshot server, and this
            # is a live push.
            flags=RecordFlag.F_LAST,
            sequence=sequence,
            ts_event=ts_event,
            ts_init=ts_init,
        )
        self._handle_data(depth)
        self._published["order_book_depths"] += 1

    def _depth_side(
        self,
        instrument: Instrument,
        levels: Any,
        side: OrderSide,
    ) -> list[BookOrder]:
        """Build one padded side of an ``OrderBookDepth10``.

        Sorting is the adapter's obligation, not the type's: ``OrderBookDepth10``
        accepts whatever order it is given and ``to_quote_tick`` blindly reads
        ``bids[0]``, so an unsorted payload would publish a wrong best price and,
        with ``emit_quotes_from_book_depths`` enabled, have the engine cache it
        as a quote.

        Padding is also ours. The type requires both sides to be the *same*
        length and Gate.io routinely sends asymmetric sides on thin contracts;
        an unequal pair raises inside a handler whose caller logs and continues,
        which would leave the subscription looking healthy while publishing
        nothing at all. Padding both sides to ten uses the type's own
        convention (it pads short sides with ``NULL_ORDER`` itself) and removes
        the failure.
        """
        ordered = sorted(
            parse_levels(levels),
            key=lambda level: level[0],
            reverse=side is OrderSide.BUY,
        )
        orders: list[BookOrder] = []
        for price, size in ordered:
            if len(orders) == DEPTH10_LEVELS:
                break
            quantity = venue_quantity(size, instrument.size_precision)
            if quantity == 0:
                # A level the instrument's size precision cannot represent. A
                # zero-sized `BookOrder` is not rejected here the way a delta
                # would be — `apply_depth` simply drops it — which is worse: it
                # would occupy one of the ten slots and then vanish, silently
                # shortening the published depth. The slot goes to the next
                # level that survives instead.
                self._published["book_levels_not_representable"] += 1
                continue
            orders.append(
                BookOrder(
                    side=side,
                    price=instrument.make_price(price),
                    size=quantity,
                    order_id=0,
                ),
            )
        # A fresh list per call: the constructor extends the list it is given
        # (`bids.extend(...)`), so a list reused for a second depth object would
        # carry the previous padding.
        return orders + [NULL_ORDER] * (DEPTH10_LEVELS - len(orders))

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
        """Build one ``Bar`` from a candlestick row, or ``None`` if it is unusable.

        The ``Bar`` construction is inside the guard, not after it. NautilusTrader
        enforces the OHLC invariants in the constructor — ``high`` at least
        ``open``/``low``/``close``, ``low`` at most ``open``/``close`` — so a
        venue row that violates them raises ``ValueError`` there and not in the
        parsing above. Illiquid delivery and option candles do produce such rows,
        and letting one escape aborts an entire ``request_bars`` response: the
        exception leaves the request coroutine, the ``DataResponse`` is never
        sent, and a caller following the documented request-then-subscribe
        pattern waits forever. One row is dropped instead.
        """
        try:
            open_price = instrument.make_price(item["o"])
            high_price = instrument.make_price(item["h"])
            low_price = instrument.make_price(item["l"])
            close_price = instrument.make_price(item["c"])

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
        except (KeyError, TypeError, ValueError) as e:
            # Counted rather than warned per row: a historical request can carry
            # a thousand candles, and the caller reports the total once.
            self._dropped_candles[bar_type] += 1
            self._log.debug(f"Dropped a {bar_type} candle opening at {open_secs}: {e}")
            return None

    def _bar_ts_event(self, bar_type: BarType, open_secs: int) -> int:
        ts_ns = open_secs * _NANOS_PER_SEC
        if not self._config.bars_timestamp_on_close:
            return ts_ns
        return ts_ns + self._bar_step_ms(bar_type) * 1_000_000

    # -- ticker-derived derivative data ------------------------------------

    def _handle_tickers(self, product: GateioProductType, result: Any) -> None:
        items = result if isinstance(result, list) else [result]
        ts_init = self._clock.timestamp_ns()
        for item in items:
            if not isinstance(item, dict):
                continue
            # ``futures.tickers`` names the contract ``contract``;
            # ``options.contract_tickers`` names the same field ``name``;
            # ``spot.tickers`` names the pair ``currency_pair``.
            raw_symbol = item.get("contract") or item.get("name") or item.get("currency_pair")
            if not raw_symbol:
                continue
            instrument_id = gateio_to_instrument_id(product, str(raw_symbol))
            kinds = self._ticker_subs.get(instrument_id)
            if not kinds:
                continue
            instrument = self._instrument(instrument_id)
            if instrument is None:
                continue
            # The options ticker carries no timestamp of its own, so its events
            # are stamped on arrival; the futures ticker sends ``t`` in ms.
            ts_event = timestamp_to_nanos(item.get("t")) or ts_init

            if _MARK in kinds:
                mark = venue_price(
                    item.get("mark_price"),
                    floor_precision=_mark_price_precision(instrument),
                )
                if mark is not None:
                    self._handle_data(
                        MarkPriceUpdate(
                            instrument_id=instrument_id,
                            value=mark,
                            ts_event=ts_event,
                            ts_init=ts_init,
                        ),
                    )
                    self._published["mark_prices"] += 1

            if _INDEX in kinds:
                # The index has no ``*_round`` field of its own: Gate.io states a
                # minimum unit for order and mark prices only, so the published
                # value's own scale is all there is to go on.
                index = venue_price(item.get("index_price"))
                if index is not None:
                    self._handle_data(
                        IndexPriceUpdate(
                            instrument_id=instrument_id,
                            value=index,
                            ts_event=ts_event,
                            ts_init=ts_init,
                        ),
                    )
                    self._published["index_prices"] += 1

            if _FUNDING in kinds and item.get("funding_rate") not in (None, ""):
                self._handle_data(
                    FundingRateUpdate(
                        instrument_id=instrument_id,
                        rate=to_decimal(item["funding_rate"]),
                        ts_event=ts_event,
                        ts_init=ts_init,
                        interval=self._funding_interval_mins(instrument),
                        next_funding_ns=self._next_funding_ns(instrument, ts_event),
                    ),
                )
                self._published["funding_rates"] += 1

            if _TICKER in kinds:
                # The venue fields the platform has no type for. Mark, index and
                # funding are published above as the platform's own types and are
                # deliberately absent from `GateioTicker`, so no consumer ever has
                # two sources for one number.
                #
                # The wrapper is not decoration. `DataEngine._handle_data`
                # dispatches on the concrete type and reaches a venue-native type
                # only through `CustomData` (`data/engine.pyx:2570-2571`); handing
                # it a bare `Data` subclass falls to
                # `self._log.error(f"Cannot handle data: unrecognized type ...")`
                # (`data/engine.pyx:2572-2573`), so every row would become an
                # error line while the client kept reporting the subscription
                # held. The metadata carries the instrument id because that is
                # what makes the published topic match the `DataType` a
                # subscriber asked for; `adapters/binance/data.py:1015-1020` is
                # the in-tree shape being followed.
                self._handle_data(
                    CustomData(
                        data_type=DataType(
                            GateioTicker,
                            metadata={"instrument_id": instrument_id},
                        ),
                        data=GateioTicker.from_payload(instrument_id, item, ts_event, ts_init),
                    ),
                )
                self._published["tickers"] += 1

    @staticmethod
    def _funding_interval_mins(instrument: Instrument) -> int | None:
        """Return the funding interval in minutes from the contract definition."""
        seconds = to_int(instrument.info.get("funding_interval")) if instrument.info else 0
        return seconds // 60 if seconds > 0 else None

    @staticmethod
    def _next_funding_ns(instrument: Instrument, ts_event: int) -> int | None:
        """Return the next funding time strictly after ``ts_event``, if it is known.

        Gate.io's ticker stream carries no next-funding timestamp. The only
        source is ``funding_next_apply`` on the contract definition, which this
        client refreshes on a timer (``update_instruments_interval_mins``,
        60 minutes by default) while the ticker pushes about once a second.
        Republishing that cached field verbatim therefore names the settlement
        that has *already happened* for up to a whole refresh interval, and
        ``next_funding_ns - clock.timestamp_ns()`` — the field's main use — comes
        out negative right after every funding.

        Rolling the anchor forward is exact rather than a guess:
        ``funding_next_apply`` is a point on the venue's funding grid and
        ``funding_interval`` is that grid's step (Gate.io schedules funding at
        ``funding_offset + k * funding_interval``), so adding whole intervals
        lands on real settlement times. Without an interval there is nothing to
        roll a stale anchor forward with, and
        ``concepts/data/funding_rate_update.md`` is explicit that ``interval``
        and ``next_funding_ns`` are to be used "only when the venue publishes
        them" — so ``None`` is the honest answer, not a timestamp in the past.
        """
        info = instrument.info or {}
        anchor_secs = to_int(info.get("funding_next_apply"))
        if anchor_secs <= 0:
            return None
        anchor_ns = anchor_secs * _NANOS_PER_SEC
        if anchor_ns > ts_event:
            return anchor_ns
        interval_secs = to_int(info.get("funding_interval"))
        if interval_secs <= 0:
            return None
        interval_ns = interval_secs * _NANOS_PER_SEC
        # Strictly future: an anchor landing exactly on ts_event is the funding
        # being applied now, so the next one is a whole interval later.
        elapsed_intervals = (ts_event - anchor_ns) // interval_ns
        return anchor_ns + (elapsed_intervals + 1) * interval_ns

    # -- instrument status -------------------------------------------------

    def _cached_status(self, instrument_id: InstrumentId) -> tuple[MarketStatusAction, str] | None:
        action = self._status_cache.get(instrument_id)
        if action is None:
            return None
        return action, "last observed instrument listing"

    def _emit_instrument_status(
        self,
        instrument_id: InstrumentId,
        action: MarketStatusAction,
        reason: str,
    ) -> None:
        """Publish one ``InstrumentStatus``.

        ``ts_event`` is the clock, not a venue field. The listing payloads carry
        ``create_time``, ``expire_time`` and ``expiration_time``, and none of
        them is the time of the transition — ``create_time`` is the listing
        instant and never moves, so using it would emit a stream of events whose
        ``ts_event`` never advances. Both in-tree polled adapters stamp on
        observation for the same reason.

        ``is_quoting`` and ``is_short_sell_restricted`` stay ``None``: Gate.io
        publishes neither, and the fields are tri-state precisely so an adapter
        can decline to guess.
        """
        ts_now = self._clock.timestamp_ns()
        self._handle_data(
            InstrumentStatus(
                instrument_id=instrument_id,
                action=action,
                ts_event=ts_now,
                ts_init=ts_now,
                reason=reason,
                is_trading=action == MarketStatusAction.TRADING,
            ),
        )
        self._published["instrument_status"] += 1
        self._log.info(
            f"{instrument_id} status {market_status_action_to_str(action)} ({reason})",
            LogColor.BLUE,
        )

    async def _fetch_instrument_status(
        self,
        product: GateioProductType,
        raw_symbol: str,
    ) -> tuple[MarketStatusAction, str] | None:
        """Read one instrument's listing row and map it to an action."""
        try:
            if product.is_spot:
                payload = await self._spot_http.currency_pair(raw_symbol)
            elif product.is_option:
                payload = await self._options_http.contract(raw_symbol)
            else:
                payload = await self._futures_http[product].contract(raw_symbol)
        except (GateioError, ValueError) as e:
            self._log.warning(f"Cannot read the {product.value} listing for {raw_symbol}: {e}")
            return None
        if not isinstance(payload, dict):
            return None
        return market_status_action(product, payload, self._clock.timestamp_ns() // _NANOS_PER_SEC)

    async def _poll_instrument_statuses(self) -> None:
        """Diff the current listings against the cache and emit what changed."""
        if not self._instrument_status_subs:
            return
        statuses, scopes = await self._request_instrument_statuses()
        if not scopes:
            # Nothing was read, so nothing can be said. Diffing against an empty
            # snapshot would report every subscribed instrument as delisted.
            self._log.warning(
                "No Gate.io instrument listing could be read; skipping the status diff",
            )
            return
        diff_and_emit_statuses(
            new_statuses=statuses,
            cached_statuses=self._status_cache,
            subscriptions=self._instrument_status_subs,
            emit=self._emit_instrument_status,
            removable=lambda instrument_id: self._status_scope(instrument_id) in scopes,
        )

    async def _request_instrument_statuses(
        self,
    ) -> tuple[dict[InstrumentId, tuple[MarketStatusAction, str]], set[str]]:
        """Return the statuses read this round and the listings that were complete.

        The second element is the set of *scopes* whose full listing came back:
        one per product family, and one per option underlying, because
        ``GET /options/contracts`` is queried one underlying at a time. Removal
        detection is confined to those scopes — a request that failed must not
        look like a mass delisting of everything it would have covered.
        """
        now_secs = self._clock.timestamp_ns() // _NANOS_PER_SEC
        statuses: dict[InstrumentId, tuple[MarketStatusAction, str]] = {}
        scopes: set[str] = set()

        products = {
            product
            for product in (self._product_of(item) for item in self._instrument_status_subs)
            if product is not None
        }
        for product in sorted(products, key=lambda item: item.value):
            if product.is_option:
                for underlying in sorted(self._subscribed_option_underlyings()):
                    await self._collect_statuses(
                        product,
                        f"opt:{underlying}",
                        lambda: self._options_http.contracts(underlying=underlying),  # noqa: B023
                        "name",
                        now_secs,
                        statuses,
                        scopes,
                    )
            elif product.is_spot:
                await self._collect_statuses(
                    product,
                    product.value,
                    self._spot_http.currency_pairs,
                    "id",
                    now_secs,
                    statuses,
                    scopes,
                )
            else:
                await self._collect_statuses(
                    product,
                    product.value,
                    self._futures_http[product].contracts,
                    "name",
                    now_secs,
                    statuses,
                    scopes,
                )
        return statuses, scopes

    async def _collect_statuses(
        self,
        product: GateioProductType,
        scope: str,
        fetch: Any,
        symbol_field: str,
        now_secs: int,
        statuses: dict[InstrumentId, tuple[MarketStatusAction, str]],
        scopes: set[str],
    ) -> None:
        """Read one listing and fold it into ``statuses``, or warn and skip it.

        Each listing is caught separately. A poll that let one product's failure
        propagate would abandon the diff for every other product, and a poll that
        treated the failure as an empty listing would fabricate a delisting for
        every instrument that listing covers.
        """
        try:
            rows = await fetch()
        except (GateioError, ValueError) as e:
            self._log.warning(f"Cannot read the {scope} instrument listing: {e}")
            return
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            raw_symbol = row.get(symbol_field)
            if not raw_symbol:
                continue
            try:
                instrument_id = gateio_to_instrument_id(product, str(raw_symbol))
            except ValueError:
                continue
            statuses[instrument_id] = market_status_action(product, row, now_secs)
        scopes.add(scope)

    def _product_of(self, instrument_id: InstrumentId) -> GateioProductType | None:
        try:
            return instrument_id_to_gateio(instrument_id)[0]
        except ValueError:  # pragma: no cover - subscriptions are resolved first
            return None

    def _status_scope(self, instrument_id: InstrumentId) -> str:
        """Return the listing scope an instrument's status would have come from."""
        product = self._product_of(instrument_id)
        if product is None:  # pragma: no cover - subscriptions are resolved first
            return ""
        if product.is_option:
            underlying = self._option_underlying(instrument_id)
            return f"opt:{underlying}" if underlying else ""
        return product.value

    def _subscribed_option_underlyings(self) -> set[str]:
        underlyings = set()
        for instrument_id in self._instrument_status_subs:
            product = self._product_of(instrument_id)
            if product is None or not product.is_option:
                continue
            underlying = self._option_underlying(instrument_id)
            if underlying:
                underlyings.add(underlying)
        return underlyings

    def _option_underlying(self, instrument_id: InstrumentId) -> str:
        """Return the underlying of an option, from the contract definition.

        Falls back to the symbol's own first segment: option names are
        ``<UNDERLYING>-<YYYYMMDD>-<STRIKE>-<C|P>``, so the prefix is the
        underlying even when no instrument definition is held yet.
        """
        instrument = self._instrument(instrument_id)
        info = instrument.info if instrument is not None else None
        underlying = (info or {}).get("underlying")
        if underlying:
            return str(underlying)
        return instrument_id.symbol.value.split("-")[0]

    # -- instrument close --------------------------------------------------

    async def _watch_instrument_close(self, instrument_id: InstrumentId) -> None:
        """Wait for expiry, then poll until the venue publishes a settlement.

        Nothing is published if the settlement never appears. There is no null
        ``close_price``, so "publish something" would mean publishing a number
        nobody quoted; ``Price(0)`` is the most tempting and the most harmful,
        because a backtest reading it books a total loss on the position.
        """
        instrument = self._instrument(instrument_id)
        if instrument is None:
            self._log.error(f"Cannot watch the instrument close: no instrument {instrument_id}")
            return
        expiration_ns = int(getattr(instrument, "expiration_ns", 0) or 0)
        if expiration_ns <= 0:
            self._log.error(
                f"Cannot watch the instrument close for {instrument_id}: the contract states "
                f"no expiry",
            )
            return

        delay_secs = (expiration_ns - self._clock.timestamp_ns()) / _NANOS_PER_SEC
        if delay_secs > 0:
            self._log.info(
                f"Watching {instrument_id} for settlement in {delay_secs / 3600:.1f}h",
                LogColor.BLUE,
            )
            await asyncio.sleep(delay_secs)

        deadline_ns = self._clock.timestamp_ns() + int(
            INSTRUMENT_CLOSE_TIMEOUT_SECS * _NANOS_PER_SEC,
        )
        while True:
            try:
                close = await self._fetch_instrument_close(instrument_id, instrument)
            except _SettlementConflict:
                return
            except (GateioError, ValueError) as e:
                self._log.warning(f"Cannot read the settlement for {instrument_id}: {e}")
                close = None
            if close is not None:
                if instrument_id in self._instrument_close_emitted:
                    return
                self._instrument_close_emitted.add(instrument_id)
                self._handle_data(close)
                self._published["instrument_closes"] += 1
                return
            if self._clock.timestamp_ns() >= deadline_ns:
                self._log.error(
                    f"Gate.io published no settlement for {instrument_id} within "
                    f"{INSTRUMENT_CLOSE_TIMEOUT_SECS / 60:.0f} minutes of expiry; no "
                    f"instrument close will be published for it",
                )
                return
            await asyncio.sleep(INSTRUMENT_CLOSE_POLL_SECS)

    async def _fetch_instrument_close(
        self,
        instrument_id: InstrumentId,
        instrument: Instrument,
    ) -> InstrumentClose | None:
        """Return the settled close, ``None`` if the venue has not settled it yet."""
        resolved = self._resolve(instrument_id)
        if resolved is None:  # pragma: no cover - resolved at subscribe time
            return None
        product, raw_symbol = resolved
        if product.is_option:
            return await self._option_close(instrument_id, instrument, raw_symbol)
        return await self._delivery_close(instrument_id, instrument, raw_symbol)

    async def _delivery_close(
        self,
        instrument_id: InstrumentId,
        instrument: Instrument,
        raw_symbol: str,
    ) -> InstrumentClose | None:
        """Read a delivery contract's settlement price from two public sources.

        ``GET /delivery/{settle}/contracts/{name}`` and
        ``GET /delivery/{settle}/tickers`` both publish ``settle_price`` for the
        same contract and both stay ``"0"`` until it settles. Reading only one
        would make a single stale or wrong field the whole answer, and the field
        drives an expiry event that cancels orders and closes positions in a
        backtest.
        """
        api = self._futures_http[GateioProductType.FUT]
        contract = await api.contract(raw_symbol)
        settle = venue_price((contract or {}).get("settle_price"))
        if settle is None or settle == 0:
            return None
        rows = await api.tickers(contract=raw_symbol)
        row = next((item for item in rows or [] if isinstance(item, dict)), {})
        confirm = venue_price(row.get("settle_price"))
        if confirm is None or confirm == 0:
            # The ticker has not caught up with the contract yet; try again
            # rather than treating the difference as a contradiction.
            return None
        if settle.as_decimal() != confirm.as_decimal():
            self._log.error(
                f"Refusing to publish an instrument close for {instrument_id}: "
                f"the contract reports settle_price={settle} and the ticker reports "
                f"settle_price={confirm}",
            )
            raise _SettlementConflict
        return self._instrument_close(instrument_id, instrument, settle)

    async def _option_close(
        self,
        instrument_id: InstrumentId,
        instrument: Instrument,
        raw_symbol: str,
    ) -> InstrumentClose | None:
        """Read an option's settled value from ``GET /options/settlements``.

        The row's ``settle_price`` is **not** the option's close price: it is
        Gate.io's averaged price of the *underlying* at expiry, while the option
        is quoted in USDT per unit of that underlying. Publishing it would put a
        number orders of magnitude too large into ``close_price`` for anything
        but a deep in-the-money contract. The option's own value is ``profit``,
        the per-contract cash intrinsic value, divided back out by the contract
        multiplier — cross-checked here against the intrinsic value implied by
        the settlement price and the strike.
        """
        expiry_secs = int(getattr(instrument, "expiration_ns", 0) or 0) // _NANOS_PER_SEC
        underlying = self._option_underlying(instrument_id)
        rows = await self._options_http.settlements(
            underlying,
            frm=max(expiry_secs - OPTION_SETTLEMENT_WINDOW_SECS, 0),
            to=expiry_secs + OPTION_SETTLEMENT_WINDOW_SECS,
        )
        row = next(
            (
                item
                for item in rows or []
                if isinstance(item, dict) and str(item.get("contract")) == raw_symbol
            ),
            None,
        )
        if row is None:
            return None

        # The contract terms come from the instrument's own first-class fields,
        # not from the venue payload kept in `info`. `CryptoOption` models the
        # multiplier, the strike and the call/put kind
        # (`model/instruments/crypto_option.pyx:216-217, :188`), and reading
        # `info` instead makes an instrument whose payload did not survive a
        # cache round-trip settle as a put with a zero strike — `bool(None)` is
        # False and no exception is raised anywhere on that path.
        multiplier = instrument.multiplier.as_decimal()
        if multiplier <= 0:
            self._log.error(
                f"Refusing to publish an instrument close for {instrument_id}: the contract "
                f"states no usable multiplier ({instrument.multiplier})",
            )
            raise _SettlementConflict
        value = to_decimal(row.get("profit")) / multiplier

        settle_price = to_decimal(row.get("settle_price"))
        # The settlement row's own strike is preferred because it is what the
        # venue applied to this settlement; the instrument's strike is the
        # fallback when the row omits it.
        strike = to_decimal(row.get("strike_price")) or instrument.strike_price.as_decimal()
        is_call = instrument.option_kind == OptionKind.CALL
        intrinsic = settle_price - strike if is_call else strike - settle_price
        intrinsic = max(intrinsic, Decimal(0))
        if abs(value - intrinsic) > instrument.price_increment.as_decimal():
            self._log.error(
                f"Refusing to publish an instrument close for {instrument_id}: the settlement "
                f"row's profit implies {value} per unit of {underlying} while its settle_price "
                f"and strike imply {intrinsic}",
            )
            raise _SettlementConflict

        close_price = venue_price(value)
        if close_price is None:  # pragma: no cover - `value` is always a finite Decimal
            return None
        return self._instrument_close(instrument_id, instrument, close_price)

    def _instrument_close(
        self,
        instrument_id: InstrumentId,
        instrument: Instrument,
        close_price: Price,
    ) -> InstrumentClose:
        """Build the close event, stamped at the contract's stated expiry.

        ``close_type`` is always ``CONTRACT_EXPIRED``. ``END_OF_SESSION`` exists
        in the enum and nothing prevents its use, but Gate.io has no sessions —
        every close this adapter publishes is an expiry.
        """
        return InstrumentClose(
            instrument_id=instrument_id,
            close_price=close_price,
            close_type=InstrumentCloseType.CONTRACT_EXPIRED,
            ts_event=int(getattr(instrument, "expiration_ns", 0) or 0),
            ts_init=self._clock.timestamp_ns(),
        )

    # -- requests ----------------------------------------------------------

    async def _request(self, request: RequestData) -> None:
        """Refuse a request for a venue-native data type, because none has a history.

        The one venue-native type this client publishes is
        :class:`GateioTicker`, and Gate.io serves it as a live channel only:
        ``GET /*/tickers`` returns the current row, not a series. There is
        therefore nothing to answer a windowed request with, and building an
        answer out of the current row would invent history — the same reason
        ``_request_quote_ticks`` refuses.

        The refusal is a log line rather than the base class's
        ``NotImplementedError`` so that the message names the venue fact instead
        of showing a traceback. Neither completes the request: the platform's
        request group is closed by a response alone, so a caller awaiting the
        callback waits either way.
        """
        self._log.error(
            f"Cannot request {request.data_type}: Gate.io publishes no history for any "
            f"venue-native data type. Subscribe for the live stream instead",
        )

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

    async def _request_quote_ticks(self, request: RequestQuoteTicks) -> None:
        """Refuse a historical-quote request, because Gate.io publishes no such history.

        Implemented only to refuse. Gate.io serves quotes as a live stream
        (``*.book_ticker``) and nowhere else: no product has a bid/ask history
        endpoint, and ``GET /*/tickers`` is a single current row rather than a
        series. Answering from that row would satisfy a "quotes received with
        valid timestamps, bid/ask prices and sizes" check while inventing
        history — the same fabrication version 0.2.0 removed.

        Inheriting the base class's ``NotImplementedError`` would also refuse,
        but as a task traceback instead of a sentence. Neither form produces a
        response: the platform opens a request group for every historical
        request and only a response closes it, so a caller awaiting the
        historical-data callback waits regardless. That is a property of the
        request pipeline shared with every in-tree adapter that cannot serve a
        request type, and it is why this is logged at error rather than warning.
        """
        self._log.error(
            f"Cannot request historical quotes for {request.instrument_id}: not published by "
            f"Gate.io on any product. Subscribe to quotes for the live best bid/offer, or "
            f"request an order book snapshot",
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

    async def _request_funding_rates(self, request: RequestFundingRates) -> None:
        """Answer a historical funding-rate request from ``/futures/{settle}/funding_rate``.

        The endpoint returns records of exactly two fields, ``{"t": <unix s>,
        "r": <decimal ratio>}``, newest first. ``r`` is a ratio and not a
        percentage, which is the unit ``FundingRateUpdate.rate`` wants.
        """
        resolved = self._resolve(request.instrument_id)
        if resolved is None:
            return
        product, raw_symbol = resolved
        if not product.is_perpetual:
            self._log.error(
                f"Cannot request funding rates for {request.instrument_id}: only perpetual "
                f"contracts pay funding on Gate.io",
            )
            return
        instrument = self._instrument(request.instrument_id)
        if instrument is None:
            self._log.error(f"Cannot find instrument for {request.instrument_id}")
            return

        limit = min(request.limit or MAX_REST_LIMIT, MAX_REST_LIMIT)
        rows = await self._futures_http[product].funding_rate(raw_symbol, limit=limit)

        start_ns = dt_to_unix_nanos(request.start) if request.start is not None else 0
        end_ns = dt_to_unix_nanos(request.end) if request.end is not None else 0
        interval_mins = self._funding_interval_mins(instrument)
        ts_init = self._clock.timestamp_ns()
        rates: list[FundingRateUpdate] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            ts_event = timestamp_to_nanos(row.get("t"))
            rate = row.get("r")
            if not ts_event or rate in (None, ""):
                continue
            if start_ns and ts_event < start_ns:
                continue
            if end_ns and ts_event > end_ns:
                continue
            rates.append(
                FundingRateUpdate(
                    instrument_id=request.instrument_id,
                    rate=to_decimal(rate),
                    ts_event=ts_event,
                    ts_init=ts_init,
                    # The interval is a property of the contract, so it is as
                    # true of a past settlement as of the next one.
                    interval=interval_mins,
                    # No next-funding time: this endpoint publishes none, and the
                    # record's own timestamp is the application instant rounded
                    # up by a second, so deriving one from it would be off the
                    # funding grid. concepts/data/funding_rate_update.md asks for
                    # the field "only when the venue publishes them".
                    next_funding_ns=None,
                ),
            )
        rates.sort(key=lambda item: item.ts_event)

        self._handle_funding_rates(
            request.instrument_id,
            rates,
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

        dropped_before = self._dropped_candles[bar_type]
        bars: list[Bar] = []
        for open_secs, row in rows:
            if open_secs + step_secs > now_secs:
                continue  # the current bucket is still open
            bar = self._build_bar(bar_type, instrument, row, product, open_secs)
            if bar is not None:
                bars.append(bar)
        dropped = self._dropped_candles[bar_type] - dropped_before
        if dropped:
            # The response still goes out. A request that answers with nothing at
            # all is indistinguishable from a venue with no history, and the
            # documented request-then-subscribe pattern subscribes from inside
            # this response's callback.
            self._log.warning(
                f"Dropped {dropped} of {len(rows)} {bar_type} candles the venue returned; "
                f"responding with {len(bars)}",
            )
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
    "DEPTH10_LEVELS",
    "INSTRUMENT_CLOSE_POLL_SECS",
    "INSTRUMENT_CLOSE_TIMEOUT_SECS",
    "GateioDataClient",
    "GateioTicker",
    "bar_type_to_interval",
    "timestamp_to_nanos",
    "venue_price",
    "venue_quantity",
]
