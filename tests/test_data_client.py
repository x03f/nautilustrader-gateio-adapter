"""Regression tests for :class:`nautilus_gateio.data.GateioDataClient`.

The client is built with real NautilusTrader components (message bus, cache,
clock) and a stub instrument provider. Every REST call is stubbed and no socket
is opened, so the tests need no network and no credentials.

The instruments are built by the adapter's own parsers so that the precisions
under test are the ones the adapter really publishes: ``size_precision`` is 6 on
the spot pair and 0 on every contract product.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.data.messages import RequestOrderBookSnapshot
from nautilus_trader.model.data import (
    Bar,
    BarType,
    CustomData,
    FundingRateUpdate,
    IndexPriceUpdate,
    InstrumentClose,
    InstrumentStatus,
    MarkPriceUpdate,
    OptionGreeks,
    OrderBookDelta,
    OrderBookDeltas,
    OrderBookDepth10,
    QuoteTick,
    TradeTick,
)
from nautilus_trader.model.enums import BookAction, BookType, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, TraderId
from nautilus_trader.model.instruments import Instrument

from nautilus_gateio import data as data_module
from nautilus_gateio.books import GateioOrderBook
from nautilus_gateio.common.constants import (
    GATEIO_CLIENT_ID,
    GATEIO_HTTP_MAINNET,
    GATEIO_VENUE,
)
from nautilus_gateio.common.enums import GateioProductType
from nautilus_gateio.common.errors import GateioClientError
from nautilus_gateio.config import ORDER_BOOK_UPDATE_INTERVALS_MS, GateioDataClientConfig
from nautilus_gateio.data import GateioDataClient, venue_quantity
from nautilus_gateio.http.client import GateioHttpClient, RateLimiter
from nautilus_gateio.http.futures import GateioFuturesHttpAPI
from nautilus_gateio.http.options import GateioOptionsHttpAPI
from nautilus_gateio.http.spot import GateioSpotHttpAPI
from nautilus_gateio.instruments import (
    parse_option_instrument,
    parse_perpetual_instrument,
    parse_spot_instrument,
)
from nautilus_gateio.websocket import public as public_module

SPOT_ID = InstrumentId.from_str("BTC_USDT.GATE_IO")
PERP_ID = InstrumentId.from_str("BTC_USDT-PERP.GATE_IO")
OPTION_SYMBOL = "BTC_USDT-20260731-70000-C"
OPTION_ID = InstrumentId.from_str(f"{OPTION_SYMBOL}.GATE_IO")


# -- fixtures ----------------------------------------------------------------


class StubProvider(InstrumentProvider):
    """An instrument provider preloaded from memory; it performs no I/O."""

    async def load_all_async(self, filters: dict | None = None) -> None:
        return None

    async def load_ids_async(self, instrument_ids: list, filters: dict | None = None) -> None:
        return None

    async def load_async(self, instrument_id: Any, filters: dict | None = None) -> None:
        return None


def build_instruments() -> list[Any]:
    spot = parse_spot_instrument(
        {
            "id": "BTC_USDT",
            "base": "BTC",
            "quote": "USDT",
            "precision": 2,
            "amount_precision": 6,
            "min_base_amount": "0.0001",
            "min_quote_amount": "3",
            "fee": "0.2",
            "trade_status": "tradable",
        },
    )
    perp = parse_perpetual_instrument(
        {
            "name": "BTC_USDT",
            "quanto_multiplier": "0.0001",
            "order_price_round": "0.1",
            "mark_price_round": "0.1",
            "maker_fee_rate": "0.0002",
            "taker_fee_rate": "0.0005",
            # Gate.io publishes a maintenance rate on every contract it lists,
            # and the parser refuses one that is missing rather than reading it
            # as a zero maintenance requirement.
            "maintenance_rate": "0.003",
            "leverage_min": "1",
            "leverage_max": "100",
            "order_size_min": 1,
            "order_size_max": 1_000_000,
            "create_time": 1_700_000_000,
        },
        GateioProductType.PERP,
    )
    option = parse_option_instrument(
        {
            "name": OPTION_SYMBOL,
            "underlying": "BTC_USDT",
            "is_call": True,
            "multiplier": "0.01",
            "strike_price": "70000",
            "order_price_round": "0.1",
            "mark_price_round": "0.1",
            "maker_fee_rate": "0.0003",
            "taker_fee_rate": "0.0003",
            "order_size_min": 1,
            "order_size_max": 100_000,
            "expiration_time": 1_785_000_000,
            "create_time": 1_700_000_000,
        },
    )
    assert spot is not None and perp is not None and option is not None
    # The invariant the fractional-size findings hinge on.
    assert spot.size_precision == 6
    assert perp.size_precision == 0
    assert option.size_precision == 0
    return [spot, perp, option]


#: The concrete types ``DataEngine._handle_data`` dispatches on
#: (``data/engine.pyx:2541-2571``). Everything else reaches its ``else`` branch
#: and is logged as ``Cannot handle data: unrecognized type`` and dropped
#: (``:2572-2573``), which is a failure only an error line records. A
#: venue-native type therefore has to arrive wrapped in ``CustomData``.
ENGINE_DISPATCHED_TYPES: tuple[type, ...] = (
    OrderBookDelta,
    OrderBookDeltas,
    OrderBookDepth10,
    QuoteTick,
    TradeTick,
    MarkPriceUpdate,
    IndexPriceUpdate,
    FundingRateUpdate,
    Bar,
    Instrument,
    InstrumentStatus,
    InstrumentClose,
    OptionGreeks,
    CustomData,
)

#: Types published through a `Harness` that `DataEngine._handle_data` would not
#: dispatch. Written by the seam, read and cleared after every test by the
#: `_no_undispatchable_publish` fixture in `tests/conftest.py`.
UNDISPATCHABLE_PUBLISHES: list[str] = []


class Harness:
    """A constructed data client plus everything the tests published through it."""

    def __init__(self, client: GateioDataClient) -> None:
        self.client = client
        self.published: list[Any] = []
        client._handle_data = self._record  # type: ignore[method-assign]

    def _record(self, data: Any) -> None:
        """Record one published object, and note one the engine could not dispatch.

        This seam is what hid a real defect: replacing ``_handle_data`` with
        ``list.append`` meant an object the engine drops looked published to
        every assertion in the suite, and a venue-native type published outside
        ``CustomData`` passed 1968 tests while reaching no subscriber. The check
        lives on the seam rather than in one test, so every data test in the
        package carries it.

        It records rather than raises because most publishes happen inside
        ``_handle_ws_message``, which catches per-message exceptions so one bad
        payload never kills the stream — an assertion here would be swallowed
        exactly like the defect it is looking for. ``_no_undispatchable_publish``
        in ``tests/conftest.py`` reads the record after the test, where nothing
        can swallow it.
        """
        if not isinstance(data, ENGINE_DISPATCHED_TYPES):
            UNDISPATCHABLE_PUBLISHES.append(type(data).__name__)
        self.published.append(data)

    def deltas(self) -> list[OrderBookDeltas]:
        return [item for item in self.published if isinstance(item, OrderBookDeltas)]

    def quotes(self) -> list[QuoteTick]:
        return [item for item in self.published if isinstance(item, QuoteTick)]

    def trades(self) -> list[TradeTick]:
        return [item for item in self.published if isinstance(item, TradeTick)]

    def bars(self) -> list[Bar]:
        return [item for item in self.published if isinstance(item, Bar)]


@pytest.fixture()
def harness() -> Harness:
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    cache = Cache()
    provider = StubProvider()
    for instrument in build_instruments():
        provider.add(instrument)
    client = GateioDataClient(
        loop=asyncio.new_event_loop(),
        client_id=GATEIO_CLIENT_ID,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        instrument_provider=provider,
        http_client=object(),
        config=GateioDataClientConfig(
            products=(GateioProductType.SPOT, GateioProductType.PERP, GateioProductType.OPT),
        ),
    )
    return Harness(client)


# -- payload builders --------------------------------------------------------


def spot_book_message(
    first_id: int,
    last_id: int,
    bids: list[tuple[str, str]] | None = None,
    asks: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """``spot.order_book_update``: levels are ``["price", "amount"]`` arrays."""
    return {
        "channel": "spot.order_book_update",
        "event": "update",
        "result": {
            "t": 1_700_000_000_100,
            "s": "BTC_USDT",
            "U": first_id,
            "u": last_id,
            "b": [[price, size] for price, size in (bids or [])],
            "a": [[price, size] for price, size in (asks or [])],
        },
    }


def contract_book_message(
    channel: str,
    symbol: str,
    first_id: int,
    last_id: int,
    bids: list[tuple[str, str]] | None = None,
    asks: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """``futures``/``options.order_book_update``: levels are ``{"p", "s"}`` objects."""
    return {
        "channel": channel,
        "event": "update",
        "result": {
            "t": 1_700_000_000_100,
            "s": symbol,
            "U": first_id,
            "u": last_id,
            "b": [{"p": price, "s": size} for price, size in (bids or [])],
            "a": [{"p": price, "s": size} for price, size in (asks or [])],
        },
    }


def rest_snapshot(book_id: int, bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> dict:
    return {
        "id": book_id,
        "current": 1_700_000_000_000,
        "update": 1_700_000_000_000,
        "bids": [{"p": price, "s": size} for price, size in bids],
        "asks": [{"p": price, "s": size} for price, size in asks],
    }


def seed_book(harness: Harness, instrument_id: InstrumentId, symbol: str, book_id: int) -> None:
    book = GateioOrderBook(symbol)
    book.apply_snapshot(rest_snapshot(book_id, [("99", "5")], [("101", "5")]))
    harness.client._books[instrument_id] = book


# -- md-01: fractional contract sizes ----------------------------------------


def test_fractional_contract_size_does_not_drop_the_delta_batch(harness: Harness) -> None:
    """Regression for md-01 (critical).

    A futures level whose size is below one whole contract used to be published
    as ``UPDATE`` with a ``Quantity`` that had rounded to zero. NautilusTrader
    rejects a zero-sized UPDATE, the exception aborted the entire batch, and the
    local book — already advanced — diverged from the venue permanently.
    """
    seed_book(harness, PERP_ID, "BTC_USDT", 100)

    harness.client._handle_ws_message(
        GateioProductType.PERP,
        contract_book_message(
            "futures.order_book_update",
            "BTC_USDT",
            101,
            102,
            bids=[("99", "0.3"), ("98", "2")],
        ),
    )

    batches = harness.deltas()
    assert len(batches) == 1, "the whole delta batch was dropped"
    published = {
        (delta.order.price.as_double(), delta.action): delta.order.size
        for delta in batches[0].deltas
    }
    # 0.3 contracts is not representable at size_precision=0, so the level is
    # reported as absent rather than with a fabricated size.
    assert published[(99.0, BookAction.DELETE)] == 0
    assert published[(98.0, BookAction.UPDATE)] == 2
    assert harness.client.metrics()["published"]["book_levels_not_representable"] == 1


def test_sizes_are_truncated_toward_zero_never_rounded_up(harness: Harness) -> None:
    """Rounding up would publish depth the venue never showed.

    Gate.io itself truncates toward zero when the size-decimal opt-in is not
    requested ("the size of 1.1, 1.5, and 1.7 will be 1"), so the adapter does
    the same and the two agree whether or not the venue sends fractions.
    """
    assert venue_quantity("1.7", 0) == 1
    assert venue_quantity("1.5", 0) == 1
    assert venue_quantity("0.9", 0) == 0
    assert venue_quantity("0.0000019", 6) == Decimal("0.000001")
    assert venue_quantity("", 0) == 0

    seed_book(harness, PERP_ID, "BTC_USDT", 100)
    harness.client._handle_ws_message(
        GateioProductType.PERP,
        contract_book_message(
            "futures.order_book_update", "BTC_USDT", 101, 102, bids=[("99", "1.7")]
        ),
    )
    assert harness.deltas()[0].deltas[0].order.size == 1


def test_snapshot_batch_skips_levels_below_the_instrument_increment(harness: Harness) -> None:
    """A zero-sized ADD is rejected by NautilusTrader, so it must not be built."""
    book = GateioOrderBook("BTC_USDT")
    book.apply_snapshot(rest_snapshot(100, [("99", "0.4"), ("98", "3")], [("101", "2")]))
    deltas = harness.client._snapshot_deltas(PERP_ID, book)

    assert deltas is not None
    actions = [delta.action for delta in deltas.deltas]
    assert actions.count(BookAction.CLEAR) == 1
    prices = [d.order.price.as_double() for d in deltas.deltas if d.action == BookAction.ADD]
    assert prices == [98.0, 101.0]
    assert harness.client.metrics()["published"]["book_levels_not_representable"] == 1


# -- md-06: fractional best bid/offer sizes ----------------------------------


def test_book_ticker_publishes_a_quote_with_both_sizes(harness: Harness) -> None:
    harness.client._handle_ws_message(
        GateioProductType.PERP,
        {
            "channel": "futures.book_ticker",
            "event": "update",
            "result": {
                "t": 1_700_000_000_000,
                "u": 1,
                "s": "BTC_USDT",
                "b": "99.9",
                "B": "12",
                "a": "100.1",
                "A": "7",
            },
        },
    )
    quote = harness.quotes()[0]
    assert quote.bid_size == 12
    assert quote.ask_size == 7


@pytest.mark.parametrize(
    ("bid_size", "ask_size"),
    [("0.4", "7"), ("12", "0.4"), ("0", "7"), (None, "7"), ("12", "")],
)
def test_book_ticker_with_an_unrepresentable_size_is_skipped(
    harness: Harness,
    bid_size: str | None,
    ask_size: str,
) -> None:
    """Regression for md-06: a fractional BBO size became a silent zero.

    ``Quantity(0.4, 0)`` is zero, and a quote asserting a zero-sized top of book
    is worse than no quote at all.
    """
    result: dict[str, Any] = {
        "t": 1_700_000_000_000,
        "u": 1,
        "s": "BTC_USDT",
        "b": "99.9",
        "a": "100.1",
        "A": ask_size,
    }
    if bid_size is not None:
        result["B"] = bid_size

    harness.client._handle_ws_message(
        GateioProductType.PERP,
        {"channel": "futures.book_ticker", "event": "update", "result": result},
    )

    assert harness.quotes() == []
    assert harness.client.metrics()["published"]["quote_ticks_skipped"] == 1


# -- md-04: trades without a venue id ----------------------------------------


def test_trade_without_an_id_is_dropped(harness: Harness) -> None:
    """Regression for md-04: ``TradeId(str(None))`` produced the literal id "None"."""
    harness.client._handle_ws_message(
        GateioProductType.SPOT,
        {
            "channel": "spot.trades",
            "event": "update",
            "result": {
                "create_time_ms": "1700000000000",
                "currency_pair": "BTC_USDT",
                "side": "buy",
                "price": "100.0",
                "amount": "0.5",
            },
        },
    )
    assert harness.trades() == []
    assert harness.client.metrics()["published"]["trade_ticks_skipped"] == 1


def test_trade_with_an_empty_id_is_dropped(harness: Harness) -> None:
    harness.client._handle_ws_message(
        GateioProductType.SPOT,
        {
            "channel": "spot.trades",
            "event": "update",
            "result": {
                "id": "",
                "create_time_ms": "1700000000000",
                "currency_pair": "BTC_USDT",
                "side": "sell",
                "price": "100.0",
                "amount": "0.5",
            },
        },
    )
    assert harness.trades() == []


def test_trade_with_an_id_keeps_the_venue_id_verbatim(harness: Harness) -> None:
    harness.client._handle_ws_message(
        GateioProductType.SPOT,
        {
            "channel": "spot.trades",
            "event": "update",
            "result": {
                "id": 918273645,
                "create_time_ms": "1700000000000",
                "currency_pair": "BTC_USDT",
                "side": "buy",
                "price": "100.0",
                "amount": "0.5",
            },
        },
    )
    trade = harness.trades()[0]
    assert trade.trade_id.value == "918273645"
    assert trade.size == Decimal("0.5")


def test_futures_trade_below_one_contract_is_dropped(harness: Harness) -> None:
    """A trade size that truncates to zero cannot be published as a real trade."""
    harness.client._handle_ws_message(
        GateioProductType.PERP,
        {
            "channel": "futures.trades",
            "event": "update",
            "result": [
                {
                    "id": 5,
                    "create_time_ms": "1700000000000",
                    "contract": "BTC_USDT",
                    "size": "-0.4",
                    "price": "100.0",
                }
            ],
        },
    )
    assert harness.trades() == []
    assert harness.client.metrics()["published"]["trade_ticks_skipped"] == 1


# -- the book sequence through the client, per product -----------------------


@pytest.mark.parametrize(
    ("product", "instrument_id", "message"),
    [
        (GateioProductType.SPOT, SPOT_ID, "spot"),
        (GateioProductType.PERP, PERP_ID, "futures"),
        (GateioProductType.OPT, OPTION_ID, "options"),
    ],
)
def test_book_deltas_published_for_every_level_container_shape(
    harness: Harness,
    product: GateioProductType,
    instrument_id: InstrumentId,
    message: str,
) -> None:
    """Spot sends positional arrays, futures and options send ``{p, s}`` objects."""
    symbol = "BTC_USDT" if message != "options" else OPTION_SYMBOL
    seed_book(harness, instrument_id, symbol, 100)

    if message == "spot":
        msg = spot_book_message(101, 102, bids=[("99", "3")], asks=[("101", "0")])
    else:
        msg = contract_book_message(
            f"{message}.order_book_update",
            symbol,
            101,
            102,
            bids=[("99", "3")],
            asks=[("101", "0")],
        )
    harness.client._handle_ws_message(product, msg)

    batch = harness.deltas()[0]
    assert batch.instrument_id == instrument_id
    by_action = {delta.action: delta for delta in batch.deltas}
    assert by_action[BookAction.UPDATE].order.side == OrderSide.BUY
    assert by_action[BookAction.UPDATE].order.size == 3
    assert by_action[BookAction.DELETE].order.price.as_double() == 101.0
    assert batch.deltas[-1].flags != 0  # F_LAST on the final delta


def test_updates_are_buffered_until_the_snapshot_arrives(harness: Harness) -> None:
    """Notifications received before the REST snapshot must not be published."""
    client = harness.client
    client._books[PERP_ID] = GateioOrderBook("BTC_USDT")

    client._handle_ws_message(
        GateioProductType.PERP,
        contract_book_message(
            "futures.order_book_update", "BTC_USDT", 101, 105, bids=[("99", "4")]
        ),
    )
    assert harness.deltas() == []
    assert client._books[PERP_ID].updates_buffered == 1


async def test_seeding_replays_the_buffer_and_publishes_a_clean_snapshot(
    harness: Harness,
) -> None:
    client = harness.client
    client._books[PERP_ID] = GateioOrderBook("BTC_USDT")
    client._book_levels[PERP_ID] = 100

    client._handle_ws_message(
        GateioProductType.PERP,
        contract_book_message(
            "futures.order_book_update", "BTC_USDT", 101, 105, bids=[("98", "4")]
        ),
    )

    async def _fetch(product: Any, symbol: str, limit: int) -> dict[str, Any]:
        return rest_snapshot(100, [("99", "5")], [("101", "5")])

    client._fetch_book_snapshot = _fetch  # type: ignore[method-assign]
    await client._book_snapshot_then_deltas(PERP_ID)

    batch = harness.deltas()[0]
    assert batch.deltas[0].action == BookAction.CLEAR
    added = {
        d.order.price.as_double(): d.order.size for d in batch.deltas if d.action == BookAction.ADD
    }
    # The buffered notification was replayed into the snapshot before publishing.
    assert added == {99.0: 5, 98.0: 4, 101.0: 5}
    assert batch.deltas[-1].sequence == 105


async def test_a_gap_resyncs_and_republishes_a_clean_snapshot(harness: Harness) -> None:
    """A sequence break must rebuild the book, not silence the subscription."""
    client = harness.client
    seed_book(harness, PERP_ID, "BTC_USDT", 100)
    client._book_levels[PERP_ID] = 100

    scheduled: list[Any] = []
    # The client schedules its recovery work through the platform's
    # `LiveDataClient.create_task`; capture the coroutine instead of running it.
    client.create_task = lambda coro, **_: scheduled.append(coro)  # type: ignore[method-assign]

    client._handle_ws_message(
        GateioProductType.PERP,
        contract_book_message(
            "futures.order_book_update", "BTC_USDT", 500, 510, bids=[("99", "9")]
        ),
    )

    assert harness.deltas() == [], "a gap must not publish deltas"
    assert client.metrics()["gaps"]["PERP"] == 1
    assert not client._books[PERP_ID].is_synced

    async def _fetch(product: Any, symbol: str, limit: int) -> dict[str, Any]:
        return rest_snapshot(600, [("99.5", "6")], [("100.5", "6")])

    client._fetch_book_snapshot = _fetch  # type: ignore[method-assign]
    await scheduled[0]

    batch = harness.deltas()[0]
    assert batch.deltas[0].action == BookAction.CLEAR
    assert {d.order.price.as_double() for d in batch.deltas if d.action == BookAction.ADD} == {
        99.5,
        100.5,
    }
    assert client._books[PERP_ID].is_synced


async def test_a_stale_rest_snapshot_does_not_roll_the_book_back(harness: Harness) -> None:
    """Regression for md-02, at the client level.

    While the REST request is in flight the stream resynchronises the book with
    a ``full`` message. The late snapshot must be discarded, not republished.
    """
    client = harness.client
    seed_book(harness, PERP_ID, "BTC_USDT", 100)
    client._book_levels[PERP_ID] = 100

    async def _fetch(product: Any, symbol: str, limit: int) -> dict[str, Any]:
        # The venue pushes a full snapshot while this request is outstanding.
        client._handle_ws_message(
            GateioProductType.PERP,
            {
                "channel": "futures.order_book_update",
                "event": "update",
                "result": {
                    "t": 1_700_000_000_200,
                    "s": "BTC_USDT",
                    "u": 900,
                    "full": True,
                    "b": [{"p": "99.9", "s": "7"}],
                    "a": [{"p": "100.1", "s": "8"}],
                },
            },
        )
        return rest_snapshot(500, [("50", "1")], [("150", "1")])

    client._fetch_book_snapshot = _fetch  # type: ignore[method-assign]
    await client._book_snapshot_then_deltas(PERP_ID)

    # Exactly one batch: the republish triggered by the `full` message. The
    # stale REST snapshot (id 500) must add nothing.
    sequences = [batch.deltas[0].sequence for batch in harness.deltas()]
    assert sequences == [900], "a stale snapshot was republished"
    book = client._books[PERP_ID]
    assert book.last_update_id == 900
    assert book.best_bid() == (Decimal("99.9"), Decimal("7"))
    assert book.snapshots_stale == 1


# -- md-03: a REST failure must not kill the subscription --------------------


async def test_rest_failure_while_seeding_is_retried_not_fatal(harness: Harness) -> None:
    """Regression for md-03.

    The REST call sat outside the retry scope, so a single transport error left
    the book unsynchronised forever: every further notification is buffered and
    nothing is ever published again.
    """
    client = harness.client
    client._books[PERP_ID] = GateioOrderBook("BTC_USDT")
    client._book_levels[PERP_ID] = 100
    attempts: list[int] = []

    async def _fetch(product: Any, symbol: str, limit: int) -> dict[str, Any]:
        attempts.append(limit)
        if len(attempts) == 1:
            raise GateioClientError(502, "SERVER_ERROR", "bad gateway")
        return rest_snapshot(100, [("99", "5")], [("101", "5")])

    client._fetch_book_snapshot = _fetch  # type: ignore[method-assign]
    await client._book_snapshot_then_deltas(PERP_ID)

    assert len(attempts) == 2
    assert client._books[PERP_ID].is_synced
    assert harness.deltas(), "the book was never published after a transient REST error"
    assert client.metrics()["snapshot_errors"]["PERP"] == 1


async def test_persistent_rest_failure_schedules_a_retry_and_keeps_the_book(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = harness.client
    client._books[PERP_ID] = GateioOrderBook("BTC_USDT")
    client._book_levels[PERP_ID] = 100
    monkeypatch.setattr(data_module, "SNAPSHOT_ATTEMPTS", 2)

    async def _fetch(product: Any, symbol: str, limit: int) -> dict[str, Any]:
        raise GateioClientError(500, "SERVER_ERROR", "unavailable")

    retries: list[InstrumentId] = []
    client._fetch_book_snapshot = _fetch  # type: ignore[method-assign]
    client._schedule_book_retry = retries.append  # type: ignore[method-assign]

    await client._book_snapshot_then_deltas(PERP_ID)

    assert retries == [PERP_ID], "the subscription was abandoned instead of retried"
    assert PERP_ID in client._books
    assert PERP_ID not in client._resyncing


async def test_a_snapshot_without_an_id_is_retried_too(harness: Harness) -> None:
    """``with_id`` dropped by a proxy raises ValueError, not GateioError."""
    client = harness.client
    client._books[PERP_ID] = GateioOrderBook("BTC_USDT")
    client._book_levels[PERP_ID] = 100
    calls: list[int] = []

    async def _fetch(product: Any, symbol: str, limit: int) -> dict[str, Any]:
        calls.append(limit)
        if len(calls) == 1:
            return {"bids": [], "asks": []}  # no 'id'
        return rest_snapshot(100, [("99", "5")], [("101", "5")])

    client._fetch_book_snapshot = _fetch  # type: ignore[method-assign]
    await client._book_snapshot_then_deltas(PERP_ID)

    assert len(calls) == 2
    assert client._books[PERP_ID].is_synced


# -- seam-04: per-product snapshot depth -------------------------------------


class StubOptionsHttp:
    """Records the depth an options order book snapshot was requested with."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def order_book(self, contract: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"contract": contract, **kwargs})
        return rest_snapshot(10, [("5.0", "3")], [("5.5", "4")])


async def test_options_snapshot_request_uses_the_options_depth_table(harness: Harness) -> None:
    """Regression for seam-04.

    Options accept at most 50 levels. The request path clamped against the
    global table, which allows 100, so the default configuration produced a
    request the venue rejects.
    """
    client = harness.client
    options_http = StubOptionsHttp()
    client._options_http = options_http
    client._handle_order_book_deltas = lambda *args, **kwargs: None  # type: ignore[method-assign]

    request = RequestOrderBookSnapshot(
        instrument_id=OPTION_ID,
        limit=100,
        client_id=GATEIO_CLIENT_ID,
        venue=GATEIO_VENUE,
        callback=lambda data: None,
        request_id=UUID4(),
        ts_init=0,
        params=None,
    )
    await client._request_order_book_snapshot(request)

    assert options_http.calls == [{"contract": OPTION_SYMBOL, "limit": 50, "with_id": True}]


@pytest.mark.parametrize(
    ("product", "requested", "expected"),
    [
        (GateioProductType.OPT, 100, 50),
        (GateioProductType.OPT, 30, 50),
        (GateioProductType.OPT, 7, 10),
        (GateioProductType.SPOT, 100, 100),
        (GateioProductType.SPOT, 3, 5),
        (GateioProductType.PERP, 1000, 100),
        (GateioProductType.FUT, 1, 1),
    ],
)
def test_nearest_snapshot_limit_is_per_product(
    product: GateioProductType,
    requested: int,
    expected: int,
) -> None:
    assert public_module.nearest_snapshot_limit(product, requested) == expected


# -- seam-09 / DOC-03: one authoritative table -------------------------------


def test_the_data_client_reads_the_websocket_layer_tables(harness: Harness) -> None:
    """Regression for seam-09: the tables were maintained twice, in two forms."""
    assert data_module.BOOK_LEVELS is public_module.BOOK_LEVELS
    assert data_module.BOOK_INTERVALS_MS is public_module.BOOK_INTERVALS_MS


def test_config_book_intervals_are_the_union_of_the_authoritative_table() -> None:
    """Regression for DOC-03.

    ``config.ORDER_BOOK_UPDATE_INTERVALS_MS`` validates a configured value before
    the product is known, so it may only ever be the union of the per-product
    tables — never a different set.
    """
    assert tuple(sorted(ORDER_BOOK_UPDATE_INTERVALS_MS)) == public_module.ALL_BOOK_INTERVALS_MS


@pytest.mark.parametrize(
    ("product", "intervals"),
    [
        (GateioProductType.SPOT, (20, 100)),
        (GateioProductType.PERP, (20, 100)),
        (GateioProductType.INVERSE, (20, 100)),
        (GateioProductType.FUT, (100, 1000)),
        (GateioProductType.OPT, (100, 1000)),
    ],
)
def test_per_product_intervals_match_the_venue(
    product: GateioProductType,
    intervals: tuple[int, ...],
) -> None:
    """Spot and the perpetuals do **not** accept 1000 ms; it was withdrawn."""
    assert public_module.BOOK_INTERVALS_MS[product] == intervals
    assert public_module.book_interval_strs(product) == tuple(f"{ms}ms" for ms in intervals)


def test_configured_interval_is_clamped_per_product(harness: Harness) -> None:
    interval, level = harness.client._resolve_book_stream(GateioProductType.OPT, 100)
    assert interval == "100ms"
    assert level == 50  # options stream at most 50 levels


# -- md-07: bars on products without a window-close flag ---------------------


def _option_bar_type() -> BarType:
    return BarType.from_str(f"{OPTION_ID}-1-MINUTE-LAST-EXTERNAL")


def _candle(open_secs: int) -> dict[str, Any]:
    return {
        "t": str(open_secs),
        "o": "5.0",
        "h": "5.4",
        "l": "4.9",
        "c": "5.2",
        "v": "120",
        "n": f"1m_{OPTION_SYMBOL}",
    }


def test_closed_bucket_is_published_on_the_clock_without_a_window_flag(
    harness: Harness,
) -> None:
    """Regression for md-07.

    ``options.contract_candlesticks`` and delivery candles carry no ``w`` flag.
    The bar used to be held until a candle for a *newer* bucket arrived, which on
    an illiquid contract can be many intervals later or never.
    """
    client = harness.client
    bar_type = _option_bar_type()
    client._bar_types[(GateioProductType.OPT, f"1m_{OPTION_SYMBOL}")] = bar_type

    now_secs = client._clock.timestamp_ns() // 1_000_000_000
    closed_bucket = (now_secs // 60) * 60 - 600  # ten minutes in the past

    client._handle_ws_message(
        GateioProductType.OPT,
        {
            "channel": "options.contract_candlesticks",
            "event": "update",
            "result": [_candle(closed_bucket)],
        },
    )

    bars = harness.bars()
    assert len(bars) == 1, "a bucket closed ten minutes ago was never published"
    assert bars[0].ts_event == (closed_bucket + 60) * 1_000_000_000
    # ts_init reports when the bar was published, not when the candle arrived.
    assert bars[0].ts_init >= bars[0].ts_event
    assert bar_type not in client._bar_pending


def test_an_open_bucket_is_not_published_early(harness: Harness) -> None:
    client = harness.client
    bar_type = _option_bar_type()
    client._bar_types[(GateioProductType.OPT, f"1m_{OPTION_SYMBOL}")] = bar_type

    now_secs = client._clock.timestamp_ns() // 1_000_000_000
    current_bucket = (now_secs // 60) * 60

    client._handle_ws_message(
        GateioProductType.OPT,
        {
            "channel": "options.contract_candlesticks",
            "event": "update",
            "result": [_candle(current_bucket)],
        },
    )

    assert harness.bars() == []
    assert client._bar_pending[bar_type].open_secs == current_bucket


def test_a_newer_bucket_still_releases_the_previous_one(harness: Harness) -> None:
    """The original bucket-advance path must keep working."""
    client = harness.client
    bar_type = _option_bar_type()
    client._bar_types[(GateioProductType.OPT, f"1m_{OPTION_SYMBOL}")] = bar_type

    now_secs = client._clock.timestamp_ns() // 1_000_000_000
    current_bucket = (now_secs // 60) * 60

    client._handle_ws_message(
        GateioProductType.OPT,
        {
            "channel": "options.contract_candlesticks",
            "event": "update",
            "result": [_candle(current_bucket)],
        },
    )
    assert harness.bars() == []

    client._handle_ws_message(
        GateioProductType.OPT,
        {
            "channel": "options.contract_candlesticks",
            "event": "update",
            "result": [_candle(current_bucket + 60)],
        },
    )
    bars = harness.bars()
    assert len(bars) == 1
    assert bars[0].ts_event == (current_bucket + 60) * 1_000_000_000


def test_a_bar_is_published_once(harness: Harness) -> None:
    client = harness.client
    bar_type = _option_bar_type()
    client._bar_types[(GateioProductType.OPT, f"1m_{OPTION_SYMBOL}")] = bar_type

    now_secs = client._clock.timestamp_ns() // 1_000_000_000
    closed_bucket = (now_secs // 60) * 60 - 600

    for _ in range(3):
        client._handle_ws_message(
            GateioProductType.OPT,
            {
                "channel": "options.contract_candlesticks",
                "event": "update",
                "result": [_candle(closed_bucket)],
            },
        )
    assert len(harness.bars()) == 1


# -- md-05 complement: the client keeps a book when the socket is down --------


async def test_transient_subscribe_failure_keeps_the_local_book(harness: Harness) -> None:
    """The WebSocket client replays the channel, so the book must survive too."""
    client = harness.client

    class StubWs:
        def effective_depth(self, interval: str, level: int | None) -> int:
            return level or 100

        async def subscribe_order_book_update(self, symbol: str, interval: str, level: int) -> None:
            raise data_module.GateioError(0, "WS_NOT_CONNECTED", "reconnecting")

    client._ws_clients[GateioProductType.PERP] = StubWs()  # type: ignore[assignment]
    tracked: list[Any] = []
    client.create_task = lambda coro, **_: tracked.append(coro)  # type: ignore[method-assign]

    command = data_module.SubscribeOrderBook(
        instrument_id=PERP_ID,
        book_data_type=OrderBookDelta,
        book_type=BookType.L2_MBP,
        client_id=GATEIO_CLIENT_ID,
        venue=GATEIO_VENUE,
        command_id=UUID4(),
        ts_init=0,
        depth=100,
    )
    await client._subscribe_order_book_deltas(command)

    assert PERP_ID in client._books, "the book was discarded on a transient failure"
    for coro in tracked:
        coro.close()


# -- s2: the derivative data contract ----------------------------------------
#
# MarkPriceUpdate, IndexPriceUpdate and FundingRateUpdate are first-class
# NautilusTrader types, and everything below pins what the adapter is allowed to
# put in them: which products they exist for, on what scale a reference price is
# published, and where the next funding time comes from.

#: The BTC_USDT perpetual funding schedule: eight hours, in seconds.
FUNDING_INTERVAL_SECS = 28_800


def _perp_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "BTC_USDT",
        "quanto_multiplier": "0.0001",
        "order_price_round": "0.1",
        "mark_price_round": "0.01",  # the venue's mark grid is ten times finer
        "maker_fee_rate": "0.0002",
        "taker_fee_rate": "0.0005",
        "maintenance_rate": "0.003",
        "leverage_min": "1",
        "leverage_max": "100",
        "order_size_min": 1,
        "order_size_max": 1_000_000,
        "create_time": 1_700_000_000,
    }
    payload.update(overrides)
    return payload


def _install_perp(harness: Harness, **overrides: Any) -> Any:
    """Rebuild the BTC_USDT perpetual with ``overrides`` and register it."""
    instrument = parse_perpetual_instrument(_perp_payload(**overrides), GateioProductType.PERP)
    assert instrument is not None
    harness.client._instrument_provider.add(instrument)
    return instrument


def _install_option(harness: Harness, **overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "name": OPTION_SYMBOL,
        "underlying": "BTC_USDT",
        "is_call": True,
        "multiplier": "0.01",
        "strike_price": "70000",
        # The 598 BTC_USDT options quote orders in whole USDT and marks in 0.1.
        "order_price_round": "1",
        "mark_price_round": "0.1",
        "maker_fee_rate": "0.0003",
        "taker_fee_rate": "0.0003",
        "order_size_min": 1,
        "order_size_max": 100_000,
        "expiration_time": 1_785_000_000,
        "create_time": 1_700_000_000,
    }
    payload.update(overrides)
    instrument = parse_option_instrument(payload)
    assert instrument is not None
    harness.client._instrument_provider.add(instrument)
    return instrument


def _futures_ticker(ts_ms: int, **fields: Any) -> dict[str, Any]:
    return {
        "channel": "futures.tickers",
        "event": "update",
        "result": [{"contract": "BTC_USDT", "t": ts_ms, **fields}],
    }


def _options_ticker(**fields: Any) -> dict[str, Any]:
    """``options.contract_tickers`` keys the contract on ``name``, not ``contract``."""
    return {
        "channel": "options.contract_tickers",
        "event": "update",
        "result": [{"name": OPTION_SYMBOL, **fields}],
    }


def _marks(harness: Harness) -> list[MarkPriceUpdate]:
    return [item for item in harness.published if isinstance(item, MarkPriceUpdate)]


def _indexes(harness: Harness) -> list[IndexPriceUpdate]:
    return [item for item in harness.published if isinstance(item, IndexPriceUpdate)]


def _fundings(harness: Harness) -> list[FundingRateUpdate]:
    return [item for item in harness.published if isinstance(item, FundingRateUpdate)]


# -- next funding time -------------------------------------------------------


def test_a_stale_next_funding_time_is_rolled_onto_the_funding_grid(harness: Harness) -> None:
    """Regression: ``next_funding_ns`` used to be a timestamp in the past.

    ``funding_next_apply`` reaches this client only through the instrument
    reload task (60 minutes by default) while the ticker pushes about once a
    second, so for up to an hour after every settlement the cached field names a
    funding that has already been applied. A strategy computing
    ``next_funding_ns - clock.timestamp_ns()`` — the whole point of the field —
    got a negative time to funding.
    """
    ts_secs = 1_784_995_205  # five seconds after a settlement
    settled = 1_784_995_200  # the grid point that has just passed
    _install_perp(
        harness,
        funding_interval=FUNDING_INTERVAL_SECS,
        funding_next_apply=settled,
    )
    harness.client._ticker_subs[PERP_ID] = {"funding"}

    harness.client._handle_ws_message(
        GateioProductType.PERP,
        _futures_ticker(ts_secs * 1_000, funding_rate="0.000028"),
    )

    (update,) = _fundings(harness)
    assert update.ts_event == ts_secs * 1_000_000_000
    assert update.next_funding_ns == (settled + FUNDING_INTERVAL_SECS) * 1_000_000_000
    assert update.next_funding_ns > update.ts_event, "next funding was in the past"
    assert update.interval == FUNDING_INTERVAL_SECS // 60
    assert update.rate == Decimal("0.000028")


def test_a_long_stale_next_funding_time_rolls_to_the_first_grid_point_ahead(
    harness: Harness,
) -> None:
    """Three whole intervals of drift must land on the next settlement, not the next-but-three."""
    anchor = 1_784_995_200
    ts_secs = anchor + 3 * FUNDING_INTERVAL_SECS + 5
    _install_perp(
        harness,
        funding_interval=FUNDING_INTERVAL_SECS,
        funding_next_apply=anchor,
    )
    harness.client._ticker_subs[PERP_ID] = {"funding"}

    harness.client._handle_ws_message(
        GateioProductType.PERP,
        _futures_ticker(ts_secs * 1_000, funding_rate="0.0001"),
    )

    (update,) = _fundings(harness)
    expected = anchor + 4 * FUNDING_INTERVAL_SECS
    assert update.next_funding_ns == expected * 1_000_000_000
    assert expected - FUNDING_INTERVAL_SECS < ts_secs, "not the first grid point ahead"


def test_a_next_funding_time_still_ahead_is_published_verbatim(harness: Harness) -> None:
    """Rolling forward must not move a timestamp that is already correct."""
    ts_secs = 1_784_990_000
    next_apply = 1_784_995_200
    _install_perp(
        harness,
        funding_interval=FUNDING_INTERVAL_SECS,
        funding_next_apply=next_apply,
    )
    harness.client._ticker_subs[PERP_ID] = {"funding"}

    harness.client._handle_ws_message(
        GateioProductType.PERP,
        _futures_ticker(ts_secs * 1_000, funding_rate="0.0001"),
    )

    (update,) = _fundings(harness)
    assert update.next_funding_ns == next_apply * 1_000_000_000


def test_next_funding_time_is_omitted_when_the_grid_step_is_unknown(harness: Harness) -> None:
    """No interval means no way to roll a stale anchor forward.

    ``concepts/data/funding_rate_update.md`` asks for ``next_funding_ns`` "only
    when the venue publishes them", so ``None`` is the answer rather than a
    timestamp known to be wrong.
    """
    ts_secs = 1_784_995_205
    _install_perp(harness, funding_next_apply=1_784_995_200)  # no funding_interval
    harness.client._ticker_subs[PERP_ID] = {"funding"}

    harness.client._handle_ws_message(
        GateioProductType.PERP,
        _futures_ticker(ts_secs * 1_000, funding_rate="0.0001"),
    )

    (update,) = _fundings(harness)
    assert update.next_funding_ns is None
    assert update.interval is None


# -- reference price scale ---------------------------------------------------


def test_a_mark_price_keeps_the_venue_scale_instead_of_the_order_tick(
    harness: Harness,
) -> None:
    """Regression: a mark price used to be quantised onto the order tick.

    Gate.io publishes ``order_price_round`` and ``mark_price_round`` as two
    independent minimum units — 0.1 and 0.01 on the BTC_USDT perpetual — and a
    mark price is not an order price. Rounding it onto the order grid publishes a
    number the venue never sent.
    """
    _install_perp(harness)
    harness.client._ticker_subs[PERP_ID] = {"mark"}

    harness.client._handle_ws_message(
        GateioProductType.PERP,
        _futures_ticker(1_784_995_205_000, mark_price="64000.05"),
    )

    (update,) = _marks(harness)
    assert str(update.value) == "64000.05"
    assert update.value.precision == 2


def test_a_mark_price_scale_does_not_wobble_between_updates(harness: Harness) -> None:
    """``mark_price_round`` is a floor, so a round number keeps the same scale."""
    _install_perp(harness)
    harness.client._ticker_subs[PERP_ID] = {"mark"}

    harness.client._handle_ws_message(
        GateioProductType.PERP,
        _futures_ticker(1_784_995_205_000, mark_price="64000"),
    )

    (update,) = _marks(harness)
    assert update.value.precision == 2
    assert str(update.value) == "64000.00"


def test_an_index_price_keeps_the_venue_scale(harness: Harness) -> None:
    """The venue states no minimum unit for the index, so its own scale is used."""
    _install_perp(harness)
    harness.client._ticker_subs[PERP_ID] = {"index"}

    harness.client._handle_ws_message(
        GateioProductType.PERP,
        _futures_ticker(1_784_995_205_000, index_price="63999.123"),
    )

    (update,) = _indexes(harness)
    assert str(update.value) == "63999.123"


def test_an_unparseable_reference_price_is_not_published_as_zero(harness: Harness) -> None:
    """Regression: an unusable field used to take the rest of the message with it.

    Asserting only that no mark or index is published proves nothing — the old
    code published neither either. What it did instead was raise out of
    ``make_price``, and since the ticker carries all three derivative types in one
    message, the funding rate beside the bad field was lost with it. So the damage
    to assert is the funding update, not the absent price.
    """
    _install_perp(harness, funding_interval=28_800, funding_next_apply=1_784_995_200)
    harness.client._ticker_subs[PERP_ID] = {"mark", "index", "funding"}

    harness.client._handle_ws_message(
        GateioProductType.PERP,
        _futures_ticker(
            1_784_995_205_000,
            mark_price="",
            index_price="n/a",
            funding_rate="0.0001",
        ),
    )

    assert _marks(harness) == []
    assert _indexes(harness) == []
    (funding,) = _fundings(harness)
    assert funding.rate == Decimal("0.0001")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("nan", None),
        ("abc", None),
    ],
)
def test_venue_price_reports_absence_rather_than_a_price(value: Any, expected: Any) -> None:
    assert data_module.venue_price(value) is expected


# -- options: mark and index -------------------------------------------------


def test_mark_and_index_prices_are_published_for_options(harness: Harness) -> None:
    """Regression: options were refused, so an option position had no mark price.

    Gate.io publishes both per contract on ``options.contract_tickers``, keyed on
    ``name``. Without them a node configured with ``use_mark_prices=True`` has no
    PnL basis on exactly the instrument class where mark and last diverge most.
    """
    _install_option(harness)
    harness.client._ticker_subs[OPTION_ID] = {"mark", "index"}

    harness.client._handle_ws_message(
        GateioProductType.OPT,
        _options_ticker(mark_price="5797.7", index_price="64123.45"),
    )

    (mark,) = _marks(harness)
    (index,) = _indexes(harness)
    assert mark.instrument_id == OPTION_ID
    # The order tick is 1 and the mark tick 0.1: on the order grid this would
    # have been published as 5798.
    assert str(mark.value) == "5797.7"
    assert str(index.value) == "64123.45"


async def test_an_option_may_subscribe_to_mark_and_index_prices(harness: Harness) -> None:
    client = harness.client

    class StubWs:
        def __init__(self) -> None:
            self.subscribed: list[str] = []

        async def subscribe_tickers(self, symbol: str) -> None:
            self.subscribed.append(symbol)

    ws = StubWs()
    client._ws_clients[GateioProductType.OPT] = ws  # type: ignore[assignment]

    await client._subscribe_mark_prices(
        data_module.SubscribeMarkPrices(OPTION_ID, GATEIO_CLIENT_ID, GATEIO_VENUE, UUID4(), 0),
    )
    await client._subscribe_index_prices(
        data_module.SubscribeIndexPrices(OPTION_ID, GATEIO_CLIENT_ID, GATEIO_VENUE, UUID4(), 0),
    )

    # One venue subscription serves both; the client reference-counts them.
    assert ws.subscribed == [OPTION_SYMBOL]
    assert client._ticker_subs[OPTION_ID] == {"mark", "index"}


async def test_an_option_may_not_subscribe_to_funding_rates(harness: Harness) -> None:
    """Options pay no funding, so the subscription is refused rather than left silent."""
    client = harness.client

    class StubWs:
        def __init__(self) -> None:
            self.subscribed: list[str] = []

        async def subscribe_tickers(self, symbol: str) -> None:
            self.subscribed.append(symbol)

    ws = StubWs()
    client._ws_clients[GateioProductType.OPT] = ws  # type: ignore[assignment]

    await client._subscribe_funding_rates(
        data_module.SubscribeFundingRates(OPTION_ID, GATEIO_CLIENT_ID, GATEIO_VENUE, UUID4(), 0),
    )

    assert ws.subscribed == []
    assert not client._ticker_subs.get(OPTION_ID)


async def test_spot_may_not_subscribe_to_any_of_the_three(harness: Harness) -> None:
    client = harness.client

    class StubWs:
        def __init__(self) -> None:
            self.subscribed: list[str] = []

        async def subscribe_tickers(self, symbol: str) -> None:
            self.subscribed.append(symbol)

    ws = StubWs()
    client._ws_clients[GateioProductType.SPOT] = ws  # type: ignore[assignment]

    await client._subscribe_mark_prices(
        data_module.SubscribeMarkPrices(SPOT_ID, GATEIO_CLIENT_ID, GATEIO_VENUE, UUID4(), 0),
    )

    assert ws.subscribed == []
    assert not client._ticker_subs.get(SPOT_ID)


def test_the_ticker_channel_name_comes_from_the_transport() -> None:
    """Routing and subscribing must read the same name, or one of them is dead code."""
    assert public_module.tickers_channel(GateioProductType.OPT) == "options.contract_tickers"
    assert public_module.tickers_channel(GateioProductType.PERP) == "futures.tickers"
    assert data_module.tickers_channel is public_module.tickers_channel


# -- historical requests -----------------------------------------------------


def _bar_request(bar_type: BarType, limit: int = 0) -> Any:
    return data_module.RequestBars(
        bar_type=bar_type,
        start=None,
        end=None,
        limit=limit,
        client_id=GATEIO_CLIENT_ID,
        venue=GATEIO_VENUE,
        callback=lambda data: None,
        request_id=UUID4(),
        ts_init=0,
        params=None,
    )


async def test_one_malformed_candle_does_not_abort_the_whole_bar_request(
    harness: Harness,
) -> None:
    """Regression: a single bad row used to make the entire response disappear.

    NautilusTrader enforces the OHLC invariants inside the ``Bar`` constructor,
    which used to sit outside the parse guard. In ``_request_bars`` nothing
    caught the ``ValueError``, so it escaped the request coroutine, the
    ``DataResponse`` was never sent, and a strategy following the documented
    ``request_bars(..., callback=lambda _: self.subscribe_bars(bar_type))``
    pattern waited forever on one exception line in the log.
    """
    client = harness.client
    bar_type = _option_bar_type()
    responses: list[list[Bar]] = []
    client._handle_bars = lambda bt, bars, *args: responses.append(bars)  # type: ignore[method-assign]

    now_secs = client._clock.timestamp_ns() // 1_000_000_000
    first = (now_secs // 60) * 60 - 600
    broken = dict(_candle(first + 60), h="4.0")  # high below open: illegal OHLC
    rows = [
        (first, _candle(first)),
        (first + 60, broken),
        (first + 120, _candle(first + 120)),
    ]

    async def _fetch(*args: Any, **kwargs: Any) -> list[tuple[int, dict[str, Any]]]:
        return rows

    client._fetch_candles = _fetch  # type: ignore[method-assign]

    await client._request_bars(_bar_request(bar_type))

    assert responses, "the request produced no response at all"
    (bars,) = responses
    assert [bar.ts_event for bar in bars] == [
        (first + 60) * 1_000_000_000,
        (first + 180) * 1_000_000_000,
    ]
    assert client.metrics()["candles_dropped"][str(bar_type)] == 1


async def test_a_malformed_candle_in_the_live_stream_is_still_only_counted(
    harness: Harness,
) -> None:
    """The live path already dropped the row; it must keep doing so, and say so."""
    client = harness.client
    bar_type = _option_bar_type()
    client._bar_types[(GateioProductType.OPT, f"1m_{OPTION_SYMBOL}")] = bar_type

    now_secs = client._clock.timestamp_ns() // 1_000_000_000
    closed_bucket = (now_secs // 60) * 60 - 600

    client._handle_ws_message(
        GateioProductType.OPT,
        {
            "channel": "options.contract_candlesticks",
            "event": "update",
            "result": [dict(_candle(closed_bucket), l="9.9")],  # low above close
        },
    )

    assert harness.bars() == []
    assert client.metrics()["candles_dropped"][str(bar_type)] == 1


class StubFundingHttp:
    """Records the funding-rate history call and replays a canned response."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def funding_rate(self, contract: str, limit: int | None = None) -> list[dict[str, Any]]:
        self.calls.append({"contract": contract, "limit": limit})
        return self.rows


def _funding_request(instrument_id: InstrumentId, limit: int = 0) -> Any:
    return data_module.RequestFundingRates(
        instrument_id=instrument_id,
        start=None,
        end=None,
        limit=limit,
        client_id=GATEIO_CLIENT_ID,
        venue=GATEIO_VENUE,
        callback=lambda data: None,
        request_id=UUID4(),
        ts_init=0,
        params=None,
    )


async def test_funding_rate_history_is_requested_and_published(harness: Harness) -> None:
    """Regression: the hook was unimplemented while its REST wrapper had no callers.

    Five in-tree adapters implement ``_request_funding_rates``; the historical
    series is what a funding-carry backtest is built from.
    """
    client = harness.client
    _install_perp(harness, funding_interval=FUNDING_INTERVAL_SECS)
    http = StubFundingHttp(
        [
            {"r": "0.000028", "t": 1_784_966_401},
            {"r": "0.000023", "t": 1_784_937_601},
        ],
    )
    client._futures_http[GateioProductType.PERP] = http  # type: ignore[assignment]
    responses: list[list[FundingRateUpdate]] = []
    client._handle_funding_rates = (  # type: ignore[method-assign]
        lambda iid, rates, *args: responses.append(rates)
    )

    await client._request_funding_rates(_funding_request(PERP_ID))

    assert http.calls == [{"contract": "BTC_USDT", "limit": 1000}]
    (rates,) = responses
    # Gate.io answers newest-first; the response is oldest-first.
    assert [item.ts_event for item in rates] == [
        1_784_937_601 * 1_000_000_000,
        1_784_966_401 * 1_000_000_000,
    ]
    assert [item.rate for item in rates] == [Decimal("0.000023"), Decimal("0.000028")]
    assert rates[0].interval == FUNDING_INTERVAL_SECS // 60
    # The endpoint publishes {t, r} and nothing about the next application.
    assert all(item.next_funding_ns is None for item in rates)


async def test_funding_rate_history_is_refused_for_a_product_without_funding(
    harness: Harness,
) -> None:
    client = harness.client
    http = StubFundingHttp([])
    client._futures_http[GateioProductType.PERP] = http  # type: ignore[assignment]
    responses: list[Any] = []
    client._handle_funding_rates = (  # type: ignore[method-assign]
        lambda iid, rates, *args: responses.append(rates)
    )

    await client._request_funding_rates(_funding_request(OPTION_ID))

    assert http.calls == []
    assert responses == []


# -- s4: the client uses the platform's task machinery, not its own ----------
#
# `LiveMarketDataClient.__init__` creates `self._tasks` as a WeakSet that
# `create_task` populates and `cancel_pending_tasks` drains
# (live/data_client.py:375, :478, :1149). The client replaced it with a plain
# set, so every completed subscribe, unsubscribe and request task stayed
# reachable for the lifetime of the node, and `_disconnect` cleared the
# collection after cancelling, leaving the platform's bounded shutdown
# (live/cancellation.py) with nothing to await.


class _RecordingTransport:
    """A stand-in for the shared HTTP transport that records its teardown."""

    def __init__(self, journal: list[str]) -> None:
        self._journal = journal
        self.is_accepting = True

    def stop_accepting(self) -> None:
        self.is_accepting = False
        self._journal.append("gate")

    async def close(self) -> None:
        self._journal.append("http")


class _RecordingWebSocket:
    """A stand-in for a product WebSocket that records its teardown."""

    def __init__(self, journal: list[str]) -> None:
        self._journal = journal

    async def disconnect(self) -> None:
        self._journal.append("ws")


def _client_on_the_running_loop(http_client: Any) -> GateioDataClient:
    """Build a client bound to the loop the test is running on.

    The shared ``harness`` fixture builds its client on a fresh, never-started
    loop, which is right for the parsing tests but cannot run a task.
    """
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    provider = StubProvider()
    for instrument in build_instruments():
        provider.add(instrument)
    return GateioDataClient(
        loop=asyncio.get_running_loop(),
        client_id=GATEIO_CLIENT_ID,
        msgbus=msgbus,
        cache=Cache(),
        clock=clock,
        instrument_provider=provider,
        http_client=http_client,
        config=GateioDataClientConfig(products=(GateioProductType.PERP,)),
    )


async def test_the_client_keeps_the_platforms_task_registry() -> None:
    """The registry must stay the base class's WeakSet."""
    from weakref import WeakSet

    client = _client_on_the_running_loop(_RecordingTransport([]))
    assert isinstance(client._tasks, WeakSet), (
        "the client replaced LiveMarketDataClient._tasks with its own collection"
    )


async def test_completed_background_tasks_do_not_accumulate() -> None:
    """A long-running node must not retain every finished task.

    Each subscribe, unsubscribe and request the data engine issues becomes a
    task in this registry. Holding a plain set meant a session that resubscribed
    or requested bars on a schedule grew one entry per call, forever.
    """
    import gc

    client = _client_on_the_running_loop(_RecordingTransport([]))
    assert len(client._tasks) == 0

    for index in range(3):
        client.create_task(asyncio.sleep(0), log_msg=f"probe-{index}")
    await asyncio.sleep(0.01)
    gc.collect()

    assert len(client._tasks) == 0, "finished tasks are still held by the client"


async def test_disconnect_settles_its_tasks_before_releasing_the_transports() -> None:
    """Shutdown closes the gate first and releases the transport last.

    Three properties in one sequence. The gate goes first, before anything is
    awaited, because that is the only step that binds work this snapshot cannot
    see. The background task is then cancelled *and awaited* through the
    platform's bounded teardown rather than merely told to cancel. The shared
    HTTP transport - reference counted, so this release is what closes the pool
    for both clients - goes last, after the sockets.

    The order of the last two no longer carries the guarantee, and the comment
    that used to claim it did was false besides: closing the pool under a
    request already on the wire does not produce ``CLIENT_CLOSED``, it produces
    a bare ``RuntimeError`` from ``httpx`` (measured on 0.28.1).
    """
    journal: list[str] = []
    client = _client_on_the_running_loop(_RecordingTransport(journal))
    client._ws_clients[GateioProductType.PERP] = _RecordingWebSocket(journal)  # type: ignore[assignment]

    running = asyncio.Event()

    async def _background() -> None:
        running.set()
        try:
            await asyncio.sleep(3600)
        finally:
            journal.append("task")

    task = client.create_task(_background(), log_msg="probe")
    await asyncio.wait_for(running.wait(), timeout=1.0)

    await client._disconnect()

    assert task.done(), "_disconnect returned while its background task was still pending"
    assert journal == ["gate", "task", "ws", "http"], f"shutdown ran out of order: {journal}"


async def test_disconnect_leaves_the_platforms_shutdown_nothing_to_do() -> None:
    """`cancel_pending_tasks` runs again after `_disconnect`; it must be a no-op.

    The base `disconnect()` calls `_disconnect()` and then
    `cancel_pending_tasks()` (live/data_client.py:550-552). Clearing the
    registry inside `_disconnect` used to make that second call meaningless
    whether or not anything had actually finished.
    """
    client = _client_on_the_running_loop(_RecordingTransport([]))
    running = asyncio.Event()

    async def _background() -> None:
        running.set()
        await asyncio.sleep(3600)

    task = client.create_task(_background(), log_msg="probe")
    await asyncio.wait_for(running.wait(), timeout=1.0)

    await client._disconnect()

    # The registry is the platform's to manage. Emptying it by hand is what made
    # the second call meaningless: it reported a clean shutdown for tasks it had
    # simply stopped tracking.
    assert task in client._tasks, "_disconnect emptied the platform's task registry"
    assert task.done()

    await client.cancel_pending_tasks()
    assert [pending for pending in client._tasks if not pending.done()] == []


# -- teardown: nothing reaches the venue once `_disconnect` has begun ---------
#
# The data client's teardown had two holes of its own. It released the shared
# transport *last*, so anything still on the wire met a closed socket pool and
# raised a bare `RuntimeError` rather than the `CLIENT_CLOSED` its own comment
# promised; and the background work it has to stop - the instrument reload -
# reaches the venue through the *shared* instrument provider, not through this
# client's namespaces, so nothing scoped to this client could have stopped it.
#
# The gate on the transport covers both, and its position (the first statement
# of `_disconnect`, before anything is awaited) is what makes the order of
# everything after it immaterial. Every "nothing was sent" assertion carries a
# positive control: the same spy must record the same call while the node runs.


class _DataWireSpy:
    """A real ``GateioHttpClient`` whose every send is recorded."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.transport = GateioHttpClient(max_retries=1)
        self.transport._client = httpx.AsyncClient(
            transport=httpx.MockTransport(self._handle),
            base_url=GATEIO_HTTP_MAINNET,
        )
        self.transport._limiter = RateLimiter(max_per_second=1_000.0)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(f"{request.method} {request.url.raw_path.decode()}")
        return httpx.Response(
            200,
            json={"id": 10, "current": 1, "update": 1, "bids": [], "asks": []},
        )

    def since(self, mark: int) -> list[str]:
        return self.sent[mark:]


def _data_client_with_a_wire_spy() -> tuple[GateioDataClient, _DataWireSpy]:
    spy = _DataWireSpy()
    client = _client_on_the_running_loop(spy.transport.acquire())
    client._spot_http = GateioSpotHttpAPI(spy.transport)
    client._options_http = GateioOptionsHttpAPI(spy.transport)
    client._futures_http = {
        GateioProductType.PERP: GateioFuturesHttpAPI(spy.transport, settle="usdt"),
        GateioProductType.INVERSE: GateioFuturesHttpAPI(spy.transport, settle="btc"),
        GateioProductType.FUT: GateioFuturesHttpAPI(spy.transport, settle="usdt", delivery=True),
    }
    client._books[PERP_ID] = GateioOrderBook("BTC_USDT")
    return client, spy


async def _settle_tasks(client: GateioDataClient, rounds: int = 400) -> None:
    """Let every task the client scheduled run to completion."""
    for _ in range(rounds):
        await asyncio.sleep(0.01)
        if not [task for task in client._tasks if not task.done()]:
            return


class _SlowSocket:
    """A socket whose close takes as long as the platform allows it to.

    ``cancel_tasks_with_timeout`` gives a socket's receive loop up to 5 s, and
    the sockets are closed one after another, so this window is wide in a real
    node. It is where a last frame arrives.
    """

    def __init__(self, delay: float = 0.05, during: Any = None) -> None:
        self.delay = delay
        self.during = during
        self.disconnected = False

    async def disconnect(self) -> None:
        if self.during is not None:
            self.during()
        await asyncio.sleep(self.delay)
        self.disconnected = True


async def test_a_frame_arriving_during_teardown_cannot_resnapshot_a_book() -> None:
    """A reconnect during teardown schedules a REST snapshot per subscribed book.

    The receive loop lives in the WebSocket client's own registry, so the task
    it creates here is born *after* ``cancel_pending_tasks`` took its snapshot,
    against a transport that used to still be open.
    """
    client, spy = _data_client_with_a_wire_spy()

    # Positive control: the same resync reaches the same spy while running.
    await client._handle_ws_reconnect(GateioProductType.PERP)
    await _settle_tasks(client)
    assert any("order_book" in line for line in spy.sent), (
        f"the resnapshot never reached the spy: {spy.sent}"
    )
    mark = len(spy.sent)

    socket = _SlowSocket(
        delay=0.05,
        during=lambda: client.create_task(
            client._handle_ws_reconnect(GateioProductType.PERP),
            log_msg="late reconnect",
        ),
    )
    client._ws_clients[GateioProductType.PERP] = socket  # type: ignore[assignment]

    await client._disconnect()
    await _settle_tasks(client)

    assert spy.since(mark) == [], "a request left the process after `_disconnect` had begun"


async def test_a_refused_snapshot_does_not_re_arm_itself_during_teardown() -> None:
    """A retry armed after the snapshot is an orphan nothing will ever cancel.

    ``cancel_pending_tasks`` has already taken its snapshot by then, so each
    stopping node would leave one live task per unsynchronised book behind.
    """
    client, spy = _data_client_with_a_wire_spy()

    # Positive control: while the node runs, a failed snapshot *is* re-armed.
    armed: list[InstrumentId] = []
    real_create_task = client.create_task

    def _record(coro: Any, **kwargs: Any) -> Any:
        armed.append(kwargs.get("log_msg", ""))
        return real_create_task(coro, **kwargs)

    client.create_task = _record  # type: ignore[method-assign]
    client._schedule_book_retry(PERP_ID)
    assert any("retry" in str(entry) for entry in armed), "the retry was never armed at all"
    await client.cancel_pending_tasks()
    armed.clear()

    spy.transport.stop_accepting()
    client._schedule_book_retry(PERP_ID)

    assert armed == [], "a retry task was armed after the transport stopped accepting"


async def test_the_instrument_reload_survives_a_venue_error_and_keeps_reloading() -> None:
    """The other half of the exit above, and the one with damage behind it.

    Leaving the loop is right for ``CLIENT_CLOSED`` and wrong for anything else:
    a single transient 500 from a listing reload would otherwise stop the reload
    for the life of the node, and instruments would silently go stale — no
    expiries, no new listings, no status changes — while the node keeps trading
    on what it last saw. ``main`` could not get this wrong because it had no
    early exit at all; the exit is new, so the discrimination has to be pinned.
    """
    client, spy = _data_client_with_a_wire_spy()

    turns = 0

    async def _initialize(reload: bool = False) -> None:
        nonlocal turns
        turns += 1
        await spy.transport.get("/spot/currency_pairs")
        if turns == 1:
            raise data_module.GateioError(500, "SERVER_ERROR", "the venue had a bad moment")

    client._instrument_provider.initialize = _initialize  # type: ignore[method-assign]
    client._send_all_instruments_to_data_engine = lambda: None  # type: ignore[method-assign]

    task = client.create_task(client._update_instruments(0.05 / 60), log_msg="update_instruments")
    client._update_instruments_task = task
    try:
        for _ in range(200):
            if turns >= 3:
                break
            await asyncio.sleep(0.01)

        assert turns >= 3, (
            f"the reload stopped after a venue error instead of surviving it: {turns} turn(s)"
        )
        assert not task.done(), "a transient venue error ended the reload loop"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_the_instrument_reload_leaves_its_loop_when_the_gate_shuts() -> None:
    """The reload reaches the venue through the *shared* provider.

    That is why the gate lives on the transport rather than on the client: a
    gate scoped to this client would not cover this loop at all. It must also
    stop rather than log, or a node with a five-second socket teardown prints
    one error per interval while it is shutting down.
    """
    client, spy = _data_client_with_a_wire_spy()

    async def _initialize(reload: bool = False) -> None:
        # What the shared provider does: it reaches the venue over the same
        # transport, through namespaces this client never sees.
        await spy.transport.get("/spot/currency_pairs")

    client._instrument_provider.initialize = _initialize  # type: ignore[method-assign]
    client._send_all_instruments_to_data_engine = lambda: None  # type: ignore[method-assign]

    # The loop's own cadence is minutes; it is driven here at 0.05 s so the test
    # observes several turns rather than one.
    task = client.create_task(client._update_instruments(0.05 / 60), log_msg="update_instruments")
    client._update_instruments_task = task
    try:
        # Positive control: the reload reaches the spy while the node runs.
        for _ in range(200):
            if len(spy.sent) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(spy.sent) >= 2, f"the reload never reached the spy: {spy.sent}"
        mark = len(spy.sent)

        spy.transport.stop_accepting()

        # It leaves the loop by itself; nothing cancels it here.
        await asyncio.wait_for(asyncio.shield(task), timeout=3.0)

        assert task.done()
        assert spy.since(mark) == []
    finally:
        task.cancel()


async def test_a_connect_still_inserting_sockets_does_not_abort_teardown() -> None:
    """``_connect`` inserts each socket into the dict before connecting it.

    A teardown that starts while it is in flight iterates a dict that grows
    underneath it: ``RuntimeError: dictionary changed size during iteration``
    escapes the per-socket ``except`` and flies out of ``_disconnect``, and the
    platform's ``_disconnect_with_cleanup`` has no ``try`` - so
    ``cancel_pending_tasks()`` and ``_set_connected(False)`` never run and the
    shared transport is never released.
    """
    client, spy = _data_client_with_a_wire_spy()
    late = _SlowSocket(delay=0.0)
    first = _SlowSocket(
        delay=0.05,
        during=lambda: client._ws_clients.__setitem__(GateioProductType.SPOT, late),
    )
    client._ws_clients[GateioProductType.PERP] = first  # type: ignore[assignment]
    owners = spy.transport.owner_count

    await client._disconnect()

    assert spy.transport.owner_count == owners - 1, "the shared transport was never released"
    assert first.disconnected and late.disconnected, "a socket was left open"
    assert client._ws_clients == {}


async def test_a_cancelled_teardown_still_releases_the_transport() -> None:
    """``CancelledError`` is not an ``Exception``; only ``finally`` catches it.

    The node's disconnect budget is bounded, so the teardown task can be
    cancelled from outside. Releasing the transport in the body rather than in
    ``finally`` would pin the shared pool open for the whole process.
    """
    client, spy = _data_client_with_a_wire_spy()

    class _Cancelled:
        async def disconnect(self) -> None:
            raise asyncio.CancelledError

    client._ws_clients[GateioProductType.PERP] = _Cancelled()  # type: ignore[assignment]
    owners = spy.transport.owner_count

    with pytest.raises(asyncio.CancelledError):
        await client._disconnect()

    assert spy.transport.owner_count == owners - 1, "a cancelled teardown leaked the transport"
