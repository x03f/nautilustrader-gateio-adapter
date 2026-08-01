"""Regression tests for ``OrderBookDepth10`` on :class:`GateioDataClient`.

The depth path reads Gate.io's periodic ``*.order_book`` snapshot channel, which
is a different venue channel from the incremental stream the delta path uses.
Everything here runs offline: the transport is a real
:class:`GateioPublicWebSocket` whose socket client is replaced by a recorder, so
the venue payloads under assertion are the ones the package would really send.

Before this feature existed, ``subscribe_order_book_depth`` recorded the
subscription and then raised ``NotImplementedError`` inside the task the base
class creates, which the platform logs once and never retries. Every subscribe
test below therefore goes through the *public* method: an assertion on
``subscribed_order_book_depth()`` would have passed on the unimplemented tree
and proved nothing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.data.messages import SubscribeOptionGreeks
from nautilus_trader.model.data import OrderBookDelta, OrderBookDepth10
from nautilus_trader.model.enums import BookType, OrderSide, RecordFlag
from nautilus_trader.model.identifiers import InstrumentId, TraderId

from gateio_nt import data as data_module
from gateio_nt.common.constants import GATEIO_CLIENT_ID, GATEIO_VENUE
from gateio_nt.common.enums import GateioProductType
from gateio_nt.config import GateioDataClientConfig
from gateio_nt.data import DEPTH10_LEVELS, GateioDataClient
from gateio_nt.websocket.public import GateioPublicWebSocket
from tests.test_data_client import (
    OPTION_ID,
    OPTION_SYMBOL,
    PERP_ID,
    SPOT_ID,
    Harness,
    StubProvider,
    build_instruments,
)


def build_harness() -> Harness:
    """A data client over the three products the shared instruments cover.

    Defined here rather than imported as a fixture: importing a fixture by name
    and then using that same name as a test parameter is a redefinition every
    linter reports, and the alternative — a shared name nobody can see the body
    of — is worse for a suite whose whole point is that the reader can check the
    claim.
    """
    clock = LiveClock()
    provider: InstrumentProvider = StubProvider()
    for instrument in build_instruments():
        provider.add(instrument)
    client = GateioDataClient(
        loop=asyncio.new_event_loop(),
        client_id=GATEIO_CLIENT_ID,
        msgbus=MessageBus(trader_id=TraderId("TESTER-000"), clock=clock),
        cache=Cache(),
        clock=clock,
        instrument_provider=provider,
        http_client=object(),
        config=GateioDataClientConfig(
            products=(GateioProductType.SPOT, GateioProductType.PERP, GateioProductType.OPT),
        ),
    )
    return Harness(client)


@pytest.fixture
def harness() -> Harness:
    return build_harness()


class RecordingTransport:
    """Stands in for the socket client behind a real ``GateioPublicWebSocket``."""

    def __init__(self) -> None:
        self.subscribed: list[tuple[str, list[str]]] = []
        self.unsubscribed: list[tuple[str, list[str]]] = []

    async def subscribe(self, channel: str, payload: list[str]) -> dict[str, Any]:
        self.subscribed.append((channel, list(payload)))
        return {}

    async def unsubscribe(self, channel: str, payload: list[str]) -> dict[str, Any]:
        self.unsubscribed.append((channel, list(payload)))
        return {}

    def stats(self) -> dict[str, Any]:
        return {"reconnects": 0}


def attach_ws(harness: Harness, product: GateioProductType) -> RecordingTransport:
    """Give ``product`` a real public WebSocket wrapper over a recording transport."""
    ws = GateioPublicWebSocket(product=product, handler=lambda msg: None)
    transport = RecordingTransport()
    ws.client = transport  # type: ignore[assignment]
    harness.client._ws_clients[product] = ws
    return transport


def subscribe_command(
    instrument_id: InstrumentId,
    depth: int = 10,
    params: dict[str, Any] | None = None,
    book_type: BookType = BookType.L2_MBP,
) -> Any:
    return data_module.SubscribeOrderBook(
        instrument_id=instrument_id,
        book_data_type=OrderBookDepth10,
        book_type=book_type,
        client_id=GATEIO_CLIENT_ID,
        venue=GATEIO_VENUE,
        command_id=UUID4(),
        ts_init=0,
        depth=depth,
        params=params,
    )


def unsubscribe_command(instrument_id: InstrumentId) -> Any:
    return data_module.UnsubscribeOrderBook(
        instrument_id=instrument_id,
        book_data_type=OrderBookDepth10,
        client_id=GATEIO_CLIENT_ID,
        venue=GATEIO_VENUE,
        command_id=UUID4(),
        ts_init=0,
    )


async def subscribe_depth(harness: Harness, command: Any) -> None:
    """Drive the public entry point, exactly as ``DataEngine`` does."""
    harness.client._loop = asyncio.get_running_loop()
    harness.client.subscribe_order_book_depth(command)
    for _ in range(4):
        await asyncio.sleep(0)


def spot_snapshot(
    last_update_id: int,
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
    ts_ms: int = 1_700_000_000_100,
) -> dict[str, Any]:
    """``spot.order_book``: array levels and a ``lastUpdateId`` sequence."""
    return {
        "channel": "spot.order_book",
        "event": "update",
        "result": {
            "t": ts_ms,
            "lastUpdateId": last_update_id,
            "s": "BTC_USDT",
            "l": "10",
            "bids": [[price, size] for price, size in bids],
            "asks": [[price, size] for price, size in asks],
        },
    }


def contract_snapshot(
    channel: str,
    symbol: str,
    book_id: int,
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
) -> dict[str, Any]:
    """``futures``/``options.order_book``: object levels and an ``id`` sequence."""
    return {
        "channel": channel,
        "event": "all",
        "result": {
            "t": 1_700_000_000_100,
            "id": book_id,
            "contract": symbol,
            "bids": [{"p": price, "s": size} for price, size in bids],
            "asks": [{"p": price, "s": size} for price, size in asks],
        },
    }


def depths(harness: Harness) -> list[OrderBookDepth10]:
    return [item for item in harness.published if isinstance(item, OrderBookDepth10)]


def real_levels(orders: list[Any]) -> list[Any]:
    """Drop the ``NULL_ORDER`` padding the type requires on a short side.

    Identity, not equality: ``BookOrder.__eq__`` compares ``order_id`` alone, so
    every aggregated level this adapter builds (all of them ``order_id=0``)
    compares equal to ``NULL_ORDER``. A padding filter written as
    ``order != NULL_ORDER`` silently returns an empty list.
    """
    return [order for order in orders if order.side != OrderSide.NO_ORDER_SIDE]


def level_tuples(orders: list[Any]) -> list[tuple[float, float]]:
    """Compare levels by what they say, for the same ``__eq__`` reason."""
    return [(order.price.as_double(), order.size.as_double()) for order in real_levels(orders)]


# -- the venue subscription --------------------------------------------------


async def test_spot_depth_subscribes_the_snapshot_channel_at_ten_levels(
    harness: Harness,
) -> None:
    """Fails on the pre-change tree: the base coroutine raises instead."""
    transport = attach_ws(harness, GateioProductType.SPOT)

    await subscribe_depth(harness, subscribe_command(SPOT_ID))

    assert transport.subscribed == [("spot.order_book", ["BTC_USDT", "10", "100ms"])]
    assert harness.client._depth_streams[SPOT_ID] == (10, "100ms")


async def test_contract_products_take_the_push_on_change_interval(harness: Harness) -> None:
    """Every product but spot accepts the single documented interval ``"0"``."""
    perp = attach_ws(harness, GateioProductType.PERP)
    option = attach_ws(harness, GateioProductType.OPT)

    await subscribe_depth(harness, subscribe_command(PERP_ID))
    await subscribe_depth(harness, subscribe_command(OPTION_ID))

    assert perp.subscribed == [("futures.order_book", ["BTC_USDT", "10", "0"])]
    assert option.subscribed == [("options.order_book", [OPTION_SYMBOL, "10", "0"])]


async def test_a_spot_push_interval_may_be_chosen_through_command_params(
    harness: Harness,
) -> None:
    """Spot pushes the whole book on a timer, so the rate has to be selectable."""
    transport = attach_ws(harness, GateioProductType.SPOT)

    await subscribe_depth(harness, subscribe_command(SPOT_ID, params={"interval": "1000ms"}))

    assert transport.subscribed == [("spot.order_book", ["BTC_USDT", "10", "1000ms"])]


async def test_an_interval_the_product_rejects_falls_back(harness: Harness) -> None:
    perp = attach_ws(harness, GateioProductType.PERP)

    await subscribe_depth(harness, subscribe_command(PERP_ID, params={"interval": "1000ms"}))

    assert perp.subscribed == [("futures.order_book", ["BTC_USDT", "10", "0"])]


async def test_a_depth_below_ten_is_rounded_up_to_one_the_venue_serves(
    harness: Harness,
) -> None:
    """Spot serves 5/10/20/50/100, so three levels is not a payload the venue accepts."""
    transport = attach_ws(harness, GateioProductType.SPOT)

    await subscribe_depth(harness, subscribe_command(SPOT_ID, depth=3))

    assert transport.subscribed == [("spot.order_book", ["BTC_USDT", "5", "100ms"])]


async def test_a_depth_above_ten_is_capped_at_the_type_capacity(harness: Harness) -> None:
    transport = attach_ws(harness, GateioProductType.PERP)

    await subscribe_depth(harness, subscribe_command(PERP_ID, depth=50))

    assert transport.subscribed == [("futures.order_book", ["BTC_USDT", "10", "0"])]


async def test_a_book_type_gateio_does_not_publish_is_refused(harness: Harness) -> None:
    transport = attach_ws(harness, GateioProductType.SPOT)

    await subscribe_depth(harness, subscribe_command(SPOT_ID, book_type=BookType.L3_MBO))

    assert transport.subscribed == []
    assert SPOT_ID not in harness.client._depth_streams


# -- the published depth -----------------------------------------------------


def register_depth(harness: Harness, instrument_id: InstrumentId) -> None:
    """Register the stream the way a completed subscription would."""
    harness.client._depth_streams[instrument_id] = (10, "0")


def test_a_spot_snapshot_publishes_one_depth_with_the_venue_levels(harness: Harness) -> None:
    """The spot push names its sequence ``lastUpdateId``, not ``id`` or ``u``."""
    register_depth(harness, SPOT_ID)

    harness.client._handle_ws_message(
        GateioProductType.SPOT,
        spot_snapshot(
            981,
            bids=[("99.5", "1.5"), ("99.4", "2")],
            asks=[("100.5", "1"), ("100.6", "3")],
        ),
    )

    published = depths(harness)
    assert len(published) == 1
    depth = published[0]
    assert depth.instrument_id == SPOT_ID
    assert depth.sequence == 981
    assert depth.ts_event == 1_700_000_000_100 * 1_000_000
    assert depth.flags == RecordFlag.F_LAST
    assert [order.price.as_double() for order in real_levels(depth.bids)] == [99.5, 99.4]
    assert [order.price.as_double() for order in real_levels(depth.asks)] == [100.5, 100.6]
    assert [order.size.as_double() for order in real_levels(depth.bids)] == [1.5, 2.0]
    assert all(order.side == OrderSide.BUY for order in real_levels(depth.bids))
    assert all(order.side == OrderSide.SELL for order in real_levels(depth.asks))
    # Gate.io publishes aggregated price levels, so there is no order count.
    assert list(depth.bid_counts) == [0] * DEPTH10_LEVELS
    assert list(depth.ask_counts) == [0] * DEPTH10_LEVELS


def test_a_contract_snapshot_uses_the_object_level_form(harness: Harness) -> None:
    register_depth(harness, PERP_ID)

    harness.client._handle_ws_message(
        GateioProductType.PERP,
        contract_snapshot(
            "futures.order_book",
            "BTC_USDT",
            book_id=4242,
            bids=[("99", "5")],
            asks=[("101", "7")],
        ),
    )

    depth = depths(harness)[0]
    assert depth.sequence == 4242
    assert real_levels(depth.bids)[0].price.as_double() == 99.0
    assert real_levels(depth.asks)[0].size.as_double() == 7.0


def test_asymmetric_sides_are_padded_rather_than_dropped(harness: Harness) -> None:
    """``OrderBookDepth10`` refuses unequal sides, and thin contracts send them.

    The constructor's ``Condition.equal(bids_len, asks_len)`` raises inside a
    handler whose caller logs and continues, so the failure would look like a
    healthy subscription publishing nothing at all.
    """
    register_depth(harness, PERP_ID)

    harness.client._handle_ws_message(
        GateioProductType.PERP,
        contract_snapshot(
            "futures.order_book",
            "BTC_USDT",
            book_id=1,
            bids=[(str(99 - i), "1") for i in range(8)],
            asks=[(str(101 + i), "1") for i in range(5)],
        ),
    )

    depth = depths(harness)[0]
    assert len(real_levels(depth.bids)) == 8
    assert len(real_levels(depth.asks)) == 5
    assert len(depth.bids) == len(depth.asks) == DEPTH10_LEVELS


def test_an_empty_side_still_publishes(harness: Harness) -> None:
    register_depth(harness, PERP_ID)

    harness.client._handle_ws_message(
        GateioProductType.PERP,
        contract_snapshot("futures.order_book", "BTC_USDT", 1, bids=[("99", "1")], asks=[]),
    )

    depth = depths(harness)[0]
    assert len(real_levels(depth.bids)) == 1
    assert real_levels(depth.asks) == []


def test_levels_are_sorted_before_publication(harness: Harness) -> None:
    """The type sorts nothing and ``to_quote_tick`` reads ``bids[0]`` blindly."""
    register_depth(harness, PERP_ID)

    harness.client._handle_ws_message(
        GateioProductType.PERP,
        contract_snapshot(
            "futures.order_book",
            "BTC_USDT",
            book_id=1,
            bids=[("98", "1"), ("100", "2"), ("99", "3")],
            asks=[("103", "1"), ("101", "2"), ("102", "3")],
        ),
    )

    depth = depths(harness)[0]
    assert [order.price.as_double() for order in real_levels(depth.bids)] == [100.0, 99.0, 98.0]
    assert [order.price.as_double() for order in real_levels(depth.asks)] == [101.0, 102.0, 103.0]
    quote = depth.to_quote_tick()
    assert quote is not None
    assert quote.bid_price.as_double() == 100.0
    assert quote.ask_price.as_double() == 101.0


def test_a_level_below_the_size_increment_frees_its_slot(harness: Harness) -> None:
    """Every derivative has ``size_precision == 0``, so a fractional size truncates.

    ``apply_depth`` drops a zero-sized level rather than rejecting it, which is
    worse than an error: the level would occupy one of the ten slots and then
    vanish, silently shortening the published depth.
    """
    register_depth(harness, PERP_ID)
    levels = [(str(120 - i), "1") for i in range(DEPTH10_LEVELS)]
    levels.insert(0, ("121", "0.4"))

    harness.client._handle_ws_message(
        GateioProductType.PERP,
        contract_snapshot("futures.order_book", "BTC_USDT", 1, bids=levels, asks=[("130", "1")]),
    )

    depth = depths(harness)[0]
    prices = [order.price.as_double() for order in real_levels(depth.bids)]
    assert len(prices) == DEPTH10_LEVELS
    assert 121.0 not in prices, "the unrepresentable level was published anyway"
    assert prices[0] == 120.0
    assert harness.client.metrics()["published"]["book_levels_not_representable"] == 1


def test_a_reordered_push_is_dropped_and_counted(harness: Harness) -> None:
    """The platform performs no gap or staleness check at all on depth."""
    register_depth(harness, PERP_ID)

    for book_id in (100, 102, 101):
        harness.client._handle_ws_message(
            GateioProductType.PERP,
            contract_snapshot("futures.order_book", "BTC_USDT", book_id, [("99", "1")], []),
        )

    assert [depth.sequence for depth in depths(harness)] == [100, 102]
    assert harness.client.metrics()["published"]["order_book_depths_out_of_order"] == 1


def test_a_push_without_a_sequence_is_published(harness: Harness) -> None:
    """A missing sequence means "cannot judge", not "stale"."""
    register_depth(harness, PERP_ID)
    message = contract_snapshot("futures.order_book", "BTC_USDT", 0, [("99", "1")], [])
    message["result"].pop("id")

    harness.client._handle_ws_message(GateioProductType.PERP, message)
    harness.client._handle_ws_message(GateioProductType.PERP, message)

    assert len(depths(harness)) == 2
    assert depths(harness)[0].sequence == 0


def test_two_pushes_from_the_same_level_lists_publish_equal_sides(harness: Harness) -> None:
    """``OrderBookDepth10.__init__`` extends the lists it is handed.

    A side list built once and reused would come back padded with seven
    ``NULL_ORDER`` entries the second time round, which a single-shot test never
    sees.
    """
    register_depth(harness, PERP_ID)
    bids = [{"p": "99", "s": "1"}]
    asks = [{"p": "101", "s": "1"}]

    for book_id in (1, 2):
        harness.client._handle_ws_message(
            GateioProductType.PERP,
            {
                "channel": "futures.order_book",
                "event": "all",
                "result": {
                    "t": 1_700_000_000_100,
                    "id": book_id,
                    "contract": "BTC_USDT",
                    "bids": bids,
                    "asks": asks,
                },
            },
        )

    first, second = depths(harness)
    assert level_tuples(first.bids) == level_tuples(second.bids) == [(99.0, 1.0)]
    assert level_tuples(first.asks) == level_tuples(second.asks) == [(101.0, 1.0)]


def test_a_push_for_an_unsubscribed_instrument_is_ignored(harness: Harness) -> None:
    harness.client._handle_ws_message(
        GateioProductType.PERP,
        contract_snapshot("futures.order_book", "BTC_USDT", 1, [("99", "1")], [("101", "1")]),
    )

    assert depths(harness) == []


# -- unsubscribe -------------------------------------------------------------


async def test_unsubscribe_repeats_the_payload_that_was_subscribed(harness: Harness) -> None:
    """Gate.io keys a subscription by its full argument list."""
    transport = attach_ws(harness, GateioProductType.SPOT)
    await subscribe_depth(harness, subscribe_command(SPOT_ID, params={"interval": "1000ms"}))

    await harness.client._unsubscribe_order_book_depth(unsubscribe_command(SPOT_ID))

    assert transport.unsubscribed == [("spot.order_book", ["BTC_USDT", "10", "1000ms"])]
    assert SPOT_ID not in harness.client._depth_streams

    await harness.client._unsubscribe_order_book_depth(unsubscribe_command(SPOT_ID))
    assert len(transport.unsubscribed) == 1, "a second unsubscribe must be a no-op"


async def test_unsubscribing_depth_clears_the_sequence_watermark(harness: Harness) -> None:
    """Re-subscribing must not drop the venue's first push as "not newer"."""
    attach_ws(harness, GateioProductType.PERP)
    await subscribe_depth(harness, subscribe_command(PERP_ID))
    harness.client._handle_ws_message(
        GateioProductType.PERP,
        contract_snapshot("futures.order_book", "BTC_USDT", 900, [("99", "1")], []),
    )
    await harness.client._unsubscribe_order_book_depth(unsubscribe_command(PERP_ID))

    await subscribe_depth(harness, subscribe_command(PERP_ID))
    harness.client._handle_ws_message(
        GateioProductType.PERP,
        contract_snapshot("futures.order_book", "BTC_USDT", 5, [("99", "1")], []),
    )

    assert [depth.sequence for depth in depths(harness)] == [900, 5]


# -- coexistence with the delta path -----------------------------------------


async def test_deltas_and_depth_hold_two_venue_subscriptions_and_warn(harness: Harness) -> None:
    """Both are legal at the venue; the collision is in the platform's cached book."""
    transport = attach_ws(harness, GateioProductType.PERP)
    harness.client._loop = asyncio.get_running_loop()
    # The delta path seeds its book from REST in a task of its own, which this
    # test has no transport for; only its venue subscription is under assertion.
    harness.client.create_task = lambda coro, **kwargs: coro.close()  # type: ignore[method-assign]
    await harness.client._subscribe_order_book_deltas(
        data_module.SubscribeOrderBook(
            instrument_id=PERP_ID,
            book_data_type=OrderBookDelta,
            book_type=BookType.L2_MBP,
            client_id=GATEIO_CLIENT_ID,
            venue=GATEIO_VENUE,
            command_id=UUID4(),
            ts_init=0,
            depth=20,
        ),
    )
    del harness.client.create_task

    await subscribe_depth(harness, subscribe_command(PERP_ID))

    channels = [channel for channel, _ in transport.subscribed]
    assert "futures.order_book_update" in channels
    assert "futures.order_book" in channels
    assert PERP_ID in harness.client._books
    assert PERP_ID in harness.client._depth_streams

    await harness.client._unsubscribe_order_book_depth(unsubscribe_command(PERP_ID))
    assert PERP_ID in harness.client._books, "the delta subscription was torn down with depth"


async def test_a_reconnect_drops_the_sequence_watermark(harness: Harness) -> None:
    """A venue that restarts its ids must not be read as one replaying old books.

    Gate.io opens a new stream on a new connection and its snapshot ``id`` starts
    again from a low number. Keeping the watermark across the reconnect made the
    monotonic guard drop every push of the new stream into a counter, with no log
    line, while the subscription still reported healthy.
    """
    attach_ws(harness, GateioProductType.PERP)
    await subscribe_depth(harness, subscribe_command(PERP_ID))
    harness.client._handle_ws_message(
        GateioProductType.PERP,
        contract_snapshot("futures.order_book", "BTC_USDT", 9_000_000, [("99", "1")], []),
    )

    await harness.client._handle_ws_reconnect(GateioProductType.PERP)
    for sequence in (1, 2, 3):
        harness.client._handle_ws_message(
            GateioProductType.PERP,
            contract_snapshot("futures.order_book", "BTC_USDT", sequence, [("99", "1")], []),
        )

    assert [depth.sequence for depth in depths(harness)] == [9_000_000, 1, 2, 3]
    published = harness.client.metrics()["published"]
    assert published.get("order_book_depths_out_of_order", 0) == 0


def test_an_unreadable_snapshot_message_is_reported(
    harness: Harness,
    log_capture: Any,  # the shared session fixture from `tests/conftest.py`
) -> None:
    """Silence on this channel is indistinguishable from a quiet market."""
    register_depth(harness, PERP_ID)
    log_capture.mark()

    harness.client._handle_ws_message(
        GateioProductType.PERP,
        {"channel": "futures.order_book", "event": "all", "result": ["not", "a", "book"]},
    )

    lines = log_capture.wait_for("unreadable")
    assert any("[WARN]" in line and "order book snapshot" in line for line in lines), lines
    assert depths(harness) == []


async def test_a_reconnect_schedules_no_resnapshot_for_a_depth_only_instrument(
    harness: Harness,
) -> None:
    """The snapshot channel resynchronises itself; a REST seed would be pointless."""
    attach_ws(harness, GateioProductType.PERP)
    await subscribe_depth(harness, subscribe_command(PERP_ID))
    scheduled: list[str] = []
    harness.client.create_task = lambda coro, **kwargs: (  # type: ignore[method-assign]
        coro.close(),
        scheduled.append(kwargs.get("log_msg", "")),
    )[0]

    await harness.client._handle_ws_reconnect(GateioProductType.PERP)

    assert scheduled == []


# -- the platform behaviour this feature replaced ----------------------------


async def test_an_unimplemented_hook_still_leaves_a_phantom_subscription(
    harness: Harness,
) -> None:
    """The platform fact the depth hook used to demonstrate, pinned elsewhere.

    ``LiveMarketDataClient`` records a subscription before creating the task
    that raises, and ``DataEngine`` then skips anything already recorded, so an
    unimplemented hook is logged exactly once and reported as subscribed
    forever. ``subscribe_option_greeks`` is still unimplemented here, so it is
    where that claim stays checkable now that depth is served.
    """
    client = harness.client
    client._loop = asyncio.get_running_loop()

    client.subscribe_option_greeks(
        SubscribeOptionGreeks(OPTION_ID, GATEIO_CLIENT_ID, GATEIO_VENUE, UUID4(), 0),
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert client.subscribed_option_greeks() == [OPTION_ID]

    # The claim is also a documented one, and the page has to keep saying it
    # while any hook on this client is left to the base class. The page is read
    # with its line wrapping collapsed, so re-flowing a paragraph does not fail
    # a test about what the paragraph says.
    page = (Path(__file__).resolve().parent.parent / "docs" / "market-data.md").read_text(
        encoding="utf-8",
    )
    prose = " ".join(page.split())
    assert "Read the log line, not the subscription list." in prose
    assert "`subscribe_order_book_depth` (`OrderBookDepth10`) is not implemented" not in prose


async def test_a_depth_subscription_now_reaches_the_venue_and_publishes(
    harness: Harness,
) -> None:
    """The end-to-end claim: subscribe through the platform, then receive a depth.

    On the pre-change tree the subscription raised inside its task, no venue
    subscription was sent, and no ``OrderBookDepth10`` could ever arrive.
    """
    transport = attach_ws(harness, GateioProductType.PERP)

    await subscribe_depth(harness, subscribe_command(PERP_ID))
    harness.client._handle_ws_message(
        GateioProductType.PERP,
        contract_snapshot("futures.order_book", "BTC_USDT", 7, [("99", "2")], [("101", "3")]),
    )

    assert transport.subscribed == [("futures.order_book", ["BTC_USDT", "10", "0"])]
    assert harness.client.subscribed_order_book_depth() == [PERP_ID]
    assert len(depths(harness)) == 1
    assert harness.client.metrics()["published"]["order_book_depths"] == 1


# -- the payload builder, per product ----------------------------------------


@pytest.mark.parametrize(
    ("product", "symbol", "channel", "interval"),
    [
        (GateioProductType.SPOT, "BTC_USDT", "spot.order_book", "100ms"),
        (GateioProductType.PERP, "BTC_USDT", "futures.order_book", "0"),
        (GateioProductType.INVERSE, "BTC_USD", "futures.order_book", "0"),
        (GateioProductType.FUT, "BTC_USDT_20260925", "futures.order_book", "0"),
        (GateioProductType.OPT, OPTION_SYMBOL, "options.order_book", "0"),
    ],
)
async def test_the_snapshot_payload_builder_serves_ten_levels_on_every_product(
    product: GateioProductType,
    symbol: str,
    channel: str,
    interval: str,
) -> None:
    """Ten is a member of every product's accepted depth table, so nothing truncates."""
    ws = GateioPublicWebSocket(product=product, handler=lambda msg: None)
    transport = RecordingTransport()
    ws.client = transport  # type: ignore[assignment]

    await ws.subscribe_order_book_snapshot(symbol, limit=DEPTH10_LEVELS)

    assert transport.subscribed == [(channel, [symbol, "10", interval])]
