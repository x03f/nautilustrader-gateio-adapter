"""Regression tests for the venue-native ticker type and the quote-history refusal.

``GateioTicker`` is what the in-tree adapters call an adapter-specific data type:
a ``Data`` subclass carrying the venue fields the platform has no type for,
routed through the generic ``_subscribe`` / ``_unsubscribe`` hooks. Mark prices,
index prices, funding rates and the option greeks are *not* on it — the platform
has its own types for those and this client publishes them from the same message.

The greeks are also tested here rather than beside the other ticker-derived
types, because what they demonstrate is the boundary this module is about: the
five standard greeks, the implied volatilities and the open interest are the
platform's canonical option schema and left the custom type when they gained a
native home, while a venue-specific sensitivity Gate.io does not publish at all
would have stayed on it.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.actor import Actor
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.common.config import ActorConfig
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import DataEngineConfig
from nautilus_trader.core.data import Data
from nautilus_trader.core.nautilus_pyo3 import GreeksConvention
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.data.engine import DataEngine
from nautilus_trader.data.messages import (
    RequestData,
    RequestQuoteTicks,
    SubscribeData,
    SubscribeMarkPrices,
    SubscribeOptionGreeks,
    UnsubscribeData,
    UnsubscribeMarkPrices,
    UnsubscribeOptionGreeks,
)
from nautilus_trader.model.data import (
    CustomData,
    DataType,
    MarkPriceUpdate,
    OptionGreeks,
    QuoteTick,
)
from nautilus_trader.model.identifiers import InstrumentId, TraderId
from nautilus_trader.portfolio.portfolio import Portfolio
from nautilus_trader.serialization.arrow.serializer import ArrowSerializer, get_schema
from nautilus_trader.serialization.base import register_serializable_type

from gateio_nt.common.constants import GATEIO_CLIENT_ID, GATEIO_VENUE
from gateio_nt.common.enums import GateioProductType
from gateio_nt.common.errors import GateioError
from gateio_nt.config import GateioDataClientConfig
from gateio_nt.data import GateioDataClient
from gateio_nt.types import TICKER_FIELDS, GateioTicker
from gateio_nt.websocket.public import GateioPublicWebSocket
from tests.test_data_book_depth import RecordingTransport, attach_ws, build_harness
from tests.test_data_client import (
    OPTION_ID,
    OPTION_SYMBOL,
    PERP_ID,
    SPOT_ID,
    Harness,
    StubProvider,
    build_instruments,
)


@pytest.fixture
def harness() -> Harness:
    return build_harness()


def subscribe_ticker(instrument_id: InstrumentId) -> SubscribeData:
    return SubscribeData(
        data_type=DataType(GateioTicker),
        instrument_id=instrument_id,
        client_id=GATEIO_CLIENT_ID,
        venue=GATEIO_VENUE,
        command_id=UUID4(),
        ts_init=0,
    )


def unsubscribe_ticker(instrument_id: InstrumentId) -> UnsubscribeData:
    return UnsubscribeData(
        data_type=DataType(GateioTicker),
        instrument_id=instrument_id,
        client_id=GATEIO_CLIENT_ID,
        venue=GATEIO_VENUE,
        command_id=UUID4(),
        ts_init=0,
    )


def futures_ticker_message(symbol: str = "BTC_USDT") -> dict[str, Any]:
    return {
        "channel": "futures.tickers",
        "event": "update",
        "result": [
            {
                "contract": symbol,
                "t": 1_700_000_000_000,
                "last": "64100.1",
                "change_percentage": "1.23",
                "high_24h": "65000",
                "low_24h": "63000",
                "volume_24h": "123456",
                "volume_24h_base": "12.3",
                "total_size": "98765",
                "funding_rate": "0.0001",
                "funding_rate_indicative": "0.00012",
                "mark_price": "64099.9",
                "index_price": "64100.0",
            },
        ],
    }


def published_tickers(harness: Harness) -> list[CustomData]:
    """Every ticker the client published, still in its ``CustomData`` wrapper.

    The wrapper is asserted on rather than unwrapped at the seam, because it is
    the part the platform reads: an unwrapped ``GateioTicker`` reaches
    ``DataEngine._handle_data`` and is logged as an unrecognised type.
    """
    return [
        item
        for item in harness.published
        if isinstance(item, CustomData) and item.data_type.type is GateioTicker
    ]


def tickers(harness: Harness) -> list[GateioTicker]:
    return [item.data for item in published_tickers(harness)]


#: Sentinel for a field ``options_ticker_message`` should leave out of the row
#: entirely, which is not the same payload as one carrying an empty string.
ABSENT: Any = object()

#: The nine values the greek row states, all distinct, so a test asserting that
#: one of them landed on one field cannot pass with two of them transposed.
GREEK_ROW: dict[str, str] = {
    "delta": "0.5512",
    "gamma": "0.000031",
    "vega": "51.77",
    "theta": "-13.9",
    "rho": "22.4",
    "mark_iv": "0.6231",
    "bid_iv": "0.6109",
    "ask_iv": "0.6402",
    "position_size": "1234",
}


def options_ticker_message(**overrides: Any) -> dict[str, Any]:
    """One ``options.contract_tickers`` row; that channel keys on ``name``.

    A field passed as :data:`ABSENT` is dropped from the row, which is how the
    venue says nothing about it.
    """
    row: dict[str, Any] = {
        "name": OPTION_SYMBOL,
        "last": "5800",
        "mark_price": "5797.7",
        "index_price": "64123.45",
        **GREEK_ROW,
    }
    row.update(overrides)
    return {
        "channel": "options.contract_tickers",
        "event": "update",
        "result": [{key: value for key, value in row.items() if value is not ABSENT}],
    }


def greeks(harness: Harness) -> list[OptionGreeks]:
    return [item for item in harness.published if isinstance(item, OptionGreeks)]


def greeks_command(instrument_id: InstrumentId) -> SubscribeOptionGreeks:
    return SubscribeOptionGreeks(instrument_id, GATEIO_CLIENT_ID, GATEIO_VENUE, UUID4(), 0)


# -- the type ----------------------------------------------------------------


def test_the_ticker_carries_nothing_the_platform_has_a_type_for() -> None:
    """Two sources for one number is how a strategy reads two different values."""
    forbidden = {"mark_price", "index_price", "funding_rate", *GREEK_ROW}

    assert forbidden.isdisjoint(GateioTicker.__annotations__)
    assert forbidden.isdisjoint(TICKER_FIELDS)
    # The *indicative* next-funding rate is not the applied rate and has no
    # platform type, so it does belong here.
    assert "funding_rate_indicative" in TICKER_FIELDS


def test_the_ticker_round_trips_through_the_platform_serializers() -> None:
    """Through the registry the platform would use, not the class's own methods."""
    ticker = GateioTicker(
        ts_event=1_700_000_000_000_000_000,
        ts_init=1_700_000_000_000_000_001,
        instrument_id=PERP_ID,
        last="64100.1",
        volume_24h="123456",
    )

    batch = ArrowSerializer.serialize_batch([ticker], GateioTicker)
    restored = ArrowSerializer.deserialize(GateioTicker, batch)

    assert restored == [ticker]
    assert get_schema(GateioTicker) is not None


def test_the_ticker_is_registered_exactly_once() -> None:
    """``@customdataclass`` registers the type itself.

    Copying the older in-tree pattern and calling ``register_serializable_type``
    again in ``__init__.py`` would raise at *import* time, so the package would
    stop importing entirely rather than failing one test.
    """
    with pytest.raises(KeyError):
        register_serializable_type(GateioTicker, GateioTicker.to_dict, GateioTicker.from_dict)


def test_an_absent_venue_field_is_empty_rather_than_carried_forward() -> None:
    first = GateioTicker.from_payload(PERP_ID, {"last": "1", "high_24h": "2"}, 1, 2)
    second = GateioTicker.from_payload(PERP_ID, {"last": "3"}, 3, 4)

    assert first.high_24h == "2"
    assert second.high_24h == "", "a stale value survived into the next ticker"


# -- the subscribe path ------------------------------------------------------


async def test_a_ticker_subscription_publishes_the_venue_row(harness: Harness) -> None:
    """Fails on the pre-change tree: ``_subscribe`` is not implemented there."""
    transport = attach_ws(harness, GateioProductType.PERP)

    await harness.client._subscribe(subscribe_ticker(PERP_ID))
    harness.client._handle_ws_message(GateioProductType.PERP, futures_ticker_message())

    assert transport.subscribed == [("futures.tickers", ["BTC_USDT"])]
    wrapped = published_tickers(harness)
    assert len(wrapped) == 1
    # The metadata is what the engine turns into the published topic, so a
    # subscriber asking for this instrument's ticker is addressed by it.
    assert wrapped[0].data_type == DataType(GateioTicker, metadata={"instrument_id": PERP_ID})
    published = tickers(harness)
    assert published[0].instrument_id == PERP_ID
    assert published[0].last == "64100.1"
    assert published[0].total_size == "98765"
    assert published[0].funding_rate_indicative == "0.00012"
    assert published[0].ts_event == 1_700_000_000_000 * 1_000_000
    assert harness.client.metrics()["published"]["tickers"] == 1


async def test_a_ticker_and_a_mark_price_share_one_venue_subscription(
    harness: Harness,
) -> None:
    """The reference count is what keeps a fourth kind from cancelling the other three."""
    transport = attach_ws(harness, GateioProductType.PERP)

    await harness.client._subscribe(subscribe_ticker(PERP_ID))
    await harness.client._subscribe_mark_prices(
        SubscribeMarkPrices(PERP_ID, GATEIO_CLIENT_ID, GATEIO_VENUE, UUID4(), 0),
    )

    assert transport.subscribed == [("futures.tickers", ["BTC_USDT"])]
    assert harness.client._ticker_subs[PERP_ID] == {"ticker", "mark"}

    await harness.client._unsubscribe(unsubscribe_ticker(PERP_ID))

    assert transport.unsubscribed == [], "the mark-price subscriber lost its channel"
    assert harness.client._ticker_subs[PERP_ID] == {"mark"}

    await harness.client._unsubscribe_mark_prices(
        UnsubscribeMarkPrices(PERP_ID, GATEIO_CLIENT_ID, GATEIO_VENUE, UUID4(), 0),
    )
    assert transport.unsubscribed == [("futures.tickers", ["BTC_USDT"])]


async def test_both_a_ticker_and_the_platform_types_come_from_one_message(
    harness: Harness,
) -> None:
    attach_ws(harness, GateioProductType.PERP)
    await harness.client._subscribe(subscribe_ticker(PERP_ID))
    await harness.client._subscribe_mark_prices(
        SubscribeMarkPrices(PERP_ID, GATEIO_CLIENT_ID, GATEIO_VENUE, UUID4(), 0),
    )

    harness.client._handle_ws_message(GateioProductType.PERP, futures_ticker_message())

    marks = [item for item in harness.published if isinstance(item, MarkPriceUpdate)]
    assert len(marks) == 1
    assert len(tickers(harness)) == 1
    assert str(marks[0].value) == "64099.9"


async def test_spot_may_subscribe_the_ticker_even_though_it_has_no_mark_price(
    harness: Harness,
) -> None:
    """``spot.tickers`` is a real channel; it just carries none of the three."""
    transport = attach_ws(harness, GateioProductType.SPOT)

    await harness.client._subscribe(subscribe_ticker(SPOT_ID))
    harness.client._handle_ws_message(
        GateioProductType.SPOT,
        {
            "channel": "spot.tickers",
            "event": "update",
            "result": {
                "currency_pair": "BTC_USDT",
                "last": "64100.1",
                "highest_bid": "64100.0",
                "lowest_ask": "64100.2",
                "base_volume": "12.3",
                "quote_volume": "789012",
            },
        },
    )

    assert transport.subscribed == [("spot.tickers", ["BTC_USDT"])]
    published = tickers(harness)
    assert len(published) == 1
    assert published[0].highest_bid == "64100.0"
    assert published[0].base_volume == "12.3"


async def test_a_spot_mark_price_subscription_is_still_refused(harness: Harness) -> None:
    transport = attach_ws(harness, GateioProductType.SPOT)

    await harness.client._subscribe_mark_prices(
        SubscribeMarkPrices(SPOT_ID, GATEIO_CLIENT_ID, GATEIO_VENUE, UUID4(), 0),
    )

    assert transport.subscribed == []
    assert not harness.client._ticker_subs.get(SPOT_ID)


async def test_an_unknown_custom_data_type_is_refused_without_raising(
    harness: Harness,
) -> None:
    """A ``_subscribe`` that raises leaves a subscription the engine never retries."""
    # Attached so that the refusal is measured against a transport that would
    # have recorded a venue subscription. Without one, this test passed on the
    # absence of a socket rather than on the refusal.
    transport = attach_ws(harness, GateioProductType.PERP)

    class Unrelated(Data):
        """A custom data type this venue knows nothing about."""

    command = SubscribeData(
        data_type=DataType(Unrelated),
        instrument_id=PERP_ID,
        client_id=GATEIO_CLIENT_ID,
        venue=GATEIO_VENUE,
        command_id=UUID4(),
        ts_init=0,
    )

    await harness.client._subscribe(command)
    await harness.client._unsubscribe(
        UnsubscribeData(
            data_type=DataType(Unrelated),
            instrument_id=PERP_ID,
            client_id=GATEIO_CLIENT_ID,
            venue=GATEIO_VENUE,
            command_id=UUID4(),
            ts_init=0,
        ),
    )

    assert tickers(harness) == []
    assert transport.subscribed == [], "an unknown type reached the venue"


async def test_a_ticker_subscription_naming_no_instrument_is_refused(
    harness: Harness,
) -> None:
    transport = attach_ws(harness, GateioProductType.PERP)
    command = SubscribeData(
        data_type=DataType(GateioTicker),
        instrument_id=None,
        client_id=GATEIO_CLIENT_ID,
        venue=GATEIO_VENUE,
        command_id=UUID4(),
        ts_init=0,
    )

    await harness.client._subscribe(command)

    assert harness.client._ticker_subs == {}
    assert transport.subscribed == [], "a command naming no instrument reached the venue"


async def test_a_transient_subscribe_failure_keeps_the_ticker_registry_entry(
    harness: Harness,
) -> None:
    """The replayed stream is routed by this registry and by nothing else."""

    class DisconnectedWs:
        async def subscribe_tickers(self, symbol: str) -> None:
            raise GateioError(0, "WS_NOT_CONNECTED", "reconnecting")

    harness.client._ws_clients[GateioProductType.PERP] = DisconnectedWs()  # type: ignore[assignment]

    await harness.client._subscribe(subscribe_ticker(PERP_ID))

    assert harness.client._ticker_subs[PERP_ID] == {"ticker"}
    # The proof that the entry matters: a row arriving on the replayed stream is
    # published rather than discarded as unsubscribed.
    harness.client._handle_ws_message(GateioProductType.PERP, futures_ticker_message())
    assert len(tickers(harness)) == 1


async def test_a_refused_ticker_subscription_drops_the_registry_entry(
    harness: Harness,
) -> None:
    """A venue refusal is not replayed, so holding the entry would leak state."""

    class RefusingWs:
        async def subscribe_tickers(self, symbol: str) -> None:
            raise GateioError(0, "WS_REQUEST_REJECTED", "no such channel")

    harness.client._ws_clients[GateioProductType.PERP] = RefusingWs()  # type: ignore[assignment]

    await harness.client._subscribe(subscribe_ticker(PERP_ID))

    assert harness.client._ticker_subs.get(PERP_ID) in (None, set())


async def test_an_instrument_named_only_in_the_metadata_is_honoured(
    harness: Harness,
) -> None:
    """A caller may build the ``DataType`` by hand, leaving a plain string there."""
    transport = attach_ws(harness, GateioProductType.OPT)
    command = SubscribeData(
        data_type=DataType(GateioTicker, metadata={"instrument_id": OPTION_ID.value}),
        instrument_id=None,
        client_id=GATEIO_CLIENT_ID,
        venue=GATEIO_VENUE,
        command_id=UUID4(),
        ts_init=0,
    )

    await harness.client._subscribe(command)

    assert transport.subscribed == [("options.contract_tickers", [OPTION_ID.symbol.value])]


# -- through a real DataEngine -----------------------------------------------
#
# Every other test in this file reads what the client handed to `_handle_data`,
# because `Harness` replaces that method with a list. That seam is why a
# published type the engine cannot dispatch survived a green suite: the client
# published a bare `GateioTicker`, `DataEngine._handle_data` reached its `else`
# branch and logged "Cannot handle data: unrecognized type", and no assertion in
# the package ever saw it. These two tests drive the same publishes into a real
# engine with a real subscribing actor, offline.


class _Listener(Actor):
    """A real subscriber, so the assertion is that data arrives, not that it was sent."""

    def __init__(self) -> None:
        super().__init__(ActorConfig(component_id="LISTENER"))
        self.received: list[Any] = []

    def on_data(self, data: Any) -> None:
        self.received.append(data)

    def on_mark_price(self, mark_price: Any) -> None:
        self.received.append(mark_price)

    def on_index_price(self, index_price: Any) -> None:
        self.received.append(index_price)

    def on_funding_rate(self, funding_rate: Any) -> None:
        self.received.append(funding_rate)

    def on_option_greeks(self, option_greeks: Any) -> None:
        self.received.append(option_greeks)


def build_engine_rig() -> tuple[DataEngine, GateioDataClient, _Listener]:
    """A data client registered with a real ``DataEngine`` and a started actor."""
    clock = LiveClock()
    msgbus = MessageBus(trader_id=TraderId("TESTER-000"), clock=clock)
    cache = Cache()
    provider: InstrumentProvider = StubProvider()
    for instrument in build_instruments():
        provider.add(instrument)
        cache.add_instrument(instrument)
    portfolio = Portfolio(msgbus=msgbus, cache=cache, clock=clock)
    engine = DataEngine(msgbus=msgbus, cache=cache, clock=clock, config=DataEngineConfig())
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
    engine.register_client(client)
    engine.register_default_client(client)
    engine.start()
    listener = _Listener()
    listener.register_base(portfolio=portfolio, msgbus=msgbus, cache=cache, clock=clock)
    listener.start()
    return engine, client, listener


async def test_a_published_ticker_reaches_a_subscribing_actor() -> None:
    """Fails on the pre-change tree: the engine rejected the unwrapped row."""
    engine, client, listener = build_engine_rig()
    client._loop = asyncio.get_running_loop()
    ws = GateioPublicWebSocket(product=GateioProductType.PERP, handler=lambda msg: None)
    ws.client = RecordingTransport()  # type: ignore[assignment]
    client._ws_clients[GateioProductType.PERP] = ws

    data_type = DataType(GateioTicker, metadata={"instrument_id": PERP_ID})
    listener.subscribe_data(data_type, client_id=GATEIO_CLIENT_ID)
    engine.execute(
        SubscribeData(
            data_type=data_type,
            instrument_id=PERP_ID,
            client_id=GATEIO_CLIENT_ID,
            venue=GATEIO_VENUE,
            command_id=UUID4(),
            ts_init=0,
        ),
    )
    for _ in range(6):
        await asyncio.sleep(0)
    assert data_type in client.subscribed_custom_data()

    client._handle_ws_message(GateioProductType.PERP, futures_ticker_message())

    received = [item for item in listener.received if isinstance(item, GateioTicker)]
    assert len(received) == 1, "no GateioTicker reached the subscribing actor"
    assert received[0].last == "64100.1"


async def test_every_object_from_one_ticker_message_reaches_its_subscriber() -> None:
    """One ticker message publishes four objects; all four have to arrive.

    That message is the widest a single Gate.io message gets — three platform
    types and one venue-native type from the same row — so it is where a type the
    engine cannot dispatch is cheapest to catch. An object the engine rejects is
    an error line and nothing else: the engine still counts it, the client still
    reports the subscription held, and only a subscriber notices the absence.
    """
    engine, client, listener = build_engine_rig()
    client._loop = asyncio.get_running_loop()
    ws = GateioPublicWebSocket(product=GateioProductType.PERP, handler=lambda msg: None)
    ws.client = RecordingTransport()  # type: ignore[assignment]
    client._ws_clients[GateioProductType.PERP] = ws
    listener.subscribe_mark_prices(PERP_ID, client_id=GATEIO_CLIENT_ID)
    listener.subscribe_index_prices(PERP_ID, client_id=GATEIO_CLIENT_ID)
    listener.subscribe_funding_rates(PERP_ID, client_id=GATEIO_CLIENT_ID)
    listener.subscribe_data(
        DataType(GateioTicker, metadata={"instrument_id": PERP_ID}),
        client_id=GATEIO_CLIENT_ID,
    )
    for kind in ("mark", "index", "funding"):
        client._ticker_subs[PERP_ID].add(kind)
    await client._subscribe(subscribe_ticker(PERP_ID))
    before = engine.data_count

    client._handle_ws_message(GateioProductType.PERP, futures_ticker_message())

    assert engine.data_count == before + 4
    assert sorted(type(item).__name__ for item in listener.received) == [
        "FundingRateUpdate",
        "GateioTicker",
        "IndexPriceUpdate",
        "MarkPriceUpdate",
    ]


# -- option greeks -----------------------------------------------------------
#
# Gate.io states the five standard greeks, three implied volatilities and the
# contract's open interest on every `options.contract_tickers` row. They used to
# reach a strategy only as `GateioTicker` strings, so a strategy written against
# the platform's `subscribe_option_greeks` — the API Deribit, OKX and Bybit all
# serve — got nothing at all here, and got it silently.


async def test_a_platform_greeks_subscription_now_delivers_on_gateio() -> None:
    """The finding: this was the one unimplemented data hook on the client.

    Driven through a real ``DataEngine`` and a real subscribing actor rather
    than through the client's seam, because the seam is not where the damage
    was: on the pre-change tree the base class recorded the subscription and
    raised inside its own task, so ``subscribed_option_greeks()`` answered
    ``[OPTION_ID]`` there too and only the actor noticed the silence. The
    assertion that fails on that tree is the arrival, not the registration.
    """
    engine, client, listener = build_engine_rig()
    client._loop = asyncio.get_running_loop()
    ws = GateioPublicWebSocket(product=GateioProductType.OPT, handler=lambda msg: None)
    ws.client = RecordingTransport()  # type: ignore[assignment]
    client._ws_clients[GateioProductType.OPT] = ws

    listener.subscribe_option_greeks(OPTION_ID, client_id=GATEIO_CLIENT_ID)
    engine.execute(greeks_command(OPTION_ID))
    for _ in range(6):
        await asyncio.sleep(0)
    assert client.subscribed_option_greeks() == [OPTION_ID]

    client._handle_ws_message(GateioProductType.OPT, options_ticker_message())

    received = [item for item in listener.received if isinstance(item, OptionGreeks)]
    assert len(received) == 1, "no OptionGreeks reached the subscribing actor"
    assert received[0].instrument_id == OPTION_ID
    assert received[0].delta == 0.5512


def test_the_convention_is_left_for_the_platform_because_gate_io_states_none(
    harness: Harness,
) -> None:
    """The one published field the venue did not supply.

    Gate.io documents no numeraire for its greeks, so the client passes no
    ``convention`` and the platform's own default stands. Naming one here would
    publish a claim about Gate.io that Gate.io never made — and a strategy
    joining this book with a venue that *does* state its convention (OKX
    publishes both) would join on a label we invented. The docstring, the page
    and the changelog all make a point of this; nothing pinned it.
    """
    harness.client._ticker_subs[OPTION_ID] = {"greeks"}

    harness.client._handle_ws_message(GateioProductType.OPT, options_ticker_message())

    (published,) = greeks(harness)
    assert published.convention == GreeksConvention.BLACK_SCHOLES, (
        "the client stated a numeraire convention of its own; Gate.io states none, "
        "so the value must be the platform's default rather than an adapter claim"
    )


def test_each_venue_number_lands_on_the_field_that_names_it(harness: Harness) -> None:
    """Nine distinct values, so no two of them can be transposed and still pass."""
    harness.client._ticker_subs[OPTION_ID] = {"greeks"}

    harness.client._handle_ws_message(GateioProductType.OPT, options_ticker_message())

    (published,) = greeks(harness)
    assert (published.delta, published.gamma, published.vega) == (0.5512, 0.000031, 51.77)
    assert (published.theta, published.rho) == (-13.9, 22.4)
    assert (published.mark_iv, published.bid_iv, published.ask_iv) == (0.6231, 0.6109, 0.6402)
    assert published.open_interest == 1234.0


def test_the_underlying_price_is_the_index_and_not_the_option_mark(
    harness: Harness,
) -> None:
    """A Gate.io option settles against its underlying pair's index.

    ``index_price`` is the only underlying quote the row carries, and the option
    chain seeds at-the-money from ``underlying_price``. Seeding it from
    ``mark_price`` instead — the option's own premium, 5797.7 against an
    underlying near 64123.45 — would put every strike in the chain deep
    out-of-the-money.
    """
    harness.client._ticker_subs[OPTION_ID] = {"greeks"}

    harness.client._handle_ws_message(GateioProductType.OPT, options_ticker_message())

    (published,) = greeks(harness)
    assert published.underlying_price == 64123.45


def test_open_interest_reads_the_option_spelling_of_the_field(harness: Harness) -> None:
    """``position_size`` on an option row, ``total_size`` on a futures one.

    A row carrying both is not something Gate.io sends; it is here so that
    reading the futures spelling produces a different number rather than the
    same one, which is the only way the mistake shows.
    """
    harness.client._ticker_subs[OPTION_ID] = {"greeks"}

    harness.client._handle_ws_message(
        GateioProductType.OPT,
        options_ticker_message(total_size="99"),
    )

    (published,) = greeks(harness)
    assert published.open_interest == 1234.0


@pytest.mark.parametrize("field", ["delta", "gamma", "vega", "theta", "rho"])
def test_a_row_that_omits_one_greek_publishes_nothing_rather_than_a_zero(
    harness: Harness,
    field: str,
) -> None:
    """``0.0`` is a real delta, so it cannot also mean "the venue did not say".

    The five are non-optional doubles on the platform type, so there is no way
    to publish four of them. Filling the fifth from a parser's default would
    hand a strategy a fabricated sensitivity — a hedge sized on a gamma of zero
    is a hedge that is never rebalanced.
    """
    harness.client._ticker_subs[OPTION_ID] = {"greeks"}

    harness.client._handle_ws_message(
        GateioProductType.OPT,
        options_ticker_message(**{field: ABSENT}),
    )

    assert greeks(harness) == []
    assert harness.client._published["option_greeks_incomplete"] == 1
    assert harness.client._published["option_greeks"] == 0


@pytest.mark.parametrize("value", ["", "n/a", "nan", "inf", "-inf"])
def test_a_greek_that_is_not_a_finite_number_publishes_nothing(
    harness: Harness,
    value: str,
) -> None:
    """``float`` accepts ``"nan"`` and ``"inf"``; a venue quoting them does not.

    A NaN delta propagates through every portfolio aggregation that touches it
    and turns the whole book's exposure into NaN, which is worse than the
    missing update, so the row is dropped rather than passed on.
    """
    harness.client._ticker_subs[OPTION_ID] = {"greeks"}

    harness.client._handle_ws_message(
        GateioProductType.OPT,
        options_ticker_message(vega=value),
    )

    assert greeks(harness) == []
    assert harness.client._published["option_greeks_incomplete"] == 1


def test_a_row_of_zeros_is_published_because_zero_is_what_it_says(
    harness: Harness,
) -> None:
    """The other half of the rule the two tests above state.

    Gate.io sends ``"0"`` on every numeric field of a contract that has not
    traded, and a far out-of-the-money option really does have a delta near
    zero. Refusing a value because it is falsy would drop exactly the contracts
    an options strategy watches for a first quote, and would drop them silently.
    """
    harness.client._ticker_subs[OPTION_ID] = {"greeks"}

    harness.client._handle_ws_message(
        GateioProductType.OPT,
        options_ticker_message(**dict.fromkeys(GREEK_ROW, "0")),
    )

    (published,) = greeks(harness)
    assert published.delta == 0.0
    assert published.rho == 0.0
    assert published.mark_iv == 0.0
    assert published.open_interest == 0.0


def test_an_absent_optional_field_is_none_rather_than_zero(harness: Harness) -> None:
    """Zero implied volatility and zero open interest are both real statements.

    ``mark_iv``, ``bid_iv``, ``ask_iv``, ``underlying_price`` and
    ``open_interest`` are nullable on the platform type precisely so an adapter
    can decline to guess. A row that states none of them still publishes, since
    the five greeks it does state are the ones the type requires.
    """
    harness.client._ticker_subs[OPTION_ID] = {"greeks"}

    harness.client._handle_ws_message(
        GateioProductType.OPT,
        options_ticker_message(
            mark_iv=ABSENT,
            bid_iv="",
            ask_iv="n/a",
            index_price=ABSENT,
            position_size="",
        ),
    )

    (published,) = greeks(harness)
    assert (published.mark_iv, published.bid_iv, published.ask_iv) == (None, None, None)
    assert published.underlying_price is None
    assert published.open_interest is None
    assert published.delta == 0.5512


def test_one_venue_number_reaches_a_strategy_from_one_type_only(
    harness: Harness,
) -> None:
    """The nine canonical fields left ``GateioTicker`` when they gained a home.

    Both subscriptions are held here, which is exactly the case where a
    duplicated field gives a strategy two readings of one number and no rule for
    which to believe. The check is on the published values, not on the class, so
    restoring any one of the nine fields fails it.
    """
    harness.client._ticker_subs[OPTION_ID] = {"greeks", "ticker"}

    harness.client._handle_ws_message(GateioProductType.OPT, options_ticker_message())

    (published,) = greeks(harness)
    (row,) = tickers(harness)
    assert published.gamma == 0.000031
    assert row.last == "5800", "the row still carries what has no platform type"
    carried = {getattr(row, name) for name in TICKER_FIELDS}
    assert carried.isdisjoint(set(GREEK_ROW.values())), sorted(carried & set(GREEK_ROW.values()))


async def test_greeks_and_a_mark_price_share_the_one_option_ticker_channel(
    harness: Harness,
) -> None:
    """Gate.io has no greeks channel: both come off ``options.contract_tickers``.

    So canceling greeks must not stop the mark price, and the mark price must
    not keep greeks flowing after they were canceled — the second is the quieter
    failure, because the data still looks fine.
    """
    client = harness.client
    transport = attach_ws(harness, GateioProductType.OPT)

    await client._subscribe_option_greeks(greeks_command(OPTION_ID))
    await client._subscribe_mark_prices(
        SubscribeMarkPrices(OPTION_ID, GATEIO_CLIENT_ID, GATEIO_VENUE, UUID4(), 0),
    )
    assert len(transport.subscribed) == 1, transport.subscribed
    assert client._ticker_subs[OPTION_ID] == {"greeks", "mark"}

    await client._unsubscribe_option_greeks(
        UnsubscribeOptionGreeks(OPTION_ID, GATEIO_CLIENT_ID, GATEIO_VENUE, UUID4(), 0),
    )
    assert transport.unsubscribed == [], "canceling greeks stopped the mark price too"

    client._handle_ws_message(GateioProductType.OPT, options_ticker_message())
    assert greeks(harness) == [], "greeks kept arriving after they were canceled"
    assert [item for item in harness.published if isinstance(item, MarkPriceUpdate)]

    await client._unsubscribe_mark_prices(
        UnsubscribeMarkPrices(OPTION_ID, GATEIO_CLIENT_ID, GATEIO_VENUE, UUID4(), 0),
    )
    assert len(transport.unsubscribed) == 1


@pytest.mark.parametrize(
    ("instrument_id", "product"),
    [
        (PERP_ID, GateioProductType.PERP),
        (SPOT_ID, GateioProductType.SPOT),
    ],
)
async def test_greeks_are_refused_on_a_product_that_has_no_greeks(
    harness: Harness,
    instrument_id: InstrumentId,
    product: GateioProductType,
) -> None:
    """Refused rather than accepted and left silent.

    A perpetual and a spot pair have no strike and no expiry, and their ticker
    rows carry no greek fields. Accepting the subscription would take out a
    venue subscription that can never answer it and leave the client reporting a
    stream it will never publish.
    """
    transport = attach_ws(harness, product)

    await harness.client._subscribe_option_greeks(greeks_command(instrument_id))

    assert transport.subscribed == []
    assert not harness.client._ticker_subs.get(instrument_id)


# -- the helpers behind the hooks --------------------------------------------


def test_no_hook_shaped_method_takes_anything_but_a_command() -> None:
    """The ticker helpers used to sit in the hook namespace with another signature.

    Every ``_subscribe_*`` / ``_unsubscribe_*`` name on a ``LiveMarketDataClient``
    is a platform hook taking one command object; a private helper wearing that
    name misleads a reader and any sweep that enumerates hooks.
    """
    offenders = []
    for name, member in inspect.getmembers(GateioDataClient, inspect.isfunction):
        if name not in ("_subscribe", "_unsubscribe") and not name.startswith(
            ("_subscribe_", "_unsubscribe_"),
        ):
            continue
        parameters = list(inspect.signature(member).parameters)
        if parameters != ["self", "command"]:
            offenders.append((name, parameters))

    assert offenders == []


# -- historical quotes -------------------------------------------------------


class ExplodingTickersHttp:
    """Any read of the current ticker row is a fabrication of history."""

    async def tickers(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the refusal must not read the ticker endpoint")


def quote_request(instrument_id: InstrumentId) -> RequestQuoteTicks:
    return RequestQuoteTicks(
        instrument_id,
        None,
        None,
        0,
        GATEIO_CLIENT_ID,
        GATEIO_VENUE,
        None,
        UUID4(),
        0,
        {"some_adapter_param": True},
    )


async def test_a_historical_quote_request_is_refused_without_raising(
    harness: Harness,
    log_capture: Any,  # the shared session fixture from `tests/conftest.py`
) -> None:
    """Fails on the pre-change tree with ``NotImplementedError`` from the base class.

    The refusal does not *complete* the request: the platform opens a request
    group for every historical request and only a response closes it, so a
    caller awaiting the callback waits either way. What changes is that the log
    carries a sentence naming the venue and the alternative rather than a
    traceback.
    """
    harness.client._spot_http = ExplodingTickersHttp()  # type: ignore[assignment]
    log_capture.mark()

    await harness.client._request_quote_ticks(quote_request(SPOT_ID))

    lines = log_capture.wait_for("Cannot request historical quotes")
    assert any(
        "[ERROR]" in line and "not published by Gate.io" in line and "BTC_USDT.GATE_IO" in line
        for line in lines
    ), lines
    assert [item for item in harness.published if isinstance(item, QuoteTick)] == []


async def test_the_refusal_never_touches_the_ticker_endpoint(harness: Harness) -> None:
    """``GET /*/tickers`` returns a current row, and one row is not a history."""
    harness.client._spot_http = ExplodingTickersHttp()  # type: ignore[assignment]
    responses: list[Any] = []
    harness.client._handle_quote_ticks = (  # type: ignore[method-assign]
        lambda *args, **kwargs: responses.append(args)
    )

    await harness.client._request_quote_ticks(quote_request(SPOT_ID))

    assert responses == [], "a response would complete the request with invented history"


async def test_a_request_for_the_venue_ticker_is_refused_without_raising(
    harness: Harness,
    log_capture: Any,  # the shared session fixture from `tests/conftest.py`
) -> None:
    """Fails on the pre-change tree with ``NotImplementedError`` from the base class.

    ``_request`` is the last hook the platform's adapter template declares that
    this client left to the base class. Gate.io serves the ticker as a live
    channel only, so the honest answer is a refusal naming that fact.
    """
    harness.client._spot_http = ExplodingTickersHttp()  # type: ignore[assignment]
    log_capture.mark()

    await harness.client._request(
        RequestData(
            DataType(GateioTicker, metadata={"instrument_id": SPOT_ID}),
            SPOT_ID,
            None,
            None,
            0,
            GATEIO_CLIENT_ID,
            GATEIO_VENUE,
            None,
            UUID4(),
            0,
            None,
        ),
    )

    # Waited on by a fragment unique to this refusal: the quote refusal in this
    # same file also begins "Cannot request", and under the full suite its line
    # can land after the mark and end the wait before this one has been written.
    lines = log_capture.wait_for("no history for any venue-native data type")
    assert any("[ERROR]" in line and "GateioTicker" in line for line in lines), lines
    assert tickers(harness) == []


async def test_the_refusal_reads_request_params_without_choking(harness: Harness) -> None:
    request = quote_request(PERP_ID)
    assert request.params == {"some_adapter_param": True}

    await harness.client._request_quote_ticks(request)

    assert [item for item in harness.published if isinstance(item, QuoteTick)] == []
