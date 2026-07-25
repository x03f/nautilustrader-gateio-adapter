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

import pytest
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.data.messages import RequestOrderBookSnapshot
from nautilus_trader.model.data import (
    Bar,
    BarType,
    OrderBookDelta,
    OrderBookDeltas,
    QuoteTick,
    TradeTick,
)
from nautilus_trader.model.enums import BookAction, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, TraderId

from nautilus_gateio import data as data_module
from nautilus_gateio.books import GateioOrderBook
from nautilus_gateio.common.constants import GATEIO_CLIENT_ID, GATEIO_VENUE
from nautilus_gateio.common.enums import GateioProductType
from nautilus_gateio.common.errors import GateioClientError
from nautilus_gateio.config import ORDER_BOOK_UPDATE_INTERVALS_MS, GateioDataClientConfig
from nautilus_gateio.data import GateioDataClient, venue_quantity
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


class Harness:
    """A constructed data client plus everything the tests published through it."""

    def __init__(self, client: GateioDataClient) -> None:
        self.client = client
        self.published: list[Any] = []
        client._handle_data = self.published.append  # type: ignore[method-assign]

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
    client._track = lambda coro, log_msg: scheduled.append(coro)  # type: ignore[method-assign]

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
    client._track = lambda coro, log_msg: tracked.append(coro)  # type: ignore[method-assign]

    from nautilus_trader.model.enums import BookType

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
